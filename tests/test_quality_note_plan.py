from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.note_models import ReaderDraft
from learnnest.quality_note_generation import (
    create_quality_plan,
    generate_quality_note_plan,
    load_quality_plan,
    load_quality_state,
    organize_quality_plan,
    recover_quality_plan,
    review_quality_note_plan,
)
from learnnest.reader_templates import builtin_reader_template
from learnnest.task_store import write_task_atomic


def _pack() -> ContentPack:
    return ContentPack(
        task_id="20260723-quality-plan",
        source_fingerprint="quality-plan-fingerprint",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="打开设置。",
                artifact_path="content_pack.json",
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
                start_ms=500,
                end_ms=700,
                text="设置",
                artifact_path="content_pack.json",
                frame_id="fr_0001",
            ),
            Evidence(
                id="tr_0002",
                kind="transcript",
                start_ms=1_000,
                end_ms=2_000,
                text="保存修改。",
                artifact_path="content_pack.json",
            ),
        ],
    )


def _make_task_root(tmp_path: Path) -> Path:
    root = tmp_path / "output"
    task_dir = root / "视频学习素材" / "quality-task"
    task_dir.mkdir(parents=True)
    pack = _pack()
    pack_path = task_dir / "content_pack.json"
    pack_path.write_text(pack.model_dump_json(indent=2), encoding="utf-8")
    write_task_atomic(
        task_dir,
        TaskRecord(
            task_id=pack.task_id,
            source_path="C:/videos/quality.mp4",
            source_fingerprint=pack.source_fingerprint,
            title="质量计划",
            stages={"content_pack": StageStatus.COMPLETED},
            artifacts={"content_pack": ["content_pack.json"]},
        ),
    )
    return root


class FakeQualityProvider:
    name = "fake-quality"
    model = "fake-1"

    def __init__(self, *, review_status: str = "flagged") -> None:
        self.organize_calls = 0
        self.write_calls = 0
        self.review_calls = 0
        self.review_status = review_status

    def organize(self, shard_json: str) -> str:
        self.organize_calls += 1
        payload = json.loads(shard_json)
        return json.dumps(
            {
                "schema_version": "1.0",
                "units": [
                    {
                        "raw_evidence_ids": [
                            atom["evidence_id"] for atom in payload["atoms"]
                        ],
                        "unit_type": "step",
                        "topic_labels": ["步骤"],
                        "outline": "按顺序执行",
                        "visual_role": "useful",
                        "visual_reason": "确认操作上下文",
                    }
                ],
            },
            ensure_ascii=False,
        )

    def write(self, organization_json: str, template_json: str) -> str:
        self.write_calls += 1
        organization = json.loads(organization_json)
        unit_id = organization["units"][0]["unit_id"]
        return ReaderDraft(
            title="设置操作",
            sections=[
                {
                    "slot": slot,
                    "items": [{"text": text, "evidence_unit_ids": [unit_id]}],
                }
                for slot, text in (
                    ("summary", "先打开设置，再保存修改。"),
                    ("why_learn", "这样可以复现修改流程。"),
                    ("narrative", "内容先展示入口，再说明保存。"),
                    ("core", "设置和保存是连续步骤。"),
                    ("practice", "按照视频顺序完成一次操作。"),
                    ("review", "回忆操作的两个关键环节。"),
                )
            ],
        ).model_dump_json()

    def review(self, note_json: str, organization_json: str) -> str:
        self.review_calls += 1
        return json.dumps(
            {
                "schema_version": "1.0",
                "status": self.review_status,
                "issues": (
                    [
                        {
                            "code": "reader-flow",
                            "severity": "medium",
                            "message": "复核者建议再检查阅读主线。",
                        }
                    ]
                    if self.review_status == "flagged"
                    else []
                ),
            },
            ensure_ascii=False,
        )


def test_plan_persists_input_sha_and_explicit_call_budget_without_provider_calls(
    tmp_path: Path,
) -> None:
    root = _make_task_root(tmp_path)
    pack_path = root / "视频学习素材" / "quality-task" / "content_pack.json"
    expected_sha = hashlib.sha256(pack_path.read_bytes()).hexdigest()
    provider = FakeQualityProvider()

    plan_path = create_quality_plan(
        root,
        ["20260723-quality-plan"],
        template=builtin_reader_template("concept"),
        organizer_provider=provider.name,
        organizer_model="organizer-1",
        writer_provider=provider.name,
        writer_model="writer-1",
        reviewer_provider=provider.name,
        reviewer_model="reviewer-1",
        review_mode="gate",
        shard_size=2,
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )

    plan = load_quality_plan(plan_path)

    assert provider.organize_calls == provider.write_calls == provider.review_calls == 0
    assert plan.tasks[0].content_pack_sha256 == expected_sha
    assert plan.tasks[0].max_calls == 5
    assert plan.total_max_calls == 5


