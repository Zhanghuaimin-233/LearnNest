from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from learnnest.assisted_note_generation import (
    build_reader_dossier,
    create_assisted_plan,
    generate_assisted_plan,
    load_assisted_plan,
    load_assisted_state,
    recover_assisted_plan,
    review_assisted_plan,
)
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.task_store import write_task_atomic


def _snapshot(name: str = "default") -> AssistedConnectionSnapshot:
    return AssistedConnectionSnapshot(
        connection_name=name,
        provider="fake-openai",
        endpoint_identity="https://example.test/v1",
        model="fake-1",
        adapter_revision="1",
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "output"
    task_dir = root / "视频学习素材" / "assisted-task"
    task_dir.mkdir(parents=True)
    pack = ContentPack(
        task_id="20260726-assisted",
        source_fingerprint="assisted-fingerprint",
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
        ],
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2), encoding="utf-8"
    )
    write_task_atomic(
        task_dir,
        TaskRecord(
            task_id=pack.task_id,
            source_path="C:/videos/assisted.mp4",
            source_fingerprint=pack.source_fingerprint,
            title="辅助笔记",
            stages={"content_pack": StageStatus.COMPLETED},
            artifacts={"content_pack": ["content_pack.json"]},
        ),
    )
    return root


class FakeAssistedProvider:
    name = "fake-openai"
    model = "fake-1"
    endpoint_identity = "https://example.test/v1"

    def __init__(self, *, fail_review: bool = False) -> None:
        self.writer_calls = 0
        self.reviewer_calls = 0
        self.fail_review = fail_review

    def write_markdown(self, dossier_json: str) -> str:
        self.writer_calls += 1
        assert "tr_0001" not in dossier_json
        assert "打开设置" in dossier_json
        return "# 设置\n\n打开设置后完成修改。"

    def review_markdown(self, dossier_json: str, candidate_markdown: str) -> str:
        self.reviewer_calls += 1
        if self.fail_review:
            raise RuntimeError("review transport unavailable")
        assert "打开设置" in dossier_json
        assert candidate_markdown.startswith("# 设置")
        return "# 设置\n\n先打开设置，再完成修改。"


def _plan(root: Path) -> Path:
    snapshot = _snapshot()
    return create_assisted_plan(
        root,
        ["20260726-assisted"],
        writer=snapshot,
        reviewer=snapshot,
        now=datetime(2026, 7, 26, 8, 0, tzinfo=UTC),
    )


def test_plan_freezes_dossier_and_connection_without_provider_call(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)

    plan = load_assisted_plan(plan_path)
    dossier = next((root / "视频学习素材").rglob("dossier.json")).read_text(
        encoding="utf-8"
    )

    assert plan.total_max_calls == 2
    assert plan.writer.connection_name == "default"
    assert "tr_0001" not in dossier
    assert "打开设置" in dossier
    assert load_assisted_state(plan_path).tasks[0].status == "planned"


def test_reader_dossier_keeps_transcript_and_frame_ocr_separate(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    task_dir = next((root / "视频学习素材").rglob("task.json")).parent
    task = TaskRecord.model_validate_json((task_dir / "task.json").read_bytes())
    pack = ContentPack.model_validate_json(
        (task_dir / "content_pack.json").read_bytes()
    )

    dossier = json.loads(build_reader_dossier(task, pack))

    assert dossier["schema_version"] == "1.1"
    assert dossier["instructions"]["source_separation"]
    assert dossier["source_material"]["transcript"]["segments"] == [
        {"start_ms": 0, "end_ms": 1_000, "content": "打开设置。"}
    ]
    assert dossier["source_material"]["visual_frames"] == [
        {
            "start_ms": 500,
            "visual_context": (
                "Only OCR extracted from this frame is supplied; frame pixels "
                "are not included."
            ),
            "ocr": [{"content": "设置"}],
        }
    ]
    assert dossier["source_material"]["orphaned_ocr"] == []
    serialized = json.dumps(dossier, ensure_ascii=False)
    assert "tr_0001" not in serialized
    assert "fr_0001" not in serialized
    assert "ocr_0001" not in serialized


def test_writer_and_reviewer_are_each_called_once_and_stay_isolated(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)
    provider = FakeAssistedProvider()

    generate_assisted_plan(plan_path, root, provider)
    generate_assisted_plan(plan_path, root, provider)
    review_assisted_plan(plan_path, root, provider)
    review_assisted_plan(plan_path, root, provider)

    state = load_assisted_state(plan_path).tasks[0]
    bundle = (
        next((root / "视频学习素材").rglob("assisted-draft"))
        / load_assisted_plan(plan_path).plan_id
    )
    metadata = json.loads(
        (bundle / "reviewed" / "metadata.json").read_text(encoding="utf-8")
    )
    assert provider.writer_calls == provider.reviewer_calls == 1
    assert state.status == "model_reviewed"
    assert state.writer.actual_call_count == state.reviewer.actual_call_count == 1
    assert (bundle / "candidate" / "note.md").is_file()
    assert (bundle / "reviewed" / "note.md").is_file()
    assert metadata["route"] == "assisted_draft"
    assert "quality-first" not in str(bundle)
    assert not (root / "视频学习笔记").exists()


def test_reviewer_failure_keeps_candidate_and_requires_new_plan_for_retry(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)
    provider = FakeAssistedProvider(fail_review=True)

    generate_assisted_plan(plan_path, root, provider)
    review_assisted_plan(plan_path, root, provider)
    review_assisted_plan(plan_path, root, provider)

    state = load_assisted_state(plan_path).tasks[0]
    assert provider.writer_calls == 1
    assert provider.reviewer_calls == 1
    assert state.status == "review_failed"
    assert state.candidate_path is not None
    assert state.reviewed_path is None


def test_recover_recreates_missing_local_candidate_without_provider_call(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)
    provider = FakeAssistedProvider()
    generate_assisted_plan(plan_path, root, provider)
    candidate = next((root / "视频学习素材").rglob("candidate/note.md"))
    candidate.unlink()

    recover_assisted_plan(plan_path, root)

    assert candidate.is_file()
    assert provider.writer_calls == 1
    assert provider.reviewer_calls == 0


def test_recover_finishes_a_persisted_writer_response_without_a_second_call(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)
    provider = FakeAssistedProvider()
    generate_assisted_plan(plan_path, root, provider)
    state_path = plan_path.parent / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["tasks"][0]["writer"]["status"] = "running"
    payload["tasks"][0]["status"] = "local_recovery_failed"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    candidate = next((root / "视频学习素材").rglob("candidate/note.md"))
    candidate.unlink()

    recover_assisted_plan(plan_path, root)

    state = load_assisted_state(plan_path).tasks[0]
    assert provider.writer_calls == 1
    assert provider.reviewer_calls == 0
    assert candidate.is_file()
    assert state.status == "draft_ready"
    assert state.writer.status == "completed"


def test_changed_content_pack_fails_locally_before_writer_call(tmp_path: Path) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)
    provider = FakeAssistedProvider()
    pack_path = next((root / "视频学习素材").rglob("content_pack.json"))
    pack_path.write_text("{}", encoding="utf-8")

    generate_assisted_plan(plan_path, root, provider)

    state = load_assisted_state(plan_path).tasks[0]
    assert provider.writer_calls == 0
    assert state.status == "local_recovery_failed"
