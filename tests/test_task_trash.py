from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from learnnest.automation_models import AutomationIntake, AutomationTaskState
from learnnest.automation_store import create_intake, save_task_state
from learnnest.models import StageStatus
from learnnest.task_store import create_task, find_task_by_id, write_task_atomic
from learnnest.task_trash import (
    TaskTrashError,
    list_trashed_tasks,
    purge_trashed_task,
    restore_trashed_task,
    trash_task,
)
from learnnest.web_jobs import WebJobStore


def _task(root: Path, task_id: str, *, running: bool = False) -> Path:
    task_dir = root / "视频学习素材" / task_id
    task = create_task(
        task_id=task_id,
        source_path="C:/private/original.mp4",
        source_fingerprint=task_id,
        title="待删除任务",
    ).model_copy(
        update={
            "stages": {
                "content_pack": (
                    StageStatus.RUNNING if running else StageStatus.COMPLETED
                )
            },
            "artifacts": {"content_pack": ["content_pack.json"]},
        }
    )
    write_task_atomic(task_dir, task)
    (task_dir / "content_pack.json").write_text("{}", encoding="utf-8")
    return task_dir


def test_trash_task_moves_all_task_identity_facts_without_touching_original_video(
    tmp_path: Path,
) -> None:
    task_id = "20260814-trash0001"
    task_dir = _task(tmp_path, task_id)
    original_video = tmp_path.parent / "original-video.mp4"
    original_video.write_bytes(b"original")
    create_intake(
        tmp_path,
        AutomationIntake(
            task_id=task_id,
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime.now(UTC),
            status="needs_attention",
        ),
    )
    save_task_state(
        tmp_path,
        AutomationTaskState(
            task_id=task_id,
            policy_sha256="a" * 64,
            default_output="complete_note",
            status="needs_attention",
            blocked_reason="non_retryable_failure",
        ),
    )
    jobs = WebJobStore(tmp_path)
    job = jobs.create("local_video", str(original_video))
    jobs.start(job.job_id)
    jobs.complete(job.job_id, task_id)
    _task(tmp_path, "20260814-preserved")

    result = trash_task(tmp_path, task_id)

    assert result.task_id == task_id
    assert result.trash_path.is_relative_to(tmp_path / ".learnnest" / "trash")
    assert (result.trash_path / "task" / "task.json").is_file()
    assert (result.trash_path / "automation-intake.json").is_file()
    assert (result.trash_path / "automation-states" / ("a" * 64 + ".json")).is_file()
    assert (result.trash_path / "source-jobs" / f"{job.job_id}.json").is_file()
    assert (result.trash_path / "manifest.json").is_file()
    assert not task_dir.exists()
    assert find_task_by_id(tmp_path, task_id) is None
    assert find_task_by_id(tmp_path, "20260814-preserved") is not None
    assert original_video.read_bytes() == b"original"


def test_trash_task_refuses_a_task_that_is_still_running(tmp_path: Path) -> None:
    task_id = "20260814-running"
    task_dir = _task(tmp_path, task_id, running=True)

    with pytest.raises(TaskTrashError, match="正在处理"):
        trash_task(tmp_path, task_id)

    assert task_dir.is_dir()


def test_trashed_task_can_be_listed_restored_and_purged(tmp_path: Path) -> None:
    task_id = "20260814-restore1"
    task_dir = _task(tmp_path, task_id)
    trashed = trash_task(tmp_path, task_id)

    listed = list_trashed_tasks(tmp_path)

    assert [(item.bundle_id, item.task_id, item.title) for item in listed] == [
        (trashed.trash_path.name, task_id, "待删除任务")
    ]
    restored = restore_trashed_task(tmp_path, trashed.trash_path.name)
    assert restored.task_id == task_id
    assert task_dir.is_dir()
    assert list_trashed_tasks(tmp_path) == ()

    trashed_again = trash_task(tmp_path, task_id)
    purged_id = purge_trashed_task(tmp_path, trashed_again.trash_path.name)
    assert purged_id == task_id
    assert not trashed_again.trash_path.exists()
    assert list_trashed_tasks(tmp_path) == ()


def test_restore_refuses_to_overwrite_an_existing_task_identity(tmp_path: Path) -> None:
    task_id = "20260814-conflict"
    _task(tmp_path, task_id)
    trashed = trash_task(tmp_path, task_id)
    _task(tmp_path, task_id)

    with pytest.raises(TaskTrashError, match="已有同一任务"):
        restore_trashed_task(tmp_path, trashed.trash_path.name)

    assert trashed.trash_path.is_dir()


def test_trash_task_rolls_back_identity_facts_when_a_move_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = "20260814-rollback"
    task_dir = _task(tmp_path, task_id)
    create_intake(
        tmp_path,
        AutomationIntake(
            task_id=task_id,
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime.now(UTC),
            status="needs_attention",
        ),
    )
    intake_path = tmp_path / ".learnnest" / "automation" / "intake" / f"{task_id}.json"
    original_replace = Path.replace

    def fail_task_move(source: Path, target: Path) -> Path:
        if source == task_dir:
            raise OSError("simulated task move failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_task_move)

    with pytest.raises(TaskTrashError, match="未能移入回收区"):
        trash_task(tmp_path, task_id)

    assert task_dir.is_dir()
    assert intake_path.is_file()
