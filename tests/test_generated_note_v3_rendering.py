from __future__ import annotations

import json
from pathlib import Path

from helpers.note_v3_fixtures import (
    concept_payload,
    long_resource_payload,
    practical_payload,
    resource_payload,
    short_practical_payload,
)
from learnnest.citation_audit import statement_citation_packet_sha256
from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.note_evidence_scope import StatementCitationPacketV12
from learnnest.note_generation import generate_note_bundle
from learnnest.note_models import GENERATED_NOTE_ADAPTER
from learnnest.rendering import render_generated_note
from learnnest.task_store import write_task_atomic
from learnnest.validation import validate_note_markdown_text


class _FakeConceptProvider:
    name = "fake"
    model = "v3-rendering"

    def generate(
        self,
        draft_scope_json: str,
        validation_feedback: tuple[str, ...],
        *,
        draft_execution_json: str,
        requested_note_type: str | None = None,
    ) -> str:
        assert json.loads(draft_scope_json)["task_id"] == "20260713-a1b2c3d4"
        assert validation_feedback == ()
        assert json.loads(draft_execution_json)["units"]
        assert requested_note_type is None
        return json.dumps(concept_payload(), ensure_ascii=False)

    def plan(
        self,
        content_pack_json: str,
        *,
        validation_feedback: tuple[str, ...] = (),
    ) -> str:
        assert validation_feedback == ()
        content_pack = json.loads(content_pack_json)
        return json.dumps(
            {
                "schema_version": "1.1",
                "task_id": content_pack["task_id"],
                "source_fingerprint": content_pack["source_fingerprint"],
                "units": [
                    {
                        "label": "背景",
                        "source_evidence_ids": ["tr_0001"],
                        "rationale": "背景概念是材料的主体。",
                    },
                    {
                        "label": "智能体循环",
                        "source_evidence_ids": ["tr_0002"],
                        "rationale": "循环是材料的核心概念。",
                    },
                    {
                        "label": "检索增强",
                        "source_evidence_ids": ["tr_0003"],
                        "rationale": "检索机制是材料的核心概念。",
                    },
                    {
                        "label": "常见误解",
                        "source_evidence_ids": ["tr_0004"],
                        "rationale": "误解澄清是材料的核心概念。",
                    },
                ],
                "visuals": [
                    {
                        "frame_evidence_id": "fr_0001",
                        "supporting_ocr_evidence_ids": [],
                        "disposition": "use",
                        "unit_label": "背景",
                        "rationale": "渲染测试使用该帧支持核心概念。",
                    }
                ],
            },
            ensure_ascii=False,
        )

    def review(
        self,
        content_pack_json: str,
        candidate_note_json: str,
        coverage_plan_json: str,
        statement_manifest_json: str,
        *,
        validation_feedback: tuple[str, ...] = (),
    ) -> str:
        content_pack = json.loads(content_pack_json)
        plan = json.loads(coverage_plan_json)
        statement_manifest = json.loads(statement_manifest_json)
        assert json.loads(candidate_note_json)["schema_version"] == "3.0"
        return json.dumps(
            {
                "schema_version": "1.1",
                "task_id": content_pack["task_id"],
                "source_fingerprint": content_pack["source_fingerprint"],
                "verdict": "approve",
                "coverage_units": [
                    {
                        "label": unit["label"],
                        "source_evidence_ids": unit["source_evidence_ids"],
                        "candidate_paths": [path],
                        "status": "covered",
                        "rationale": "候选包含该冻结概念。",
                    }
                    for unit, path in zip(
                        plan["units"],
                        [
                            "/summary",
                            "/concepts/0/explanation",
                            "/concepts/1/explanation",
                            "/misconceptions/0",
                        ],
                        strict=True,
                    )
                ],
                "statement_audits": [
                    {
                        "candidate_path": item["candidate_path"],
                        "status": "aligned",
                        "rationale": "测试候选的 statement 与证据一致。",
                    }
                    for item in statement_manifest
                ],
                "issues": [],
            },
            ensure_ascii=False,
        )

    def audit_citations(
        self,
        packet_json: str,
        *,
        validation_feedback: tuple[str, ...] = (),
    ) -> str:
        assert validation_feedback == ()
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


def _content_pack() -> ContentPack:
    evidence = [
        Evidence(
            id=f"tr_{index:04d}",
            kind="transcript",
            start_ms=(index - 1) * 1_000,
            end_ms=index * 1_000,
            text=(
                "字幕证据原文 sentinel，资源地址 https://resource.example/tool"
                if index == 1
                else f"字幕证据 {index}"
            ),
            artifact_path="transcript.json",
        )
        for index in range(1, 6)
    ]
    evidence.append(
        Evidence(
            id="fr_0001",
            kind="frame",
            start_ms=1_500,
            artifact_path="frames/selected/fr_0001.png",
        )
    )
    return ContentPack(
        task_id="20260713-a1b2c3d4",
        source_fingerprint="a1b2c3d4",
        evidence=evidence,
    )


