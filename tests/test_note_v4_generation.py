"""One-call V4 generation and optional audit/activation contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.note_audit import candidate_sha256
from learnnest.note_generation import (
    NoteGenerationError,
    _repair_v4_provider_json,
    build_and_activate_external_v4_note,
    generate_and_activate_v4_note,
    rerender_and_activate_note,
    validate_external_v4_note,
)
from learnnest.note_templates import (
    builtin_template,
    load_template_file,
    template_snapshot_sha256,
)
from learnnest.publication import select_published_note_path
from learnnest.task_store import load_task, write_task_atomic
from learnnest.validation import validate_task


class FakeV4Provider:
    name = "fake-v4"
    model = "fake-model"
    safe_input_tokens = 100_000

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def generate_v4(self, content_pack_json: str, template_snapshot_json: str) -> str:
        self.calls.append((content_pack_json, template_snapshot_json))
        return self.response


class FakeAuditor:
    name = "fake-auditor"
    model = "audit-model"

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str, str, str]] = []

    def audit_note(
        self,
        content_pack_json: str,
        candidate_note_json: str,
        template_snapshot_json: str,
        statement_manifest_json: str,
    ) -> str:
        self.calls.append(
            (
                content_pack_json,
                candidate_note_json,
                template_snapshot_json,
                statement_manifest_json,
            )
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _workspace(tmp_path: Path) -> tuple[Path, ContentPack]:
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    pack = ContentPack(
        task_id="20260719-a1b2c3d4",
        source_fingerprint="a1b2c3d4",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="材料说明了核心概念和学习用途。",
                artifact_path="transcript.json",
            )
        ],
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2), encoding="utf-8"
    )
    (task_dir / "transcript.json").write_text("{}", encoding="utf-8")
    write_task_atomic(
        task_dir,
        TaskRecord(
            task_id=pack.task_id,
            source_path="C:/lesson.mp4",
            source_fingerprint=pack.source_fingerprint,
            title="lesson",
            artifacts={"content_pack": ["content_pack.json"]},
            stages={"content_pack": "completed"},
        ),
    )
    return task_dir, pack


def _candidate_json(template_id: str, template_sha256: str, *, title: str) -> str:
    return json.dumps(
        {
            "schema_version": "4.0",
            "task_id": "20260719-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "template_id": template_id,
            "template_sha256": template_sha256,
            "title": {"text": title, "evidence_ids": ["tr_0001"]},
            "blocks": [
                {
                    "block_id": "core",
                    "semantic_block": "core_facts",
                    "items": [
                        {
                            "content": {
                                "text": "材料说明了核心概念和学习用途。",
                                "evidence_ids": ["tr_0001"],
                            }
                        }
                    ],
                },
                {
                    "block_id": "concepts",
                    "semantic_block": "concept_cards",
                    "items": [
                        {
                            "title": {
                                "text": "核心概念",
                                "evidence_ids": ["tr_0001"],
                            },
                            "content": {
                                "text": "核心概念可以组织学习用途。",
                                "evidence_ids": ["tr_0001"],
                            },
                        }
                    ],
                },
                {
                    "block_id": "review",
                    "semantic_block": "review_questions",
                    "items": [],
                },
            ],
            "ai_supplements": [],
        },
        ensure_ascii=False,
    )


def _malformed_content_wrapper_json(template_id: str, template_sha256: str) -> str:
    """Model-shaped V4 output with an anonymous wrapper around content."""
    lines = json.dumps(
        json.loads(_candidate_json(template_id, template_sha256, title="标题")),
        ensure_ascii=False,
        indent=2,
    ).splitlines()
    concepts_index = next(
        index
        for index, line in enumerate(lines)
        if line == '      "block_id": "concepts",'
    )
    content_index = next(
        index
        for index in range(concepts_index + 1, len(lines))
        if lines[index] == '          "content": {'
    )
    content_close = next(
        index
        for index in range(content_index + 1, len(lines) - 1)
        if lines[index] == "          }"
        and lines[index + 1]
        in {
            "        },",
            "        }",
        }
    )
    lines.insert(content_index, "          {")
    for index in range(content_index + 1, content_close + 1):
        lines[index] = "  " + lines[index]
    lines.insert(content_close + 1, "          }")
    return "\n".join(lines) + "\n"


def _audit_json(candidate_json: str, *, verdict: str = "passed") -> str:
    from learnnest.note_validation import (
        iter_factual_statement_entries,
        parse_generated_note,
    )

    candidate = parse_generated_note(candidate_json)
    assessment = "supported" if verdict == "passed" else "ambiguous"
    payload = {
        "schema_version": "1.0",
        "task_id": candidate.task_id,
        "source_fingerprint": candidate.source_fingerprint,
        "candidate_sha256": candidate_sha256(candidate),
        "verdict": verdict,
        "statement_audits": [
            {
                "candidate_path": path,
                "assessment": assessment,
                "rationale": "审验结果。",
            }
            for path, _ in iter_factual_statement_entries(candidate)
        ],
        "issues": (
            []
            if verdict == "passed"
            else [
                {
                    "category": "ambiguity",
                    "severity": "medium",
                    "candidate_paths": ["/blocks/0/items/0/content"],
                    "rationale": "需要人工复核。",
                }
            ]
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


def test_v4_default_mode_calls_the_model_once_and_activates_source_valid_note(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    provider = FakeV4Provider(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="第一次笔记",
        )
    )

    result = generate_and_activate_v4_note(
        task_dir,
        provider,
        tmp_path,
        template=template,
    )

    assert result.activated is True
    assert len(provider.calls) == 1
    active = load_task(task_dir)
    bundle = task_dir / Path(active.artifacts["note"][0]).parent
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["source_validation_status"] == "source_valid"
    assert metadata["review"]["status"] == "not_requested"
    assert metadata["model_call_count"] == 1
    assert (bundle / "template.json").is_file()


def test_v4_repairs_one_anonymous_item_wrapper_before_strict_validation(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    raw = _malformed_content_wrapper_json(
        template.template_id,
        template_snapshot_sha256(template),
    )

    repaired, normalizations = _repair_v4_provider_json(raw)

    assert repaired is not None
    assert normalizations == ["merged anonymous V4 item object with content"]
    result = generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(raw),
        tmp_path,
        template=template,
    )

    assert result.activated is True
    metadata = json.loads(
        (result.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["response_normalizations"] == normalizations
    assert not (result.bundle_path / "attempt-1.raw.txt").exists()
    assert not (result.bundle_path / "attempt-1.repaired.json").exists()


def test_v4_retains_provider_debug_files_only_when_explicitly_requested(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    raw = _malformed_content_wrapper_json(
        template.template_id,
        template_snapshot_sha256(template),
    )

    result = generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(raw),
        tmp_path,
        template=template,
        retain_debug_artifacts=True,
    )

    assert (result.bundle_path / "attempt-1.raw.txt").read_text(encoding="utf-8") == raw
    assert (result.bundle_path / "attempt-1.repaired.json").is_file()


def test_v4_provider_normalizes_a_uniquely_derived_ocr_parent_frame(
    tmp_path: Path,
) -> None:
    task_dir, pack = _workspace(tmp_path)
    pack = pack.model_copy(
        update={
            "evidence": [
                *pack.evidence,
                Evidence(
                    id="fr_0001",
                    kind="frame",
                    start_ms=1_000,
                    artifact_path="frames/selected/fr_0001.png",
                    related_evidence_ids=["ocr_0001"],
                ),
                Evidence(
                    id="ocr_0001",
                    kind="ocr",
                    text="核心概念图",
                    artifact_path="ocr.json",
                    frame_id="fr_0001",
                ),
            ]
        }
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2), encoding="utf-8"
    )
    frame_path = task_dir / "frames" / "selected" / "fr_0001.png"
    frame_path.parent.mkdir(parents=True)
    frame_path.write_bytes(b"frame")
    template = builtin_template("concept-explanation")
    payload = json.loads(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="带画面证据的笔记",
        )
    )
    payload["blocks"][1]["items"][0]["title"]["evidence_ids"] = ["ocr_0001"]
    payload["blocks"][1]["items"][0]["content"]["evidence_ids"] = ["ocr_0001"]

    result = generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(json.dumps(payload, ensure_ascii=False)),
        tmp_path,
        template=template,
    )

    assert result.activated is True
    active = load_task(task_dir)
    bundle = task_dir / Path(active.artifacts["note"][0]).parent
    note = json.loads((bundle / "note.json").read_text(encoding="utf-8"))
    concept = note["blocks"][1]["items"][0]
    assert concept["title"]["evidence_ids"] == ["ocr_0001", "fr_0001"]
    assert concept["content"]["evidence_ids"] == ["ocr_0001", "fr_0001"]
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["evidence_normalizations"] == [
        {
            "kind": "v4_factual_ocr_parent",
            "candidate_path": "/blocks/1/items/0/title",
            "trigger_ocr_evidence_ids": ["ocr_0001"],
            "added_parent_frame_id": "fr_0001",
        },
        {
            "kind": "v4_factual_ocr_parent",
            "candidate_path": "/blocks/1/items/0/content",
            "trigger_ocr_evidence_ids": ["ocr_0001"],
            "added_parent_frame_id": "fr_0001",
        },
    ]


def test_v4_template_presentation_warning_does_not_block_default_activation(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    payload = json.loads(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="没有概念标题的笔记",
        )
    )
    del payload["blocks"][1]["items"][0]["title"]

    result = generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(json.dumps(payload, ensure_ascii=False)),
        tmp_path,
        template=template,
    )

    assert result.activated is True
    active = load_task(task_dir)
    bundle = task_dir / Path(active.artifacts["note"][0]).parent
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["source_validation_status"] == "source_valid"
    assert metadata["template_validation"] == {
        "status": "flagged",
        "warnings": [
            "generated note template block requires evidence-backed item titles: "
            "concepts"
        ],
    }
    assert validate_task(task_dir) == []


def test_v4_gate_retains_a_template_flagged_candidate_even_when_audit_passes(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    payload = json.loads(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="需要模板补全的候选",
        )
    )
    del payload["blocks"][1]["items"][0]["title"]
    candidate = json.dumps(payload, ensure_ascii=False)
    provider = FakeV4Provider(candidate)

    result = generate_and_activate_v4_note(
        task_dir,
        provider,
        tmp_path,
        template=template,
        review_mode="gate",
        auditor=FakeAuditor(_audit_json(candidate)),
    )

    assert result.activated is False
    assert len(provider.calls) == 1
    assert load_task(task_dir).artifacts.get("note") is None
    bundle = result.bundle_path
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["source_validation_status"] == "source_valid"
    assert metadata["template_validation"]["status"] == "flagged"
    assert metadata["review"]["status"] == "passed"
    assert metadata["activation_decision"] == "retain_candidate"


def test_v4_external_candidate_does_not_normalize_an_ocr_parent_frame(
    tmp_path: Path,
) -> None:
    task_dir, pack = _workspace(tmp_path)
    pack = pack.model_copy(
        update={
            "evidence": [
                *pack.evidence,
                Evidence(
                    id="fr_0001",
                    kind="frame",
                    start_ms=1_000,
                    artifact_path="frames/selected/fr_0001.png",
                    related_evidence_ids=["ocr_0001"],
                ),
                Evidence(
                    id="ocr_0001",
                    kind="ocr",
                    text="核心概念图",
                    artifact_path="ocr.json",
                    frame_id="fr_0001",
                ),
            ]
        }
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2), encoding="utf-8"
    )
    template = builtin_template("concept-explanation")
    payload = json.loads(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="外部候选",
        )
    )
    payload["blocks"][1]["items"][0]["content"]["evidence_ids"] = ["ocr_0001"]
    external = tmp_path / "external-v4.json"
    external.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    errors = validate_external_v4_note(task_dir, external, template=template)

    assert any("ocr_0001 without parent frame fr_0001" in error for error in errors)
    with pytest.raises(NoteGenerationError, match="without parent frame"):
        build_and_activate_external_v4_note(
            task_dir,
            external,
            tmp_path,
            template=template,
        )


def test_v4_retry_reconciles_pending_publication_without_another_model_call(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    first = generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(
            _candidate_json(
                template.template_id,
                template_snapshot_sha256(template),
                title="已发布笔记",
            )
        ),
        tmp_path,
        template=template,
    )
    active = load_task(task_dir)
    write_task_atomic(
        task_dir,
        active.model_copy(
            update={
                "stages": {**active.stages, "publish": StageStatus.RUNNING},
                "artifacts": {
                    stage: paths
                    for stage, paths in active.artifacts.items()
                    if stage != "publish"
                },
            }
        ),
    )
    retry_provider = FakeV4Provider(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="不应生成",
        )
    )

    result = generate_and_activate_v4_note(
        task_dir,
        retry_provider,
        tmp_path,
        template=template,
    )

    assert retry_provider.calls == []
    assert result.bundle_path == first.bundle_path
    assert result.task.stages["publish"] is StageStatus.COMPLETED


def test_v4_retry_recovers_a_durable_auto_activation_candidate_without_a_model_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.note_generation as note_generation

    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    provider = FakeV4Provider(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="中断前已落盘的笔记",
        )
    )
    original_activate = note_generation.activate_note_bundle

    def crash_before_activation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt("simulate process interruption")

    monkeypatch.setattr(
        note_generation, "activate_note_bundle", crash_before_activation
    )
    with pytest.raises(KeyboardInterrupt, match="simulate process interruption"):
        generate_and_activate_v4_note(task_dir, provider, tmp_path, template=template)

    assert len(provider.calls) == 1
    pending = load_task(task_dir)
    assert pending.active_attempt_id is not None
    monkeypatch.setattr(note_generation, "activate_note_bundle", original_activate)
    retry_provider = FakeV4Provider(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="不应再次调用模型",
        )
    )

    result = generate_and_activate_v4_note(
        task_dir, retry_provider, tmp_path, template=template
    )

    assert result.activated is True
    assert retry_provider.calls == []
    assert load_task(task_dir).active_attempt_id is None


def test_v4_recovery_refuses_a_tampered_candidate_without_calling_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.note_generation as note_generation

    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    provider = FakeV4Provider(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="中断后被篡改的笔记",
        )
    )
    original_activate = note_generation.activate_note_bundle

    def crash_before_activation(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt("simulate process interruption")

    monkeypatch.setattr(
        note_generation, "activate_note_bundle", crash_before_activation
    )
    with pytest.raises(KeyboardInterrupt):
        generate_and_activate_v4_note(task_dir, provider, tmp_path, template=template)

    bundle = next((task_dir / "generated_notes").iterdir())
    payload = json.loads((bundle / "note.json").read_text(encoding="utf-8"))
    payload["title"]["evidence_ids"] = ["tr_9999"]
    (bundle / "note.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(note_generation, "activate_note_bundle", original_activate)
    retry_provider = FakeV4Provider(provider.response)

    with pytest.raises(RuntimeError, match="cannot be safely activated"):
        generate_and_activate_v4_note(
            task_dir, retry_provider, tmp_path, template=template
        )

    assert retry_provider.calls == []
    assert load_task(task_dir).artifacts.get("note") is None


def test_v4_report_keeps_source_valid_note_when_audit_is_unavailable(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    candidate = _candidate_json(
        template.template_id,
        template_snapshot_sha256(template),
        title="报告模式笔记",
    )
    provider = FakeV4Provider(candidate)
    auditor = FakeAuditor(RuntimeError("timeout"))

    result = generate_and_activate_v4_note(
        task_dir,
        provider,
        tmp_path,
        template=template,
        review_mode="report",
        auditor=auditor,
    )

    assert result.activated is True
    assert len(provider.calls) == 1
    assert len(auditor.calls) == 1
    active = load_task(task_dir)
    bundle = task_dir / Path(active.artifacts["note"][0]).parent
    report = json.loads((bundle / "note_audit.json").read_text(encoding="utf-8"))
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert report["status"] == "unavailable"
    assert "模型辅助审验，不等于人工确认" in report["disclaimer"]
    assert metadata["model_call_count"] == 2


def test_v4_report_keeps_source_valid_note_when_audit_json_is_invalid(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    provider = FakeV4Provider(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="无效审验报告笔记",
        )
    )
    auditor = FakeAuditor("not-json")

    result = generate_and_activate_v4_note(
        task_dir,
        provider,
        tmp_path,
        template=template,
        review_mode="report",
        auditor=auditor,
    )

    assert result.activated is True
    assert len(provider.calls) == 1
    assert len(auditor.calls) == 1
    active = load_task(task_dir)
    bundle = task_dir / Path(active.artifacts["note"][0]).parent
    report = json.loads((bundle / "note_audit.json").read_text(encoding="utf-8"))
    assert report["status"] == "unavailable"
    assert report["errors"] == ["NoteAudit response failed schema validation"]


def test_v4_gate_retains_candidate_without_replacing_the_old_active_note(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    template_sha = template_snapshot_sha256(template)
    first = generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(
            _candidate_json(template.template_id, template_sha, title="旧笔记")
        ),
        tmp_path,
        template=template,
    )
    before = load_task(task_dir)
    old_note = (task_dir / before.artifacts["note"][0]).read_bytes()
    published_path = select_published_note_path(task_dir, before, tmp_path)
    old_published = published_path.read_bytes()
    candidate = _candidate_json(template.template_id, template_sha, title="新候选")

    result = generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(candidate),
        tmp_path,
        template=template,
        review_mode="gate",
        auditor=FakeAuditor(_audit_json(candidate, verdict="flagged")),
    )

    assert first.activated is True
    assert result.activated is False
    after = load_task(task_dir)
    assert (task_dir / after.artifacts["note"][0]).read_bytes() == old_note
    assert published_path.read_bytes() == old_published
    candidate_metadata = json.loads(
        (result.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert candidate_metadata["review"]["status"] == "flagged"
    assert candidate_metadata["activation_decision"] == "retain_candidate"
    assert candidate_metadata["model_call_count"] == 2


def test_v4_external_candidate_uses_the_same_template_contract_without_provider(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    external = tmp_path / "external-v4.json"
    external.write_text(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="外部 V4 笔记",
        ),
        encoding="utf-8",
    )

    assert validate_external_v4_note(task_dir, external, template=template) == []
    result = build_and_activate_external_v4_note(
        task_dir,
        external,
        tmp_path,
        template=template,
    )

    assert result.activated is True
    bundle = result.bundle_path
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["operation"] == "external"
    assert metadata["model_call_count"] == 0


def test_v4_rerender_uses_the_persisted_snapshot_after_template_file_changes(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    payload = builtin_template("concept-explanation").model_dump(mode="json")
    payload["template_id"] = "my-template"
    payload["sections"][0]["heading"] = "原始核心章节"
    template_path = tmp_path / "my-template.json"
    template_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    template = load_template_file(template_path)
    generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(
            _candidate_json(
                template.template_id,
                template_snapshot_sha256(template),
                title="可重渲染笔记",
            )
        ),
        tmp_path,
        template=template,
    )
    payload["sections"][0]["heading"] = "被修改的章节"
    template_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    before = load_task(task_dir)
    original_bundle = task_dir / Path(before.artifacts["note"][0]).parent

    rerender_and_activate_note(task_dir, tmp_path)

    active = load_task(task_dir)
    bundle = task_dir / Path(active.artifacts["note"][0]).parent
    markdown = (bundle / "note.md").read_text(encoding="utf-8")
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert bundle == original_bundle
    assert "## 原始核心章节" in markdown
    assert "被修改的章节" not in markdown
    assert metadata["render_count"] == 1


def test_validate_rejects_an_active_v4_bundle_with_a_tampered_template_snapshot(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(
            _candidate_json(
                template.template_id,
                template_snapshot_sha256(template),
                title="可校验笔记",
            )
        ),
        tmp_path,
        template=template,
    )
    active = load_task(task_dir)
    bundle = task_dir / Path(active.artifacts["note"][0]).parent
    snapshot = json.loads((bundle / "template.json").read_text(encoding="utf-8"))
    snapshot["sections"][0]["heading"] = "被篡改的章节"
    (bundle / "template.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )

    errors = validate_task(task_dir)

    assert "generated note template_sha256 does not match template snapshot" in errors


def test_validate_rejects_an_active_v4_bundle_when_content_pack_sha_changes(
    tmp_path: Path,
) -> None:
    task_dir, pack = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(
            _candidate_json(
                template.template_id,
                template_snapshot_sha256(template),
                title="来源哈希笔记",
            )
        ),
        tmp_path,
        template=template,
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    errors = validate_task(task_dir)

    assert "active GeneratedNote 4.0 metadata content_pack_sha256 is invalid" in errors


def test_v4_rerender_refuses_to_rebind_a_note_to_a_changed_content_pack(
    tmp_path: Path,
) -> None:
    task_dir, pack = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    first = generate_and_activate_v4_note(
        task_dir,
        FakeV4Provider(
            _candidate_json(
                template.template_id,
                template_snapshot_sha256(template),
                title="不可重绑笔记",
            )
        ),
        tmp_path,
        template=template,
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="content_pack_sha256"):
        rerender_and_activate_note(task_dir, tmp_path)

    assert (
        load_task(task_dir).artifacts["note"][0]
        == (first.bundle_path / "note.json").relative_to(task_dir).as_posix()
    )


def test_v4_context_preflight_stops_before_the_paid_provider_call(
    tmp_path: Path,
) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    provider = FakeV4Provider(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="不会发送",
        )
    )
    provider.safe_input_tokens = 1

    with pytest.raises(NoteGenerationError, match="safe input budget"):
        generate_and_activate_v4_note(
            task_dir,
            provider,
            tmp_path,
            template=template,
        )

    assert provider.calls == []
    assert load_task(task_dir).artifacts.get("note") is None


def test_v4_invalid_evidence_never_reaches_audit_or_active_note(tmp_path: Path) -> None:
    task_dir, _ = _workspace(tmp_path)
    template = builtin_template("concept-explanation")
    payload = json.loads(
        _candidate_json(
            template.template_id,
            template_snapshot_sha256(template),
            title="错误证据",
        )
    )
    payload["blocks"][0]["items"][0]["content"]["evidence_ids"] = ["tr_9999"]
    provider = FakeV4Provider(json.dumps(payload, ensure_ascii=False))
    auditor = FakeAuditor(_audit_json(provider.response))

    with pytest.raises(NoteGenerationError, match="unknown evidence id"):
        generate_and_activate_v4_note(
            task_dir,
            provider,
            tmp_path,
            template=template,
            review_mode="report",
            auditor=auditor,
        )

    assert len(provider.calls) == 1
    assert auditor.calls == []
    assert load_task(task_dir).artifacts.get("note") is None