def test_plan_execution_does_not_repeat_completed_paid_roles_and_gate_preserves_active(
    tmp_path: Path,
) -> None:
    root = _make_task_root(tmp_path)
    provider = FakeQualityProvider(review_status="flagged")
    plan_path = create_quality_plan(
        root,
        ["20260723-quality-plan"],
        template=builtin_reader_template("concept"),
        organizer_provider=provider.name,
        organizer_model=provider.model,
        writer_provider=provider.name,
        writer_model=provider.model,
        reviewer_provider=provider.name,
        reviewer_model=provider.model,
        review_mode="gate",
        shard_size=2,
        now=datetime(2026, 7, 23, 12, 1, tzinfo=UTC),
    )

    organize_quality_plan(plan_path, root, provider)
    generate_quality_note_plan(plan_path, root, provider)
    review_quality_note_plan(plan_path, root, provider)
    organize_quality_plan(plan_path, root, provider)
    generate_quality_note_plan(plan_path, root, provider)
    review_quality_note_plan(plan_path, root, provider)

    state = load_quality_state(plan_path)
    task_state = state.tasks[0]
    assert provider.organize_calls == 3
    assert provider.write_calls == 1
    assert provider.review_calls == 1
    assert task_state.status == "quality_reported"
    assert task_state.activation_decision == "retain_previous_active"
    assert not (plan_path.parent / "active" / "20260723-quality-plan.md").exists()


def test_recovery_only_reconciles_local_candidate_and_never_calls_provider(
    tmp_path: Path,
) -> None:
    root = _make_task_root(tmp_path)
    provider = FakeQualityProvider(review_status="passed")
    plan_path = create_quality_plan(
        root,
        ["20260723-quality-plan"],
        template=builtin_reader_template("concept"),
        organizer_provider=provider.name,
        organizer_model=provider.model,
        writer_provider=provider.name,
        writer_model=provider.model,
        reviewer_provider=provider.name,
        reviewer_model=provider.model,
        review_mode="gate",
        shard_size=2,
        now=datetime(2026, 7, 23, 12, 2, tzinfo=UTC),
    )
    organize_quality_plan(plan_path, root, provider)
    generate_quality_note_plan(plan_path, root, provider)

    recover_quality_plan(plan_path, root)

    assert provider.organize_calls == 3
    assert provider.write_calls == 1
    assert provider.review_calls == 0
    state = load_quality_state(plan_path)
    assert state.tasks[0].status == "source_valid"


def test_report_mode_recovery_waits_for_quality_report(
    tmp_path: Path,
) -> None:
    root = _make_task_root(tmp_path)
    provider = FakeQualityProvider(review_status="passed")
    plan_path = create_quality_plan(
        root,
        ["20260723-quality-plan"],
        template=builtin_reader_template("concept"),
        organizer_provider=provider.name,
        organizer_model=provider.model,
        writer_provider=provider.name,
        writer_model=provider.model,
        reviewer_provider=provider.name,
        reviewer_model=provider.model,
        review_mode="report",
        shard_size=2,
        now=datetime(2026, 7, 23, 12, 3, tzinfo=UTC),
    )
    organize_quality_plan(plan_path, root, provider)
    generate_quality_note_plan(plan_path, root, provider)

    recover_quality_plan(plan_path, root)

    state = load_quality_state(plan_path)
    assert state.tasks[0].status == "source_valid"
    assert not (plan_path.parent / "active" / "20260723-quality-plan.md").exists()

    review_quality_note_plan(plan_path, root, provider)
    state = load_quality_state(plan_path)
    assert state.tasks[0].status == "active"
    assert provider.review_calls == 1


def test_recovery_marks_interrupted_writer_without_retrying_provider(
    tmp_path: Path,
) -> None:
    root = _make_task_root(tmp_path)
    provider = FakeQualityProvider()
    plan_path = create_quality_plan(
        root,
        ["20260723-quality-plan"],
        template=builtin_reader_template("concept"),
        organizer_provider=provider.name,
        organizer_model=provider.model,
        writer_provider=provider.name,
        writer_model=provider.model,
        reviewer_provider=provider.name,
        reviewer_model=provider.model,
        review_mode="gate",
        shard_size=2,
        now=datetime(2026, 7, 23, 12, 4, tzinfo=UTC),
    )
    state_path = plan_path.parent / "state.json"
    state = load_quality_state(plan_path)
    task_state = state.tasks[0]
    state.tasks[0] = task_state.model_copy(
        update={
            "status": "organized",
            "writer": task_state.writer.model_copy(update={"status": "running"}),
        }
    )
    state_path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    recovered = recover_quality_plan(plan_path, root)

    assert provider.organize_calls == provider.write_calls == provider.review_calls == 0
    assert recovered.tasks[0].status == "failed"
    assert recovered.tasks[0].writer.failure_code == "recovery_not_possible"
    assert recovered.tasks[0].writer.retryability == "requires_new_plan"