def _task() -> TaskRecord:
    return TaskRecord(
        task_id="20260713-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )


def _render(payload: dict[str, object]) -> str:
    note = GENERATED_NOTE_ADAPTER.validate_python(payload)
    return render_generated_note(
        _task(),
        _content_pack(),
        note,
        asset_prefix="视频学习素材/lesson",
    )


def test_concept_renderer_has_no_practical_or_visible_source_sections() -> None:
    markdown = _render(concept_payload())

    assert "## 核心概念" in markdown
    assert "## 操作步骤" not in markdown
    assert "## 引用画面" not in markdown
    assert "来源：" not in markdown
    assert "<!-- evidence: tr_0001, fr_0001 -->" in markdown


def test_frame_is_embedded_at_first_fact_only() -> None:
    markdown = _render(concept_payload(reuse_frame=True))

    assert markdown.count("![[视频学习素材/") == 1
    assert markdown.index("![[") > markdown.index("### Agent Loop")


def test_optional_sections_and_placeholder_sentences_are_absent() -> None:
    markdown = _render(short_practical_payload())

    assert "## 故障处理" not in markdown
    assert "## 开始前准备" not in markdown
    assert "## 完成检查" not in markdown
    assert "## 注意事项" not in markdown
    assert "## AI 补充" not in markdown
    assert "视频中没有" not in markdown
    assert "\n- 无。" not in markdown


def test_unknown_id_inside_hidden_comment_is_rejected(tmp_path: Path) -> None:
    errors = validate_note_markdown_text(
        tmp_path / "视频学习素材" / "lesson",
        "<!-- evidence: tr_9999 -->",
        _content_pack(),
    )

    assert errors == ["note Markdown references unknown evidence id: tr_9999"]


def test_resource_renderer_orders_value_access_limit_and_reminders() -> None:
    markdown = _render(resource_payload())
    headings = ["## 资源速览", "**获取或使用：**", "**限制：**", "## 必要提醒"]

    positions = [markdown.index(value) for value in headings]

    assert positions == sorted(positions)
    assert "<https://resource.example/tool>" in markdown


def test_practical_renderer_orders_real_steps_before_checks() -> None:
    markdown = _render(practical_payload())
    headings = ["## 最终目标", "## 开始前准备", "## 操作步骤", "## 完成检查"]

    positions = [markdown.index(value) for value in headings]

    assert positions == sorted(positions)


def test_long_resource_and_short_practical_keep_their_natural_item_counts() -> None:
    resource_markdown = _render(long_resource_payload())
    practical_markdown = _render(short_practical_payload())

    assert resource_markdown.count("### ") == len(long_resource_payload()["resources"])
    assert practical_markdown.count("### 第 ") == 2


def test_v3_trace_is_folded_and_never_copies_evidence_text_or_counts() -> None:
    markdown = _render(concept_payload())

    assert "<summary>证据与追溯</summary>" in markdown
    assert "引用规模" not in markdown
    assert "字幕证据原文 sentinel" not in markdown
    assert "[[视频学习素材/lesson/transcript.md|字幕原文]]" in markdown


def test_concept_fixture_does_not_repeat_points_as_steps() -> None:
    markdown = _render(concept_payload())

    for concept in concept_payload()["concepts"]:
        assert markdown.count(concept["title"]) == 1
    assert "## 操作步骤" not in markdown


def test_generation_uses_real_v3_renderer_without_test_stub(tmp_path: Path) -> None:
    task_dir = tmp_path / "视频学习素材" / "lesson"
    task_dir.mkdir(parents=True)
    content_pack = _content_pack()
    (task_dir / "content_pack.json").write_text(
        content_pack.model_dump_json(indent=2), encoding="utf-8"
    )
    frame_path = task_dir / "frames" / "selected" / "fr_0001.png"
    frame_path.parent.mkdir(parents=True)
    frame_path.write_bytes(b"frame")
    write_task_atomic(
        task_dir,
        _task().model_copy(
            update={
                "stages": {"content_pack": StageStatus.COMPLETED},
                "artifacts": {"content_pack": ["content_pack.json"]},
            }
        ),
    )

    bundle = generate_note_bundle(
        task_dir,
        _FakeConceptProvider(),
        run_id="v3-renderer",
    )
    markdown = (bundle / "note.md").read_text(encoding="utf-8")

    assert "## 核心概念" in markdown
    assert "来源：" not in markdown
