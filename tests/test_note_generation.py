from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from learnnest.citation_audit import statement_citation_packet_sha256
from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.note_evidence_scope import StatementCitationPacketV12
from learnnest.task_store import load_task, write_task_atomic
from learnnest.validation import validate_task


@pytest.fixture
def v3_render_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> list[TaskRecord]:
    """Keep Task 5 generation tests independent from the Task 6 renderer."""
    import learnnest.note_generation as note_generation

    render_v2 = note_generation.render_generated_note
    rendered_tasks: list[TaskRecord] = []

    def render_for_task5(task, content_pack, note, *, asset_prefix: str) -> str:
        if note.schema_version == "3.0":
            rendered_tasks.append(task)
            return f"<!-- learnnest-task-id: {task.task_id} -->\n# {note.title}\n"
        return render_v2(
            task,
            content_pack,
            note,
            asset_prefix=asset_prefix,
        )

    monkeypatch.setattr(
        note_generation,
        "render_generated_note",
        render_for_task5,
    )
    return rendered_tasks


def _forbid_provider_citation_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard non-provider paths from the new paid draft/audit pipeline."""
    import learnnest.note_generation as note_generation

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("non-provider path must not enter the citation generation flow")

    for name in (
        "build_draft_evidence_scope",
        "build_draft_execution_json",
        "build_statement_citation_packet",
        "_citation_auditor",
    ):
        monkeypatch.setattr(note_generation, name, forbidden)


class FakeNoteProvider:
    name = "fake"
    model = "queued"

    def __init__(
        self,
        responses: list[str | Exception] | None = None,
        plans: list[str | Exception] | None = None,
        reviews: list[str | Exception] | None = None,
        citation_audits: list[str | Exception | Callable[[str], str]] | None = None,
        error: Exception | None = None,
    ):
        self.responses = iter(responses or [])
        self.plans = iter(plans or [valid_coverage_plan_json()])
        self.reviews = iter(reviews or [valid_review_json()])
        self.citation_audits = (
            iter(citation_audits) if citation_audits is not None else None
        )
        self.error = error
        self.calls: list[tuple[str, tuple[str, ...], str | None, str]] = []
        self.plan_calls: list[str] = []
        self.plan_retries: list[tuple[str, ...]] = []
        self.review_calls: list[tuple[str, str, str, str]] = []
        self.review_retries: list[tuple[str, ...]] = []
        self.citation_calls: list[tuple[str, tuple[str, ...]]] = []
        self.events: list[str] = []

    def generate(
        self,
        draft_scope_json: str,
        validation_feedback: tuple[str, ...],
        *,
        draft_execution_json: str,
        requested_note_type: str | None = None,
    ) -> str:
        self.calls.append(
            (
                draft_scope_json,
                validation_feedback,
                requested_note_type,
                draft_execution_json,
            )
        )
        self.events.append("draft")
        if self.error is not None:
            raise self.error
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def plan(
        self,
        content_pack_json: str,
        *,
        validation_feedback: tuple[str, ...] = (),
    ) -> str:
        self.events.append("plan")
        self.plan_calls.append(content_pack_json)
        self.plan_retries.append(validation_feedback)
        response = next(self.plans)
        if isinstance(response, Exception):
            raise response
        return response

    def review(
        self,
        content_pack_json: str,
        candidate_note_json: str,
        coverage_plan_json: str,
        statement_manifest_json: str,
        *,
        validation_feedback: tuple[str, ...] = (),
    ) -> str:
        self.events.append("review")
        self.review_calls.append(
            (
                content_pack_json,
                candidate_note_json,
                coverage_plan_json,
                statement_manifest_json,
            )
        )
        self.review_retries.append(validation_feedback)
        response = next(self.reviews)
        if isinstance(response, Exception):
            raise response
        return response

    def audit_citations(
        self,
        packet_json: str,
        *,
        validation_feedback: tuple[str, ...] = (),
    ) -> str:
        self.events.append("citation_audit")
        self.citation_calls.append((packet_json, validation_feedback))
        if self.citation_audits is None:
            return valid_citation_audit_json(packet_json)
        response = next(self.citation_audits)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(packet_json)
        return response


def valid_note_json() -> str:
    return json.dumps(
        {
            "schema_version": "2.0",
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "title": "设置模型参数",
            "audience": {"text": "工具学习者", "evidence_ids": ["tr_0001"]},
            "summary": {
                "text": "视频演示了配置流程。",
                "evidence_ids": ["tr_0001", "fr_0001"],
            },
            "key_points": [
                {"text": "需要确认模型名称。", "evidence_ids": ["ocr_0001"]}
            ],
            "steps": [{"order": 1, "text": "打开设置。", "evidence_ids": ["tr_0001"]}],
            "cautions": [],
            "ai_supplements": [{"text": "不同版本入口可能变化。"}],
        },
        ensure_ascii=False,
    )


def valid_coverage_plan_json() -> str:
    return json.dumps(
        {
            "schema_version": "1.1",
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "units": [
                {
                    "label": "核心结论",
                    "source_evidence_ids": ["tr_0001"],
                    "rationale": "缺失会使材料的核心操作不完整。",
                }
            ],
            "visuals": [
                {
                    "frame_evidence_id": "fr_0001",
                    "supporting_ocr_evidence_ids": [],
                    "disposition": "use",
                    "unit_label": "核心结论",
                    "rationale": "测试帧直接支持中央概念。",
                }
            ],
        },
        ensure_ascii=False,
    )


def valid_review_json(*, verdict: str = "approve") -> str:
    missing = verdict == "reject"
    return json.dumps(
        {
            "schema_version": "1.1",
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "verdict": verdict,
            "coverage_units": [
                {
                    "label": "核心结论",
                    "source_evidence_ids": ["tr_0001"],
                    "candidate_paths": ["/summary", "/concepts/0/explanation"],
                    "status": "missing" if missing else "covered",
                    "rationale": (
                        "候选缺少中央结论。" if missing else "候选有实质解释。"
                    ),
                }
            ],
            "statement_audits": [
                {
                    "candidate_path": "/summary",
                    "status": "aligned",
                    "rationale": "摘要证据直接支持正文。",
                },
                {
                    "candidate_path": "/concepts/0/explanation",
                    "status": "aligned",
                    "rationale": "概念证据直接支持正文。",
                },
            ],
            "issues": [],
        },
        ensure_ascii=False,
    )


def valid_v3_note_json(
    note_type: str = "concept_explanation",
    *,
    classification_evidence_ids: list[str] | None = None,
    summary_evidence_ids: list[str] | None = None,
) -> str:
    payload: dict[str, object] = {
        "schema_version": "3.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "classification_evidence_ids": (
            ["tr_0001"]
            if classification_evidence_ids is None
            else classification_evidence_ids
        ),
        "note_type": note_type,
        "title": "设置模型参数",
        "summary": {
            "text": "视频演示了配置流程。",
            "evidence_ids": (
                ["tr_0001", "fr_0001"]
                if summary_evidence_ids is None
                else summary_evidence_ids
            ),
        },
        "ai_supplements": [],
    }
    if note_type == "concept_explanation":
        payload.update(
            {
                "concepts": [
                    {
                        "title": "模型参数",
                        "explanation": {
                            "text": "材料说明了模型参数的设置方法。",
                            "evidence_ids": ["tr_0001"],
                        },
                    }
                ],
                "background": None,
                "relationships": [],
                "misconceptions": [],
                "review": None,
            }
        )
    elif note_type == "resource_share":
        payload.update(
            {
                "resources": [
                    {
                        "name": {
                            "text": "模型配置入口",
                            "evidence_ids": ["tr_0001"],
                        },
                        "value": {
                            "text": "该入口用于设置模型参数。",
                            "evidence_ids": ["tr_0001"],
                        },
                        "suitable_for": None,
                        "access_or_usage": None,
                        "locator": None,
                        "limitations": [],
                    }
                ],
                "reminders": [],
            }
        )
    else:
        payload.update(
            {
                "goal": {
                    "text": "完成模型参数配置。",
                    "evidence_ids": ["tr_0001"],
                },
                "prerequisites": [],
                "steps": [
                    {
                        "order": 1,
                        "title": "打开设置",
                        "action": {
                            "text": "打开设置页面。",
                            "evidence_ids": ["tr_0001", "fr_0001"],
                        },
                        "expected_result": None,
                    }
                ],
                "troubleshooting": [],
                "completion_checks": [],
                "cautions": [],
            }
        )
    return json.dumps(payload, ensure_ascii=False)


def valid_citation_audit_json(packet_json: str) -> str:
    """Return a locally valid approve response for a generated packet."""
    from learnnest.citation_audit import statement_citation_packet_sha256
    from learnnest.note_evidence_scope import StatementCitationPacketV12

    packet = StatementCitationPacketV12.model_validate_json(packet_json)
    return json.dumps(
        {
            "schema_version": "1.2",
            "task_id": packet.task_id,
            "source_fingerprint": packet.source_fingerprint,
            "candidate_sha256": packet.candidate_sha256,
            "packet_sha256": statement_citation_packet_sha256(packet),
            "verdict": "approve",
            "clauses": [
                {
                    "clause_id": clause.clause_id,
                    "status": "entailed",
                    "support_evidence_ids": [
                        evidence.id for evidence in statement.snippets
                    ],
                    "reason_code": "direct",
                }
                for statement in packet.statements
                for clause in statement.clauses
            ],
        },
        ensure_ascii=False,
    )


def rejecting_citation_audit_json(packet_json: str) -> str:
    """Return one valid semantic rejection without exposing candidate history."""
    payload = json.loads(valid_citation_audit_json(packet_json))
    clauses = payload["clauses"]
    assert isinstance(clauses, list)
    first = clauses[0]
    assert isinstance(first, dict)
    first.update(
        {
            "status": "unsupported",
            "support_evidence_ids": [],
            "reason_code": "missing_direct_support",
        }
    )
    payload["verdict"] = "reject"
    return json.dumps(payload, ensure_ascii=False)


def locally_invalid_citation_audit_json(packet_json: str) -> str:
    """Return parseable audit JSON with a forged program-owned clause ID."""
    payload = json.loads(valid_citation_audit_json(packet_json))
    clauses = payload["clauses"]
    assert isinstance(clauses, list)
    first = clauses[0]
    assert isinstance(first, dict)
    first["clause_id"] = "clause-9999-0001"
    return json.dumps(payload, ensure_ascii=False)


def task_workspace(tmp_path: Path) -> tuple[Path, TaskRecord]:
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    pack = ContentPack(
        task_id="20260711-a1b2c3d4",
        source_fingerprint="a1b2c3d4",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="打开设置。",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=500,
                artifact_path="frames/selected/fr_0001.png",
                related_evidence_ids=["ocr_0001"],
            ),
            Evidence(
                id="ocr_0001",
                kind="ocr",
                text="模型配置",
                artifact_path="ocr.json",
                frame_id="fr_0001",
            ),
        ],
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2), encoding="utf-8"
    )
    (task_dir / "transcript.json").write_text("{}", encoding="utf-8")
    (task_dir / "ocr.json").write_text("{}", encoding="utf-8")
    frame = task_dir / "frames" / "selected" / "fr_0001.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"frame")
    (task_dir / "note.json").write_text("{}", encoding="utf-8")
    (task_dir / "note.md").write_text("old", encoding="utf-8")
    (task_dir / "published_note.md").write_text("old", encoding="utf-8")
    task = TaskRecord(
        task_id=pack.task_id,
        source_path="C:/videos/lesson.mp4",
        source_fingerprint=pack.source_fingerprint,
        title="lesson",
        profile="note",
        stages={
            "content_pack": "completed",
            "note": "completed",
            "publish": "completed",
        },
        artifacts={
            "content_pack": ["content_pack.json"],
            "note": ["note.json", "note.md"],
            "publish": ["published_note.md"],
        },
    )
    write_task_atomic(task_dir, task)
    return task_dir, task


def _task(*, note_type_override: str | None = None) -> TaskRecord:
    return TaskRecord(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        note_type_override=note_type_override,
    )


@pytest.mark.parametrize(
    ("cli_value", "stored", "requested", "source"),
    [
        (None, None, None, "auto"),
        (None, "resource_share", "resource_share", "source"),
        ("concept", "resource_share", "concept_explanation", "cli"),
        ("auto", "resource_share", None, "auto"),
    ],
)
def test_note_type_priority(
    cli_value: str | None,
    stored: str | None,
    requested: str | None,
    source: str,
) -> None:
    from learnnest.note_generation import resolve_note_type_selection

    selection = resolve_note_type_selection(_task(note_type_override=stored), cli_value)

    assert (selection.requested, selection.source) == (requested, source)


def test_explicit_auto_ignores_a_stored_source_override() -> None:
    from learnnest.note_generation import resolve_note_type_selection

    selection = resolve_note_type_selection(
        _task(note_type_override="resource_share"), "auto"
    )

    assert (selection.requested, selection.source) == (None, "auto")


def test_provider_retries_keep_the_same_concrete_request(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(["not-json", valid_v3_note_json()])

    generate_note_bundle(
        task_dir,
        provider,
        note_type="concept",
        run_id="stable-request",
    )

    assert [call[2] for call in provider.calls] == [
        "concept_explanation",
        "concept_explanation",
    ]


def test_failed_generation_metadata_records_the_concrete_selection(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(["bad-one", "bad-two"])

    with pytest.raises(NoteGenerationError) as captured:
        generate_note_bundle(
            task_dir,
            provider,
            note_type="resource",
            run_id="failed-selection",
        )

    metadata = json.loads(
        (captured.value.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["note_type"] == "resource_share"
    assert metadata["note_type_source"] == "cli"


def test_new_provider_generation_rejects_v2(tmp_path: Path) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, task = task_workspace(tmp_path)

    with pytest.raises(NoteGenerationError, match="GeneratedNote 3.0"):
        generate_note_bundle(
            task_dir,
            FakeNoteProvider([valid_note_json(), valid_note_json()]),
            run_id="provider-v2",
        )

    assert load_task(task_dir).artifacts == task.artifacts


def test_external_concrete_mismatch_never_activates_bundle(tmp_path: Path) -> None:
    from learnnest.note_generation import (
        NoteGenerationError,
        build_external_note_bundle,
    )

    task_dir, task = task_workspace(tmp_path)
    external = tmp_path / "resource-note.json"
    external.write_text(valid_v3_note_json("resource_share"), encoding="utf-8")

    with pytest.raises(
        NoteGenerationError, match="does not match requested"
    ) as captured:
        build_external_note_bundle(
            task_dir,
            external,
            note_type="concept",
            run_id="mismatch",
        )

    metadata = json.loads(
        (captured.value.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["note_type"] == "concept_explanation"
    assert metadata["note_type_source"] == "cli"
    assert load_task(task_dir).artifacts == task.artifacts


def test_new_external_activation_rejects_v2(tmp_path: Path) -> None:
    from learnnest.note_generation import (
        NoteGenerationError,
        build_external_note_bundle,
    )

    task_dir, task = task_workspace(tmp_path)
    external = tmp_path / "legacy-note.json"
    external.write_text(valid_note_json(), encoding="utf-8")

    with pytest.raises(NoteGenerationError, match="GeneratedNote 3.0"):
        build_external_note_bundle(
            task_dir,
            external,
            note_type="auto",
            run_id="external-v2",
        )

    assert load_task(task_dir).artifacts == task.artifacts


def test_legacy_external_activation_rejects_v4_without_leaving_a_temp_bundle(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import (
        NoteGenerationError,
        build_external_note_bundle,
    )

    task_dir, task = task_workspace(tmp_path)
    external = tmp_path / "v4-note.json"
    external.write_text(
        json.dumps(
            {
                "schema_version": "4.0",
                "task_id": task.task_id,
                "source_fingerprint": task.source_fingerprint,
                "template_id": "concept-explanation",
                "template_sha256": "0" * 64,
                "title": {"text": "V4 笔记", "evidence_ids": ["tr_0001"]},
                "blocks": [
                    {
                        "block_id": "core",
                        "semantic_block": "core_facts",
                        "items": [
                            {
                                "content": {
                                    "text": "证据事实。",
                                    "evidence_ids": ["tr_0001"],
                                }
                            }
                        ],
                    }
                ],
                "ai_supplements": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(NoteGenerationError, match="GeneratedNote 3.0") as captured:
        build_external_note_bundle(task_dir, external, run_id="external-v4")

    assert captured.value.bundle_path.is_dir()
    assert not (task_dir / "generated_notes" / ".external-v4.tmp").exists()
    assert load_task(task_dir).artifacts == task.artifacts


@pytest.mark.parametrize(
    ("cli_value", "expected_source"),
    [("auto", "external"), ("resource", "cli")],
)
def test_external_metadata_records_the_effective_type_source(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
    cli_value: str,
    expected_source: str,
) -> None:
    from learnnest.note_generation import build_external_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    external = tmp_path / f"resource-{cli_value}.json"
    external.write_text(valid_v3_note_json("resource_share"), encoding="utf-8")

    bundle = build_external_note_bundle(
        task_dir,
        external,
        note_type=cli_value,
        run_id=f"external-{cli_value}",
    )

    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["note_type"] == "resource_share"
    assert metadata["note_type_source"] == expected_source


def test_external_metadata_preserves_a_stored_source_request(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import build_external_note_bundle

    task_dir, task = task_workspace(tmp_path)
    write_task_atomic(
        task_dir,
        task.model_copy(update={"note_type_override": "resource_share"}),
    )
    external = tmp_path / "resource-source.json"
    external.write_text(
        valid_v3_note_json(
            "resource_share",
            classification_evidence_ids=[],
        ),
        encoding="utf-8",
    )

    bundle = build_external_note_bundle(
        task_dir,
        external,
        run_id="external-source",
    )

    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["note_type"] == "resource_share"
    assert metadata["note_type_source"] == "source"


def test_generate_note_bundle_succeeds_on_the_first_response(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider([valid_v3_note_json()])
    old_task = (task_dir / "task.json").read_bytes()

    bundle = generate_note_bundle(task_dir, provider, run_id="run-0001")

    assert bundle == task_dir / "generated_notes" / "run-0001"
    assert len(provider.plan_calls) == 1
    assert provider.calls[0][1] == ()
    coverage_plan = bundle / "coverage-plan.json"
    assert (bundle / "coverage-plan-1.raw.txt").read_text(
        encoding="utf-8"
    ) == valid_coverage_plan_json()
    assert (
        json.loads(coverage_plan.read_text(encoding="utf-8"))["units"][0]["label"]
        == "核心结论"
    )
    canonical_plan_json = coverage_plan.read_text(encoding="utf-8").strip()
    scope = bundle / "draft-evidence-scope.json"
    assert json.loads(provider.calls[0][0]) == json.loads(
        scope.read_text(encoding="utf-8")
    )
    assert json.loads(provider.calls[0][3]) == {
        "units": [{"unit_index": 1, "anchor_evidence_ids": ["tr_0001"]}],
        "visuals": [
            {
                "visual_index": 1,
                "disposition": "use",
                "unit_index": 1,
                "frame_evidence_id": "fr_0001",
                "supporting_ocr_evidence_ids": [],
            }
        ],
    }
    assert (bundle / "attempt-1.raw.txt").read_text(
        encoding="utf-8"
    ) == valid_v3_note_json()
    assert (
        json.loads((bundle / "note.json").read_text(encoding="utf-8"))["schema_version"]
        == "3.0"
    )
    assert (bundle / "note.md").read_bytes() == (
        bundle / "published_note.md"
    ).read_bytes()
    assert len(provider.review_calls) == 1
    assert provider.review_calls[0][2] == canonical_plan_json
    candidate = bundle / "review-candidate.json"
    statement_manifest = bundle / "review-statement-manifest.json"
    review = bundle / "review.json"
    citation_packet = bundle / "statement-citation-packet.json"
    citation_audit = bundle / "citation-audit.json"
    assert json.loads(provider.review_calls[0][3]) == json.loads(
        statement_manifest.read_text(encoding="utf-8")
    )
    assert [
        item["candidate_path"]
        for item in json.loads(statement_manifest.read_text(encoding="utf-8"))
    ] == ["/summary", "/concepts/0/explanation"]
    assert json.loads(candidate.read_text(encoding="utf-8"))["schema_version"] == "3.0"
    assert (bundle / "review-1.raw.txt").read_text(
        encoding="utf-8"
    ) == valid_review_json()
    assert json.loads(review.read_text(encoding="utf-8"))["verdict"] == "approve"
    assert provider.events == ["plan", "draft", "citation_audit", "review"]
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "completed"
    assert {
        "provider": "fake",
        "model": "queued",
        "plan_call_count": 1,
        "call_count": 1,
        "status": "approved",
        "coverage_plan_sha256": hashlib.sha256(coverage_plan.read_bytes()).hexdigest(),
        "candidate_note_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "statement_manifest_sha256": hashlib.sha256(
            statement_manifest.read_bytes()
        ).hexdigest(),
        "review_sha256": hashlib.sha256(review.read_bytes()).hexdigest(),
        "errors": [],
    }.items() <= metadata["review"].items()
    assert metadata["review"]["draft_evidence_scope_status"] == "ready"
    assert (
        metadata["review"]["draft_evidence_scope_sha256"]
        == hashlib.sha256(scope.read_bytes()).hexdigest()
    )
    assert metadata["review"]["citation_audit"] == {
        "provider": "fake",
        "model": "queued",
        "call_count": 1,
        "status": "approved",
        "statement_citation_packet_sha256": statement_citation_packet_sha256(
            StatementCitationPacketV12.model_validate_json(
                citation_packet.read_text(encoding="utf-8")
            )
        ),
        "statement_citation_packet_file_sha256": hashlib.sha256(
            citation_packet.read_bytes()
        ).hexdigest(),
        "citation_audit_sha256": hashlib.sha256(
            citation_audit.read_bytes()
        ).hexdigest(),
        "errors": [],
    }
    assert (task_dir / "task.json").read_bytes() == old_task
    assert list((task_dir / "generated_notes").glob(".*.tmp")) == []


def test_generate_note_bundle_persists_scope_packet_and_audit_before_review(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    """A provider-generated bundle carries the two local citation boundaries."""
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider([valid_v3_note_json()])

    bundle = generate_note_bundle(task_dir, provider, run_id="scope-audit-artifacts")

    assert (bundle / "draft-evidence-scope.json").is_file()
    assert (bundle / "statement-citation-packet.json").is_file()
    assert (bundle / "citation-audit-1.raw.txt").is_file()
    assert (bundle / "citation-audit.json").is_file()
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["review"]["draft_evidence_scope_status"] == "ready"
    assert metadata["review"]["citation_audit"]["status"] == "approved"


def test_generation_rejects_citations_outside_draft_scope_before_audit_or_review(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    omitted_visual_plan = json.loads(valid_coverage_plan_json())
    omitted_visual_plan["visuals"][0].update(
        {
            "disposition": "omit",
            "unit_label": None,
            "rationale": "该帧不进入草稿作用域。",
        }
    )
    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json(), valid_v3_note_json(), valid_v3_note_json()],
        plans=[json.dumps(omitted_visual_plan, ensure_ascii=False)],
    )

    with pytest.raises(NoteGenerationError, match="outside draft scope") as captured:
        generate_note_bundle(task_dir, provider, run_id="scope-rejected")

    assert len(provider.calls) == 3
    assert provider.calls[1][1] == (
        "generated note cites evidence outside draft scope; use only allowlisted evidence",
        "candidate cites coverage plan visual 1 marked omit",
        "coverage plan visual 1 must remain omitted",
    )
    assert provider.calls[2][1] == provider.calls[1][1]
    assert all("fr_0001" not in error for error in provider.calls[1][1])
    metadata = json.loads(
        (captured.value.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["errors"] == [
        "generated note cites evidence outside draft scope: fr_0001",
        "candidate cites frame planned for omission: fr_0001",
    ]
    assert provider.citation_calls == []
    assert provider.review_calls == []
    assert not (captured.value.bundle_path / "statement-citation-packet.json").exists()


def test_generation_redacts_omitted_frame_ids_from_all_retry_feedback(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    omitted_visual_plan = json.loads(valid_coverage_plan_json())
    omitted_visual_plan["visuals"][0].update(
        {
            "disposition": "omit",
            "unit_label": None,
        }
    )
    invalid = valid_v3_note_json(
        classification_evidence_ids=["fr_0001", "fr_0001"],
    )
    repaired = valid_v3_note_json(summary_evidence_ids=["tr_0001"])
    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [invalid, repaired],
        plans=[json.dumps(omitted_visual_plan, ensure_ascii=False)],
    )

    generate_note_bundle(task_dir, provider, run_id="redacted-omitted-frame")

    assert len(provider.calls) == 2
    assert all("fr_0001" not in error for error in provider.calls[1][1])
    assert any("coverage plan visual 1" in error for error in provider.calls[1][1])


def test_citation_audit_retries_one_format_failure_with_the_same_packet(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        citation_audits=["not-json", valid_citation_audit_json],
    )

    bundle = generate_note_bundle(task_dir, provider, run_id="citation-format-retry")

    assert (bundle / "citation-audit-1.raw.txt").read_text(
        encoding="utf-8"
    ) == "not-json"
    assert provider.citation_calls[0][0] == provider.citation_calls[1][0]
    assert provider.citation_calls[1][1] == (
        "citation audit response failed schema validation",
    )
    assert "not-json" not in provider.citation_calls[1][0]
    assert provider.events.index("citation_audit") < provider.events.index("review")
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["review"]["citation_audit"]["call_count"] == 2
    assert metadata["review"]["citation_audit"]["status"] == "approved"


def test_citation_audit_retries_one_local_contract_failure_with_the_same_packet(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        citation_audits=[
            locally_invalid_citation_audit_json,
            valid_citation_audit_json,
        ],
    )

    bundle = generate_note_bundle(task_dir, provider, run_id="citation-contract-retry")

    assert (bundle / "citation-audit-1.raw.txt").exists()
    assert provider.citation_calls[0][0] == provider.citation_calls[1][0]
    assert provider.citation_calls[1][1] == (
        "citation audit response failed local contract validation",
    )
    assert "citation-audit-1.raw.txt" not in provider.citation_calls[1][0]
    assert provider.events.index("citation_audit") < provider.events.index("review")
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["review"]["citation_audit"]["call_count"] == 2
    assert metadata["review"]["citation_audit"]["status"] == "approved"


def test_citation_audit_second_format_failure_fails_closed_without_review(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        citation_audits=["not-json", "still-not-json"],
    )

    with pytest.raises(
        NoteGenerationError,
        match="citation audit response failed schema validation",
    ) as captured:
        generate_note_bundle(task_dir, provider, run_id="citation-format-failed")

    bundle = captured.value.bundle_path
    assert provider.review_calls == []
    assert not (bundle / "note.json").exists()
    assert (bundle / "citation-audit-2.raw.txt").read_text(
        encoding="utf-8"
    ) == "still-not-json"
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["review"]["citation_audit"]["status"] == "invalid"


def test_citation_audit_semantic_rejection_keeps_active_note_and_skips_review(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import (
        NoteGenerationError,
        generate_and_activate_note,
    )

    task_dir, original = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        citation_audits=[rejecting_citation_audit_json],
    )

    with pytest.raises(
        NoteGenerationError, match="citation audit rejected"
    ) as captured:
        generate_and_activate_note(task_dir, provider, tmp_path)

    bundle = captured.value.bundle_path
    assert provider.review_calls == []
    assert not (bundle / "note.json").exists()
    assert len(provider.citation_calls) == 1
    assert load_task(task_dir).artifacts["note"] == original.artifacts["note"]
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["review"]["citation_audit"]["status"] == "rejected"


@pytest.mark.parametrize(
    ("citation_audits", "expected_match"),
    [
        (
            [RuntimeError("transport unavailable")],
            "citation auditor failed: RuntimeError",
        ),
        (
            ["not-json", "still-not-json"],
            "citation audit response failed schema validation",
        ),
        (
            [locally_invalid_citation_audit_json, locally_invalid_citation_audit_json],
            "citation audit invalid",
        ),
    ],
    ids=["transport", "second-format", "second-local-contract"],
)
def test_citation_audit_failure_preserves_previous_active_note_and_publish(
    tmp_path: Path,
    citation_audits: list[str | Exception | Callable[[str], str]],
    expected_match: str,
) -> None:
    from learnnest.note_generation import (
        NoteGenerationError,
        generate_and_activate_note,
    )

    task_dir, _ = task_workspace(tmp_path)
    first = generate_and_activate_note(
        task_dir, FakeNoteProvider([valid_v3_note_json()]), tmp_path
    )
    active_note_path = task_dir / first.artifacts["note"][0]
    published_path = tmp_path / "视频学习笔记" / "lesson.md"
    active_note_bytes = active_note_path.read_bytes()
    published_bytes = published_path.read_bytes()
    old_artifacts = first.artifacts

    failing = FakeNoteProvider([valid_v3_note_json()], citation_audits=citation_audits)
    with pytest.raises(NoteGenerationError, match=expected_match):
        generate_and_activate_note(task_dir, failing, tmp_path)

    current = load_task(task_dir)
    assert current.artifacts == old_artifacts
    assert active_note_path.read_bytes() == active_note_bytes
    assert published_path.read_bytes() == published_bytes
    assert current.attempts[-1].status == "failed"
    assert current.attempts[-1].failed_stage == "note"


def test_citation_audit_second_local_contract_failure_fails_closed_without_review(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    def wrong_clause_id(packet_json: str) -> str:
        payload = json.loads(valid_citation_audit_json(packet_json))
        clauses = payload["clauses"]
        assert isinstance(clauses, list)
        first = clauses[0]
        assert isinstance(first, dict)
        first["clause_id"] = "clause-9999-0001"
        return json.dumps(payload, ensure_ascii=False)

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        citation_audits=[wrong_clause_id, wrong_clause_id],
    )

    with pytest.raises(NoteGenerationError, match="citation audit invalid") as captured:
        generate_note_bundle(task_dir, provider, run_id="citation-local-invalid")

    assert provider.review_calls == []
    assert len(provider.citation_calls) == 2
    assert provider.citation_calls[1][1] == (
        "citation audit response failed local contract validation",
    )
    assert "clause-9999-0001" not in provider.citation_calls[1][0]
    metadata = json.loads(
        (captured.value.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["review"]["citation_audit"]["status"] == "invalid"


def test_citation_audit_invalid_local_packet_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import learnnest.note_generation as note_generation
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    original_build_packet = note_generation.build_statement_citation_packet

    def corrupted_packet(*args: object, **kwargs: object) -> StatementCitationPacketV12:
        packet = original_build_packet(*args, **kwargs)
        first = packet.statements[0].model_copy(update={"statement_sha256": "f" * 64})
        return packet.model_copy(update={"statements": [first, *packet.statements[1:]]})

    monkeypatch.setattr(
        note_generation,
        "build_statement_citation_packet",
        corrupted_packet,
    )
    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider([valid_v3_note_json()])

    with pytest.raises(NoteGenerationError, match="citation audit invalid") as captured:
        generate_note_bundle(task_dir, provider, run_id="citation-invalid-packet")

    assert len(provider.citation_calls) == 1
    assert provider.review_calls == []
    metadata = json.loads(
        (captured.value.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["review"]["citation_audit"]["status"] == "invalid"


@pytest.mark.parametrize(
    ("plan_responses", "expected", "expected_plan_calls"),
    [
        (
            ["not-json", "not-json"],
            "coverage plan response failed schema validation",
            2,
        ),
        (
            [RuntimeError("planner transport failed")],
            "coverage planner failed: RuntimeError",
            1,
        ),
    ],
)
def test_generate_note_bundle_fails_before_draft_on_invalid_or_failed_plan(
    tmp_path: Path,
    plan_responses: list[str | Exception],
    expected: str,
    expected_plan_calls: int,
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        plans=plan_responses,
    )

    with pytest.raises(NoteGenerationError, match=expected) as captured:
        generate_note_bundle(task_dir, provider, run_id="coverage-plan-failed")

    bundle = captured.value.bundle_path
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["attempt_count"] == 0
    assert metadata["review"]["plan_call_count"] == expected_plan_calls
    assert metadata["review"]["call_count"] == 0
    assert provider.calls == []
    assert provider.review_calls == []
    assert not (bundle / "note.json").exists()


def test_generate_note_bundle_retries_once_with_corrective_plan_feedback(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        plans=["not-json", valid_coverage_plan_json()],
    )

    bundle = generate_note_bundle(task_dir, provider, run_id="coverage-plan-retry")

    assert len(provider.plan_calls) == 2
    assert provider.plan_retries[0] == ()
    retry_feedback = provider.plan_retries[1]
    assert len(retry_feedback) == 1
    assert retry_feedback[0].startswith(
        "coverage plan response failed schema validation"
    )
    assert "never the JSON Schema" in retry_feedback[0]
    assert "audit all visual entries" in retry_feedback[0].lower()
    assert (bundle / "coverage-plan-1.raw.txt").read_text(
        encoding="utf-8"
    ) == "not-json"
    assert (bundle / "coverage-plan-2.raw.txt").read_text(
        encoding="utf-8"
    ) == valid_coverage_plan_json()
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["review"]["plan_call_count"] == 2


def test_generate_note_bundle_does_not_replay_a_json_schema_as_previous_plan(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    schema_echo = json.dumps(
        {
            "$defs": {"CoveragePlan": {"type": "object"}},
            "oneOf": [],
            "properties": {"schema_version": {"const": "1.1"}},
        },
        ensure_ascii=False,
    )
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        plans=[schema_echo, valid_coverage_plan_json()],
    )

    generate_note_bundle(task_dir, provider, run_id="coverage-plan-schema-echo")

    assert provider.plan_retries[1][0].startswith(
        "coverage plan response failed schema validation"
    )


def test_generate_note_bundle_does_not_replay_a_schema_without_defs(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    schema_echo = json.dumps(
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
        ensure_ascii=False,
    )
    provider = FakeNoteProvider([schema_echo, valid_v3_note_json()])

    generate_note_bundle(task_dir, provider, run_id="schema-without-defs")

    assert provider.calls[1][0] == provider.calls[0][0]
    assert provider.calls[1][3] == provider.calls[0][3]
    assert len(provider.calls[1]) == 4


def test_generate_note_bundle_retries_plan_with_specific_structural_feedback(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    invalid_plan = json.loads(valid_coverage_plan_json())
    invalid_plan["units"][0]["source_evidence_ids"] = [
        "tr_0001",
        "tr_0002",
        "tr_0003",
        "tr_0004",
    ]
    invalid_plan["visuals"][0].update(
        {
            "supporting_ocr_evidence_ids": ["ocr_0001"],
            "disposition": "omit",
            "unit_label": None,
        }
    )
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        plans=[
            json.dumps(invalid_plan, ensure_ascii=False),
            valid_coverage_plan_json(),
        ],
    )

    generate_note_bundle(task_dir, provider, run_id="coverage-plan-specific-feedback")

    retry_feedback = provider.plan_retries[1]
    assert json.dumps(invalid_plan, ensure_ascii=False) not in "\n".join(retry_feedback)
    assert retry_feedback[1:] == (
        "coverage plan unit 1 source_evidence_ids must contain one to three IDs",
        "coverage plan visual 1 marked omit must have an empty supporting_ocr_evidence_ids list",
    )


def test_generate_note_bundle_retries_plan_identity_mismatch_without_raw_feedback(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    invalid_plan = json.loads(valid_coverage_plan_json())
    invalid_plan["source_fingerprint"] = "stale-source-fingerprint-sentinel"
    raw = json.dumps(invalid_plan, ensure_ascii=False)
    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        plans=[raw, valid_coverage_plan_json()],
    )

    generate_note_bundle(task_dir, provider, run_id="coverage-plan-identity-retry")

    assert provider.plan_retries[1] == (
        "coverage plan failed local source-only consistency checks. Return a fresh "
        "complete CoveragePlan 1.1 JSON object from the supplied content pack. Copy "
        "task_id and source_fingerprint verbatim from it. Do not reuse any prior "
        "coverage plan response.",
    )
    assert "stale-source-fingerprint-sentinel" not in "\n".join(
        provider.plan_retries[1]
    )
    assert raw not in "\n".join(provider.plan_retries[1])


def test_generate_note_bundle_fails_when_review_changes_frozen_plan(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    review_payload = json.loads(valid_review_json())
    review_payload["coverage_units"][0]["label"] = "被评审改写的标签"
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        reviews=[json.dumps(review_payload, ensure_ascii=False)],
    )
    task_dir, _ = task_workspace(tmp_path)

    with pytest.raises(
        NoteGenerationError,
        match="coverage unit 1 label does not match plan",
    ) as captured:
        generate_note_bundle(task_dir, provider, run_id="review-plan-mismatch")

    metadata = json.loads(
        (captured.value.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["review"]["status"] == "invalid"
    assert metadata["review"]["plan_call_count"] == 1
    assert metadata["review"]["call_count"] == 1


def test_generate_note_bundle_fails_closed_when_quality_review_rejects(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json()], reviews=[valid_review_json(verdict="reject")]
    )

    with pytest.raises(
        NoteGenerationError, match="quality review rejected"
    ) as captured:
        generate_note_bundle(task_dir, provider, run_id="review-rejected")

    bundle = captured.value.bundle_path
    assert (bundle / "review-candidate.json").is_file()
    assert (bundle / "review-1.raw.txt").is_file()
    assert (bundle / "review.json").is_file()
    assert not (bundle / "note.json").exists()
    assert not (bundle / "note.md").exists()
    assert not (bundle / "published_note.md").exists()
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert metadata["errors"] == ["quality review rejected: missing"]
    assert metadata["review"]["status"] == "rejected"


def test_review_rejection_reports_statement_evidence_misalignment(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    review = json.loads(valid_review_json())
    review["verdict"] = "reject"
    review["statement_audits"][0]["status"] = "misaligned"
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        reviews=[json.dumps(review, ensure_ascii=False)],
    )
    task_dir, _ = task_workspace(tmp_path)

    with pytest.raises(
        NoteGenerationError,
        match="quality review rejected: evidence_misalignment",
    ) as captured:
        generate_note_bundle(task_dir, provider, run_id="statement-audit-rejected")

    metadata = json.loads(
        (captured.value.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["errors"] == ["quality review rejected: evidence_misalignment"]


def test_generate_note_bundle_retries_one_invalid_review_format_without_repairing_candidate(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        reviews=["not-json", valid_review_json()],
    )

    bundle = generate_note_bundle(task_dir, provider, run_id="review-format-retry")

    assert len(provider.review_calls) == 2
    assert provider.review_retries == [
        (),
        ("quality review response failed schema validation",),
    ]
    assert (bundle / "review-1.raw.txt").read_text(encoding="utf-8") == "not-json"
    assert (bundle / "review-2.raw.txt").read_text(
        encoding="utf-8"
    ) == valid_review_json()
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["review"]["call_count"] == 2


def test_generate_note_bundle_retries_a_duplicate_review_object_as_format_failure(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    duplicate = valid_review_json() + "\n```json\n" + valid_review_json() + "\n```"
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        reviews=[duplicate, valid_review_json()],
    )

    bundle = generate_note_bundle(task_dir, provider, run_id="duplicate-review-retry")

    assert (bundle / "review-1.raw.txt").read_text(encoding="utf-8") == duplicate
    assert provider.review_retries[1] == (
        "quality review response failed schema validation",
    )
    assert len(provider.review_calls) == 2
    assert provider.review_calls[0] == provider.review_calls[1]
    assert len(provider.calls) == 1
    assert (bundle / "review-2.raw.txt").read_text(
        encoding="utf-8"
    ) == valid_review_json()
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["review"]["call_count"] == 2
    assert (bundle / "review.json").is_file()


def test_generate_note_bundle_fails_closed_after_two_duplicate_review_objects(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    duplicate = valid_review_json() + "\n```json\n" + valid_review_json() + "\n```"
    provider = FakeNoteProvider(
        [valid_v3_note_json()],
        reviews=[duplicate, duplicate],
    )

    with pytest.raises(
        NoteGenerationError, match="quality review response failed schema validation"
    ) as captured:
        generate_note_bundle(task_dir, provider, run_id="duplicate-review-failed")

    bundle = captured.value.bundle_path
    assert len(provider.review_calls) == 2
    assert not (bundle / "note.json").exists()
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["review"]["call_count"] == 2
    assert metadata["review"]["status"] == "invalid"


@pytest.mark.parametrize(
    ("review_responses", "expected", "expected_calls"),
    [
        (
            ["not-json", "not-json"],
            "quality review response failed schema validation",
            2,
        ),
        ([RuntimeError("review transport failed")], "reviewer failed: RuntimeError", 1),
    ],
)
def test_generate_note_bundle_fails_closed_on_invalid_or_failed_review(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
    review_responses: list[str | Exception],
    expected: str,
    expected_calls: int,
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider([valid_v3_note_json()], reviews=review_responses)

    with pytest.raises(NoteGenerationError, match=expected) as captured:
        generate_note_bundle(task_dir, provider, run_id="review-failed")

    bundle = captured.value.bundle_path
    assert (bundle / "review-candidate.json").is_file()
    assert not (bundle / "note.json").exists()
    assert len(provider.review_calls) == expected_calls
    assert (
        json.loads((bundle / "generation.json").read_text(encoding="utf-8"))["status"]
        == "failed"
    )


def test_note_generation_rejects_task_content_pack_identity_mismatch_before_provider(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, task = task_workspace(tmp_path)
    write_task_atomic(
        task_dir,
        task.model_copy(update={"source_fingerprint": "tampered"}),
    )
    provider = FakeNoteProvider([valid_v3_note_json()])

    with pytest.raises(ValueError, match="content pack source_fingerprint"):
        generate_note_bundle(task_dir, provider)

    assert provider.calls == []


def test_generate_note_bundle_retries_once_with_validation_feedback(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(["not-json", valid_v3_note_json()])

    bundle = generate_note_bundle(task_dir, provider, run_id="run-0002")

    assert len(provider.calls) == 2
    assert len(provider.plan_calls) == 1
    assert provider.calls[0][1] == ()
    assert provider.calls[1][1]
    assert provider.calls[0][0] == provider.calls[1][0]
    assert provider.calls[0][3] == provider.calls[1][3]
    assert len(provider.calls[1]) == 4
    assert (bundle / "attempt-1.raw.txt").read_text(encoding="utf-8") == "not-json"
    assert (bundle / "attempt-2.raw.txt").read_text(
        encoding="utf-8"
    ) == valid_v3_note_json()
    assert len(provider.review_calls) == 1
    assert json.loads(provider.review_calls[0][2]) == json.loads(
        valid_coverage_plan_json()
    )


def test_generate_note_bundle_does_not_replay_a_json_schema_as_previous_note(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    schema_echo = json.dumps(
        {
            "$defs": {"GeneratedNote": {"type": "object"}},
            "oneOf": [],
            "properties": {"schema_version": {"const": "3.0"}},
        },
        ensure_ascii=False,
    )
    provider = FakeNoteProvider([schema_echo, valid_v3_note_json()])

    bundle = generate_note_bundle(task_dir, provider, run_id="schema-echo-retry")

    assert (bundle / "attempt-1.raw.txt").read_text(encoding="utf-8") == schema_echo
    assert provider.calls[1][1] == (
        "generated note response failed schema validation. Return exactly one "
        "complete GeneratedNote 3.0 JSON object with no text before or after it. "
        "Do not return a legacy note, JSON Schema, field definitions, $schema, "
        "or other unrecognized top-level fields. "
        "classification_evidence_ids must contain at most 12 distinct evidence "
        "IDs from the supplied draft evidence scope.",
    )
    assert provider.calls[1][0] == provider.calls[0][0]
    assert provider.calls[1][3] == provider.calls[0][3]
    assert schema_echo not in provider.calls[1][0]
    assert schema_echo not in provider.calls[1][3]


def test_generate_note_bundle_retries_a_schema_metadata_field_without_replaying_it(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    metadata_note = json.loads(valid_v3_note_json())
    metadata_note["$schema"] = "https://example.invalid/generated-note.json"
    invalid = json.dumps(metadata_note, ensure_ascii=False)
    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider([invalid, valid_v3_note_json()])

    bundle = generate_note_bundle(task_dir, provider, run_id="schema-metadata-retry")

    assert bundle.name == "schema-metadata-retry"
    assert len(provider.calls) == 2
    assert "$schema" in provider.calls[1][1][0]
    assert invalid not in provider.calls[1][0]
    assert invalid not in provider.calls[1][3]


def test_generate_note_bundle_retries_once_with_stable_semantic_feedback(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    invalid = valid_v3_note_json(
        summary_evidence_ids=["tr_0001", "unknown_0001", "fr_0001"]
    )
    valid = valid_v3_note_json()
    provider = FakeNoteProvider([invalid, valid])

    bundle = generate_note_bundle(task_dir, provider, run_id="run-quality-retry")

    assert bundle.name == "run-quality-retry"
    assert len(provider.calls) == 2
    assert provider.calls[1][1] == (
        "generated note references unknown evidence id: unknown_0001",
    )
    assert provider.calls[1][0] == provider.calls[0][0]
    assert provider.calls[0][0] not in provider.calls[1][1][0]
    assert provider.calls[1][3] == provider.calls[0][3]
    assert invalid not in provider.calls[1][0]
    assert invalid not in provider.calls[1][3]
    assert len(provider.review_calls) == 1


def test_generate_note_bundle_retries_when_candidate_drops_plan_evidence(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    plan = json.loads(valid_coverage_plan_json())
    plan["units"][0]["source_evidence_ids"] = ["tr_0001", "ocr_0001"]
    plan["visuals"][0]["supporting_ocr_evidence_ids"] = ["ocr_0001"]
    review = json.loads(valid_review_json())
    review["coverage_units"][0]["source_evidence_ids"] = ["tr_0001", "ocr_0001"]
    provider = FakeNoteProvider(
        [
            valid_v3_note_json(),
            valid_v3_note_json(summary_evidence_ids=["tr_0001", "ocr_0001"]),
        ],
        plans=[json.dumps(plan, ensure_ascii=False)],
        reviews=[json.dumps(review, ensure_ascii=False)],
    )
    task_dir, _ = task_workspace(tmp_path)

    generate_note_bundle(task_dir, provider, run_id="plan-evidence-retry")

    assert len(provider.calls) == 2
    assert provider.calls[1][1][:2] == (
        "candidate does not cite coverage unit source evidence together: 1",
        "candidate does not cite planned visual evidence together: fr_0001",
    )
    assert (
        "coverage plan unit 1 requires all anchors together: tr_0001, ocr_0001"
        in (provider.calls[1][1])
    )
    assert any(
        "coverage plan visual fr_0001 requires its frame and supporting OCR together "
        "at its first factual statement, plus one anchor from unit 1: tr_0001, ocr_0001"
        in item
        for item in provider.calls[1][1]
    )
    assert provider.calls[1][0] == provider.calls[0][0]
    assert provider.calls[1][3] == provider.calls[0][3]
    assert valid_v3_note_json() not in provider.calls[1][0]
    assert valid_v3_note_json() not in provider.calls[1][3]
    assert len(provider.review_calls) == 1


def test_coverage_plan_retry_feedback_describes_omitted_visual() -> None:
    from learnnest.note_coverage import parse_coverage_plan
    from learnnest.note_generation import _coverage_plan_retry_feedback

    plan_payload = json.loads(valid_coverage_plan_json())
    plan_payload["visuals"][0].update(
        {
            "disposition": "omit",
            "unit_label": None,
        }
    )
    plan = parse_coverage_plan(json.dumps(plan_payload, ensure_ascii=False))

    assert _coverage_plan_retry_feedback(
        ("candidate cites frame planned for omission: fr_0001",),
        plan,
    ) == ("coverage plan visual 1 must remain omitted",)


def test_draft_retry_feedback_errors_redacts_only_omitted_frame_tokens() -> None:
    from learnnest.note_coverage import parse_coverage_plan
    from learnnest.note_generation import _draft_retry_feedback_errors

    plan_payload = json.loads(valid_coverage_plan_json())
    plan_payload["visuals"][0].update(
        {
            "disposition": "omit",
            "unit_label": None,
        }
    )
    plan = parse_coverage_plan(json.dumps(plan_payload, ensure_ascii=False))

    assert _draft_retry_feedback_errors(
        (
            "generated note classification_evidence_ids contains duplicate evidence "
            "id: fr_0001 and fr_00010",
        ),
        plan,
    ) == (
        "generated note classification_evidence_ids contains duplicate evidence "
        "id: coverage plan visual 1 and fr_00010",
    )


def test_generate_note_bundle_keeps_prior_frozen_plan_requirements_on_final_repair(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    pack = ContentPack.model_validate_json(
        (task_dir / "content_pack.json").read_text(encoding="utf-8")
    )
    expanded_pack = ContentPack.model_validate(
        {
            **pack.model_dump(),
            "evidence": [
                *(item.model_dump() for item in pack.evidence),
                Evidence(
                    id="tr_0002",
                    kind="transcript",
                    start_ms=1_000,
                    end_ms=2_000,
                    text="保存配置。",
                    artifact_path="transcript.json",
                ).model_dump(),
            ],
        }
    )
    (task_dir / "content_pack.json").write_text(
        expanded_pack.model_dump_json(indent=2),
        encoding="utf-8",
    )

    plan = json.loads(valid_coverage_plan_json())
    plan["units"][0].update(
        {
            "label": "第一结论",
            "source_evidence_ids": ["tr_0001"],
        }
    )
    plan["units"].append(
        {
            "label": "第二结论",
            "source_evidence_ids": ["tr_0002"],
            "rationale": "第二段需要保留。",
        }
    )
    plan["visuals"][0].update(
        {
            "disposition": "omit",
            "unit_label": None,
        }
    )
    review = json.loads(valid_review_json())
    review["coverage_units"] = [
        {
            "label": "第一结论",
            "source_evidence_ids": ["tr_0001"],
            "candidate_paths": ["/summary"],
            "status": "covered",
            "rationale": "摘要覆盖第一结论。",
        },
        {
            "label": "第二结论",
            "source_evidence_ids": ["tr_0002"],
            "candidate_paths": ["/summary"],
            "status": "covered",
            "rationale": "摘要覆盖第二结论。",
        },
    ]
    missing_second = valid_v3_note_json(summary_evidence_ids=["tr_0001"])
    missing_first_payload = json.loads(
        valid_v3_note_json(summary_evidence_ids=["tr_0002"])
    )
    missing_first_payload["concepts"][0]["explanation"]["evidence_ids"] = ["tr_0002"]
    missing_first = json.dumps(missing_first_payload, ensure_ascii=False)
    repaired = valid_v3_note_json(summary_evidence_ids=["tr_0001", "tr_0002"])
    provider = FakeNoteProvider(
        [missing_second, missing_first, repaired],
        plans=[json.dumps(plan, ensure_ascii=False)],
        reviews=[json.dumps(review, ensure_ascii=False)],
    )

    generate_note_bundle(task_dir, provider, run_id="cumulative-plan-repair")

    third_feedback = provider.calls[2][1]
    assert "candidate does not cite coverage unit source evidence together: 1" in (
        third_feedback
    )
    assert (
        "coverage plan unit 1 requires all anchors together: tr_0001" in third_feedback
    )
    assert (
        "coverage plan unit 2 requires all anchors together: tr_0002" in third_feedback
    )
    assert "candidate does not cite coverage unit source evidence together: 2" not in (
        third_feedback
    )
    assert provider.calls[2][0] == provider.calls[0][0]
    assert provider.calls[2][3] == provider.calls[0][3]
    assert missing_second not in provider.calls[2][0]
    assert missing_first not in provider.calls[2][3]


def test_generate_note_bundle_allows_one_final_semantic_plan_repair(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    plan = json.loads(valid_coverage_plan_json())
    plan["units"][0]["source_evidence_ids"] = ["tr_0001", "ocr_0001"]
    plan["visuals"][0]["supporting_ocr_evidence_ids"] = ["ocr_0001"]
    review = json.loads(valid_review_json())
    review["coverage_units"][0]["source_evidence_ids"] = ["tr_0001", "ocr_0001"]
    invalid = valid_v3_note_json()
    repaired = valid_v3_note_json(summary_evidence_ids=["tr_0001", "ocr_0001"])
    provider = FakeNoteProvider(
        [invalid, invalid, repaired],
        plans=[json.dumps(plan, ensure_ascii=False)],
        reviews=[json.dumps(review, ensure_ascii=False)],
    )
    task_dir, _ = task_workspace(tmp_path)

    bundle = generate_note_bundle(task_dir, provider, run_id="final-semantic-repair")

    assert len(provider.calls) == 3
    assert provider.calls[2][1][:2] == (
        "candidate does not cite coverage unit source evidence together: 1",
        "candidate does not cite planned visual evidence together: fr_0001",
    )
    assert provider.calls[2][0] == provider.calls[0][0]
    assert provider.calls[2][3] == provider.calls[0][3]
    assert (
        provider.calls[2][1].count(
            "coverage plan unit 1 requires all anchors together: tr_0001, ocr_0001"
        )
        == 1
    )
    assert (
        sum(
            item.startswith("coverage plan visual fr_0001 requires")
            for item in provider.calls[2][1]
        )
        == 1
    )
    assert invalid not in provider.calls[2][0]
    assert invalid not in provider.calls[2][3]
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["attempt_count"] == 3
    assert len(provider.review_calls) == 1


def test_generate_note_bundle_uses_final_plan_repair_after_schema_echo(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    plan = json.loads(valid_coverage_plan_json())
    plan["units"][0]["source_evidence_ids"] = ["tr_0001", "ocr_0001"]
    plan["visuals"][0]["supporting_ocr_evidence_ids"] = ["ocr_0001"]
    review = json.loads(valid_review_json())
    review["coverage_units"][0]["source_evidence_ids"] = ["tr_0001", "ocr_0001"]
    invalid = valid_v3_note_json()
    repaired = valid_v3_note_json(summary_evidence_ids=["tr_0001", "ocr_0001"])
    provider = FakeNoteProvider(
        [invalid, "not-json", repaired],
        plans=[json.dumps(plan, ensure_ascii=False)],
        reviews=[json.dumps(review, ensure_ascii=False)],
    )
    task_dir, _ = task_workspace(tmp_path)

    bundle = generate_note_bundle(task_dir, provider, run_id="schema-final-plan-repair")

    assert bundle.name == "schema-final-plan-repair"
    assert len(provider.calls) == 3
    assert provider.calls[2][1][0].startswith(
        "generated note response failed schema validation."
    )
    assert (
        "coverage plan unit 1 requires all anchors together: tr_0001, ocr_0001"
        in provider.calls[2][1]
    )
    assert any(
        item.startswith("coverage plan visual fr_0001 requires")
        for item in provider.calls[2][1]
    )
    assert provider.calls[2][0] == provider.calls[0][0]
    assert provider.calls[2][3] == provider.calls[0][3]
    assert invalid not in provider.calls[2][0]
    assert invalid not in provider.calls[2][3]
    assert len(provider.review_calls) == 1


def test_generate_note_bundle_keeps_schema_format_requirement_after_frozen_plan_failure(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    plan = json.loads(valid_coverage_plan_json())
    plan["units"][0]["source_evidence_ids"] = ["tr_0001", "ocr_0001"]
    plan["visuals"][0]["supporting_ocr_evidence_ids"] = ["ocr_0001"]
    review = json.loads(valid_review_json())
    review["coverage_units"][0]["source_evidence_ids"] = ["tr_0001", "ocr_0001"]
    frozen_plan_failure = valid_v3_note_json()
    repaired = valid_v3_note_json(summary_evidence_ids=["tr_0001", "ocr_0001"])
    provider = FakeNoteProvider(
        ["not-json", frozen_plan_failure, repaired],
        plans=[json.dumps(plan, ensure_ascii=False)],
        reviews=[json.dumps(review, ensure_ascii=False)],
    )
    task_dir, _ = task_workspace(tmp_path)

    bundle = generate_note_bundle(task_dir, provider, run_id="persistent-format")

    assert bundle.name == "persistent-format"
    assert len(provider.calls) == 3
    schema_feedback = provider.calls[1][1][0]
    assert schema_feedback.startswith(
        "generated note response failed schema validation."
    )
    assert "at most 12 distinct evidence IDs" in schema_feedback
    assert (
        "coverage plan unit 1 requires all anchors together: tr_0001, ocr_0001"
        in provider.calls[2][1]
    )
    assert any(
        item.startswith("coverage plan visual fr_0001 requires")
        for item in provider.calls[2][1]
    )
    assert schema_feedback in provider.calls[2][1]
    assert all(call[0] == provider.calls[0][0] for call in provider.calls)
    assert all(call[3] == provider.calls[0][3] for call in provider.calls)
    assert len(provider.review_calls) == 1


def test_provider_normalizes_v3_ocr_parent_before_first_validation(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    raw = valid_v3_note_json(summary_evidence_ids=["tr_0001", "ocr_0001"])
    plan = json.loads(valid_coverage_plan_json())
    plan["visuals"][0]["supporting_ocr_evidence_ids"] = ["ocr_0001"]
    provider = FakeNoteProvider([raw], plans=[json.dumps(plan, ensure_ascii=False)])
    old_task = (task_dir / "task.json").read_bytes()

    bundle = generate_note_bundle(
        task_dir,
        provider,
        run_id="normalized-first-response",
    )

    assert len(provider.calls) == 1
    assert (bundle / "attempt-1.raw.txt").read_text(encoding="utf-8") == raw
    note = json.loads((bundle / "note.json").read_text(encoding="utf-8"))
    assert note["summary"]["evidence_ids"] == [
        "tr_0001",
        "ocr_0001",
        "fr_0001",
    ]
    reviewed = json.loads(provider.review_calls[0][1])
    assert reviewed["summary"]["evidence_ids"] == [
        "tr_0001",
        "ocr_0001",
        "fr_0001",
    ]
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["attempt_count"] == 1
    assert metadata["evidence_normalizations"] == [
        {
            "attempt": 1,
            "kind": "v3_factual_ocr_parent",
            "factual_statement_index": 1,
            "trigger_ocr_evidence_ids": ["ocr_0001"],
            "added_parent_frame_id": "fr_0001",
        }
    ]
    assert (task_dir / "task.json").read_bytes() == old_task


def test_v3_ocr_parent_normalization_is_grouped_ordered_and_idempotent(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import _normalize_provider_v3_ocr_parents
    from learnnest.note_validation import parse_generated_note

    task_dir, _ = task_workspace(tmp_path)
    pack = ContentPack.model_validate_json(
        (task_dir / "content_pack.json").read_text(encoding="utf-8")
    )
    expanded_pack = ContentPack.model_validate(
        {
            **pack.model_dump(),
            "evidence": [
                *(item.model_dump() for item in pack.evidence),
                Evidence(
                    id="ocr_0002",
                    kind="ocr",
                    text="第二段模型配置",
                    artifact_path="ocr.json",
                    frame_id="fr_0001",
                ).model_dump(),
                Evidence(
                    id="fr_0002",
                    kind="frame",
                    start_ms=750,
                    artifact_path="frames/selected/fr_0002.png",
                    related_evidence_ids=["ocr_0003"],
                ).model_dump(),
                Evidence(
                    id="ocr_0003",
                    kind="ocr",
                    text="另一个画面",
                    artifact_path="ocr.json",
                    frame_id="fr_0002",
                ).model_dump(),
            ],
        }
    )
    raw = valid_v3_note_json(
        summary_evidence_ids=[
            "ocr_0002",
            "ocr_0001",
            "ocr_0002",
            "ocr_0003",
        ]
    )
    original = parse_generated_note(raw)
    original_json = original.model_dump_json()

    normalized, records = _normalize_provider_v3_ocr_parents(
        original,
        expanded_pack,
    )

    assert original.model_dump_json() == original_json
    assert normalized.summary.evidence_ids == [
        "ocr_0002",
        "ocr_0001",
        "ocr_0002",
        "ocr_0003",
        "fr_0001",
        "fr_0002",
    ]
    assert records == [
        {
            "kind": "v3_factual_ocr_parent",
            "factual_statement_index": 1,
            "trigger_ocr_evidence_ids": ["ocr_0002", "ocr_0001"],
            "added_parent_frame_id": "fr_0001",
        },
        {
            "kind": "v3_factual_ocr_parent",
            "factual_statement_index": 1,
            "trigger_ocr_evidence_ids": ["ocr_0003"],
            "added_parent_frame_id": "fr_0002",
        },
    ]

    normalized_again, second_records = _normalize_provider_v3_ocr_parents(
        normalized,
        expanded_pack,
    )

    assert normalized_again == normalized
    assert second_records == []


def test_v3_ocr_parent_normalization_ignores_classification_locator_and_v2(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import _normalize_provider_v3_ocr_parents
    from learnnest.note_validation import parse_generated_note

    task_dir, _ = task_workspace(tmp_path)
    pack = ContentPack.model_validate_json(
        (task_dir / "content_pack.json").read_text(encoding="utf-8")
    )
    payload = json.loads(
        valid_v3_note_json(
            "resource_share",
            classification_evidence_ids=["ocr_0001"],
        )
    )
    payload["resources"][0]["locator"] = {
        "url": "https://example.com/resource",
        "evidence_ids": ["ocr_0001"],
    }
    v3 = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    normalized_v3, v3_records = _normalize_provider_v3_ocr_parents(v3, pack)

    assert normalized_v3.model_dump() == v3.model_dump()
    assert normalized_v3.classification_evidence_ids == ["ocr_0001"]
    assert normalized_v3.resources[0].locator.evidence_ids == ["ocr_0001"]
    assert v3_records == []

    v2 = parse_generated_note(valid_note_json())
    normalized_v2, v2_records = _normalize_provider_v3_ocr_parents(v2, pack)

    assert normalized_v2 is v2
    assert v2_records == []


def test_provider_retries_only_normalization_remaining_errors(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    invalid = valid_v3_note_json(
        summary_evidence_ids=["tr_0001", "ocr_0001", "unknown_0001"]
    )
    plan = json.loads(valid_coverage_plan_json())
    plan["visuals"][0]["supporting_ocr_evidence_ids"] = ["ocr_0001"]
    provider = FakeNoteProvider(
        [
            invalid,
            valid_v3_note_json(summary_evidence_ids=["tr_0001", "ocr_0001", "fr_0001"]),
        ],
        plans=[json.dumps(plan, ensure_ascii=False)],
    )

    bundle = generate_note_bundle(
        task_dir,
        provider,
        run_id="normalization-remaining-retry",
    )

    assert len(provider.calls) == 2
    assert len(provider.review_calls) == 1
    assert provider.calls[1][1] == (
        "generated note references unknown evidence id: unknown_0001",
    )
    assert all("without parent frame" not in error for error in provider.calls[1][1])
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["evidence_normalizations"] == [
        {
            "attempt": 1,
            "kind": "v3_factual_ocr_parent",
            "factual_statement_index": 1,
            "trigger_ocr_evidence_ids": ["ocr_0001"],
            "added_parent_frame_id": "fr_0001",
        }
    ]


def test_external_validation_does_not_normalize_v3_ocr_parent(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import validate_external_note

    task_dir, _ = task_workspace(tmp_path)
    external = tmp_path / "external-ocr-only.json"
    external.write_text(
        valid_v3_note_json(summary_evidence_ids=["ocr_0001"]),
        encoding="utf-8",
    )

    errors = validate_external_note(task_dir, external)

    assert errors == [
        "generated note factual statement 1 cites OCR evidence "
        "ocr_0001 without parent frame fr_0001"
    ]
    assert not (task_dir / "generated_notes").exists()


def test_external_build_does_not_normalize_v3_ocr_parent(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import (
        NoteGenerationError,
        build_external_note_bundle,
    )

    task_dir, _ = task_workspace(tmp_path)
    external = tmp_path / "external-build-ocr-only.json"
    raw = valid_v3_note_json(summary_evidence_ids=["ocr_0001"])
    external.write_text(raw, encoding="utf-8")

    with pytest.raises(NoteGenerationError) as captured:
        build_external_note_bundle(
            task_dir,
            external,
            run_id="external-build-no-normalization",
        )

    bundle = captured.value.bundle_path
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["errors"] == [
        "generated note factual statement 1 cites OCR evidence "
        "ocr_0001 without parent frame fr_0001"
    ]
    assert "evidence_normalizations" not in metadata
    assert (bundle / "attempt-1.raw.txt").read_text(encoding="utf-8") == raw
    assert not (bundle / "note.json").exists()


def test_failed_bundle_records_normalizations_from_every_attempt(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    invalid = valid_v3_note_json(
        summary_evidence_ids=["tr_0001", "ocr_0001", "unknown_0001"]
    )
    plan = json.loads(valid_coverage_plan_json())
    plan["visuals"][0]["supporting_ocr_evidence_ids"] = ["ocr_0001"]
    provider = FakeNoteProvider(
        [invalid, invalid],
        plans=[json.dumps(plan, ensure_ascii=False)],
    )

    with pytest.raises(NoteGenerationError) as captured:
        generate_note_bundle(
            task_dir,
            provider,
            run_id="normalization-failed",
        )

    bundle = captured.value.bundle_path
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["errors"] == [
        "generated note references unknown evidence id: unknown_0001"
    ]
    assert [item["attempt"] for item in metadata["evidence_normalizations"]] == [
        1,
        2,
    ]
    assert not (bundle / "note.json").exists()


def test_second_provider_exception_keeps_first_attempt_normalization(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    invalid = valid_v3_note_json(summary_evidence_ids=["ocr_0001", "unknown_0001"])
    provider = FakeNoteProvider([invalid, RuntimeError("network unavailable")])

    with pytest.raises(NoteGenerationError) as captured:
        generate_note_bundle(
            task_dir,
            provider,
            run_id="normalization-provider-error",
        )

    bundle = captured.value.bundle_path
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["attempt_count"] == 2
    assert metadata["errors"] == ["provider failed: RuntimeError"]
    assert [item["attempt"] for item in metadata["evidence_normalizations"]] == [1]
    assert not (bundle / "note.json").exists()


def test_markdown_failure_metadata_keeps_provider_normalization(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import learnnest.note_generation as note_generation

    task_dir, _ = task_workspace(tmp_path)
    monkeypatch.setattr(
        note_generation,
        "validate_note_markdown_text",
        lambda *_args, **_kwargs: ["invalid rendered Markdown"],
    )
    plan = json.loads(valid_coverage_plan_json())
    plan["visuals"][0]["supporting_ocr_evidence_ids"] = ["ocr_0001"]

    with pytest.raises(note_generation.NoteGenerationError) as captured:
        note_generation.generate_note_bundle(
            task_dir,
            FakeNoteProvider(
                [valid_v3_note_json(summary_evidence_ids=["tr_0001", "ocr_0001"])],
                plans=[json.dumps(plan, ensure_ascii=False)],
            ),
            run_id="normalization-markdown-failed",
        )

    metadata = json.loads(
        (captured.value.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["errors"] == ["invalid rendered Markdown"]
    assert metadata["evidence_normalizations"] == [
        {
            "attempt": 1,
            "kind": "v3_factual_ocr_parent",
            "factual_statement_index": 1,
            "trigger_ocr_evidence_ids": ["ocr_0001"],
            "added_parent_frame_id": "fr_0001",
        }
    ]


def test_rerender_does_not_normalize_invalid_active_v3(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import rerender_and_activate_note

    task_dir, task = task_workspace(tmp_path)
    (task_dir / "note.json").write_text(
        valid_v3_note_json(summary_evidence_ids=["ocr_0001"]),
        encoding="utf-8",
    )
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "providers": {"note": "legacy-provider"},
                "models": {"note": "legacy-model"},
            }
        ),
    )

    with pytest.raises(ValueError, match="without parent frame fr_0001"):
        rerender_and_activate_note(task_dir, tmp_path)

    assert not (task_dir / "generated_notes").exists()


def test_generate_note_bundle_keeps_a_failed_bundle_after_two_invalid_responses(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(["bad-one", "bad-two"])

    with pytest.raises(NoteGenerationError) as captured:
        generate_note_bundle(task_dir, provider, run_id="run-failed")

    bundle = captured.value.bundle_path
    assert bundle == task_dir / "generated_notes" / "run-failed"
    assert len(provider.calls) == 2
    assert provider.review_calls == []
    assert (bundle / "attempt-1.raw.txt").read_text(encoding="utf-8") == "bad-one"
    assert (bundle / "attempt-2.raw.txt").read_text(encoding="utf-8") == "bad-two"
    assert not (bundle / "note.json").exists()
    assert not (bundle / "note.md").exists()
    assert (
        json.loads((bundle / "generation.json").read_text(encoding="utf-8"))["status"]
        == "failed"
    )


def test_generate_note_bundle_does_not_retry_provider_errors(tmp_path: Path) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(error=RuntimeError("network unavailable"))

    with pytest.raises(NoteGenerationError) as captured:
        generate_note_bundle(task_dir, provider, run_id="run-provider-error")

    assert len(provider.calls) == 1
    metadata = json.loads(
        (captured.value.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["errors"] == ["provider failed: RuntimeError"]


def test_generate_note_bundle_preserves_safe_note_provider_diagnostics(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import NoteGenerationError, generate_note_bundle
    from learnnest.note_providers import NoteProviderError

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider(
        error=NoteProviderError(
            "MiMo note provider failed: HTTP 402; code=insufficient_balance"
        )
    )

    with pytest.raises(NoteGenerationError) as captured:
        generate_note_bundle(task_dir, provider, run_id="run-safe-provider-error")

    assert captured.value.errors == (
        "MiMo note provider failed: HTTP 402; code=insufficient_balance",
    )


def test_external_note_uses_the_same_bundle_pipeline_without_a_provider(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from learnnest.note_generation import build_external_note_bundle

    task_dir, _ = task_workspace(tmp_path)
    external = tmp_path / "external-note.json"
    external.write_text(valid_v3_note_json(), encoding="utf-8")
    _forbid_provider_citation_paths(monkeypatch)

    bundle = build_external_note_bundle(task_dir, external, run_id="run-external")

    assert (bundle / "attempt-1.raw.txt").read_text(
        encoding="utf-8"
    ) == valid_v3_note_json()
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["provider"] == "external-agent"
    assert metadata["attempt_count"] == 1


def test_validate_external_note_has_no_filesystem_side_effects(tmp_path: Path) -> None:
    from learnnest.note_generation import validate_external_note

    task_dir, _ = task_workspace(tmp_path)
    external = tmp_path / "invalid-note.json"
    external.write_text("not-json", encoding="utf-8")

    errors = validate_external_note(task_dir, external)

    assert errors
    assert not (task_dir / "generated_notes").exists()


def test_validate_external_note_uses_the_same_effective_type_selection(
    tmp_path: Path,
) -> None:
    from learnnest.note_generation import validate_external_note

    task_dir, task = task_workspace(tmp_path)
    write_task_atomic(
        task_dir,
        task.model_copy(update={"note_type_override": "resource_share"}),
    )
    external = tmp_path / "concept-note.json"
    external.write_text(valid_v3_note_json(), encoding="utf-8")

    inherited_errors = validate_external_note(task_dir, external)
    explicit_auto_errors = validate_external_note(
        task_dir,
        external,
        note_type="auto",
    )

    assert inherited_errors == [
        "generated note type does not match requested note type"
    ]
    assert explicit_auto_errors == []
    assert not (task_dir / "generated_notes").exists()


def test_fake_provider_generation_activates_and_validates_the_complete_task(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_and_activate_note

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider([valid_v3_note_json()])

    activated = generate_and_activate_note(task_dir, provider, tmp_path)

    assert activated.providers["note"] == "fake"
    assert activated.models["note"] == "queued"
    assert activated.artifacts["note"][0].startswith("generated_notes/")
    assert activated.artifacts["publish"][0].endswith("/note.md")
    assert activated.active_attempt_id is None
    assert activated.attempts[-1].from_stage == "note"
    assert activated.attempts[-1].status == "completed"
    assert "# 设置模型参数" in (tmp_path / "视频学习笔记" / "lesson.md").read_text(
        encoding="utf-8"
    )
    assert validate_task(task_dir) == []


def test_rejected_review_preserves_the_previous_active_note_and_publish(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import (
        NoteGenerationError,
        generate_and_activate_note,
    )

    task_dir, _ = task_workspace(tmp_path)
    first = generate_and_activate_note(
        task_dir, FakeNoteProvider([valid_v3_note_json()]), tmp_path
    )
    active_note_path = task_dir / first.artifacts["note"][0]
    published_path = tmp_path / "视频学习笔记" / "lesson.md"
    active_note_bytes = active_note_path.read_bytes()
    published_bytes = published_path.read_bytes()
    old_artifacts = first.artifacts

    rejected = FakeNoteProvider(
        [valid_v3_note_json()], reviews=[valid_review_json(verdict="reject")]
    )
    with pytest.raises(NoteGenerationError):
        generate_and_activate_note(task_dir, rejected, tmp_path)

    current = load_task(task_dir)
    assert current.artifacts == old_artifacts
    assert active_note_path.read_bytes() == active_note_bytes
    assert published_path.read_bytes() == published_bytes
    assert current.attempts[-1].status == "failed"
    assert current.attempts[-1].failed_stage == "note"


def test_validate_rejects_tampered_published_vault_note(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_and_activate_note

    task_dir, _ = task_workspace(tmp_path)
    generate_and_activate_note(
        task_dir,
        FakeNoteProvider([valid_v3_note_json()]),
        tmp_path,
    )
    published = tmp_path / "视频学习笔记" / "lesson.md"
    published.write_text("# 被篡改\n", encoding="utf-8")

    errors = validate_task(task_dir)

    assert any("published Vault note" in error for error in errors)


def test_new_note_invalidates_active_podcast_and_audio(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_and_activate_note

    task_dir, task = task_workspace(tmp_path)
    with_derived = task.model_copy(
        update={
            "stages": {
                **task.stages,
                "podcast_script": StageStatus.COMPLETED,
                "tts": StageStatus.COMPLETED,
            },
            "artifacts": {
                **task.artifacts,
                "podcast_script": [
                    "generated_podcasts/old/podcast_script.json",
                    "generated_podcasts/old/podcast_script.md",
                    "generated_podcasts/old/speech.txt",
                ],
                "tts": [
                    "generated_audio/old/audio.json",
                    "generated_audio/old/audio.wav",
                    "generated_audio/old/audio.mp3",
                ],
            },
            "providers": {"podcast_script": "old", "tts": "old"},
            "models": {"podcast_script": "old", "tts": "old"},
        }
    )
    write_task_atomic(task_dir, with_derived)

    activated = generate_and_activate_note(
        task_dir,
        FakeNoteProvider([valid_v3_note_json()]),
        tmp_path,
    )

    assert activated.stages["podcast_script"] is StageStatus.PENDING
    assert activated.stages["tts"] is StageStatus.PENDING
    assert "podcast_script" not in activated.artifacts
    assert "tts" not in activated.artifacts
    assert "podcast_script" not in activated.providers
    assert "tts" not in activated.providers
    assert len(v3_render_tasks) == 1
    render_task = v3_render_tasks[0]
    assert render_task.stages["podcast_script"] == StageStatus.PENDING
    assert render_task.stages["tts"] == StageStatus.PENDING
    assert "podcast_script" not in render_task.artifacts
    assert "tts" not in render_task.artifacts


def test_generation_retry_reconciles_pending_publish_without_calling_provider(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import generate_and_activate_note

    task_dir, _ = task_workspace(tmp_path)
    first_provider = FakeNoteProvider([valid_v3_note_json()])
    activated = generate_and_activate_note(task_dir, first_provider, tmp_path)
    pending = activated.model_copy(
        update={
            "stages": {**activated.stages, "publish": StageStatus.RUNNING},
            "artifacts": {
                stage: paths
                for stage, paths in activated.artifacts.items()
                if stage != "publish"
            },
        }
    )
    write_task_atomic(task_dir, pending)
    retry_provider = FakeNoteProvider(error=AssertionError("provider must not run"))

    recovered = generate_and_activate_note(task_dir, retry_provider, tmp_path)

    assert retry_provider.calls == []
    assert recovered.stages["publish"] is StageStatus.COMPLETED
    assert load_task(task_dir) == recovered


def test_rerender_active_note_creates_a_new_bundle_without_provider_call(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from learnnest.note_generation import (
        generate_and_activate_note,
        rerender_and_activate_note,
    )

    task_dir, _ = task_workspace(tmp_path)
    provider = FakeNoteProvider([valid_v3_note_json()])
    first = generate_and_activate_note(task_dir, provider, tmp_path)
    first_note_json = first.artifacts["note"][0]
    _forbid_provider_citation_paths(monkeypatch)

    rerendered = rerender_and_activate_note(task_dir, tmp_path)

    assert len(provider.calls) == 1
    assert rerendered.artifacts["note"][0] != first_note_json
    assert rerendered.providers["note"] == "fake"
    assert rerendered.models["note"] == "queued"
    bundle = task_dir / Path(rerendered.artifacts["note"][0]).parent
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["attempt_count"] == 0
    assert metadata["operation"] == "rerender"
    assert metadata["note_type"] == "concept_explanation"
    assert metadata["note_type_source"] == "auto"
    assert validate_task(task_dir) == []


def test_rerender_preserves_a_non_auto_v3_type_source(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import (
        generate_and_activate_note,
        rerender_and_activate_note,
    )

    task_dir, _ = task_workspace(tmp_path)
    generate_and_activate_note(
        task_dir,
        FakeNoteProvider([valid_v3_note_json()]),
        tmp_path,
        note_type="concept",
    )

    rerendered = rerender_and_activate_note(task_dir, tmp_path)

    bundle = task_dir / Path(rerendered.artifacts["note"][0]).parent
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert metadata["note_type"] == "concept_explanation"
    assert metadata["note_type_source"] == "cli"


def test_rerender_v2_remains_compatible_without_type_metadata(tmp_path: Path) -> None:
    from learnnest.note_generation import rerender_and_activate_note

    task_dir, task = task_workspace(tmp_path)
    (task_dir / "note.json").write_text(valid_note_json(), encoding="utf-8")
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "providers": {"note": "legacy"},
                "models": {"note": "2.0"},
            }
        ),
    )

    rerendered = rerender_and_activate_note(task_dir, tmp_path)

    bundle = task_dir / Path(rerendered.artifacts["note"][0]).parent
    metadata = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert "note_type" not in metadata
    assert "note_type_source" not in metadata


def test_external_generation_activates_through_the_same_task_contract(
    tmp_path: Path,
    v3_render_tasks: list[TaskRecord],
) -> None:
    from learnnest.note_generation import build_and_activate_external_note

    task_dir, _ = task_workspace(tmp_path)
    external = tmp_path / "external-note.json"
    external.write_text(valid_v3_note_json(), encoding="utf-8")

    activated = build_and_activate_external_note(task_dir, external, tmp_path)

    assert activated.providers["note"] == "external-agent"
    assert activated.models["note"] == "external"
    assert validate_task(task_dir) == []
