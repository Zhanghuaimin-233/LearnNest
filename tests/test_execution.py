from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from learnnest.execution import (
    begin_attempt,
    begin_persisted_attempt,
    classify_failure,
    complete_persisted_attempt,
    finish_attempt,
    next_retry_time,
    plan_recovery,
)
from learnnest.models import StageStatus, TaskRecord
from learnnest.stages import stage_artifacts
from learnnest.task_store import (
    complete_task_goal,
    create_task,
    load_task,
    write_task_atomic,
)


def _task(**changes: object) -> TaskRecord:
    payload: dict[str, object] = {
        "task_id": "20260712-a1b2c3d4",
        "source_path": "C:/videos/lesson.mp4",
        "source_fingerprint": "a1b2c3d4",
        "title": "lesson",
    }
    payload.update(changes)
    return TaskRecord(**payload)


def test_failure_classification_distinguishes_retry_manual_and_terminal() -> None:
    assert (
        classify_failure(RuntimeError("HTTP 503 unavailable")).disposition
        == "retryable"
    )
    assert (
        classify_failure(RuntimeError("HTTP 429 rate limited")).disposition
        == "retryable"
    )


def test_retryable_failures_use_bounded_one_five_thirty_minute_backoff() -> None:
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    failure = classify_failure(RuntimeError("HTTP 503 unavailable"))
    task = _task()
    observed = []

    for ordinal, expected_minutes in enumerate((1, 5, 30, None), start=1):
        running = begin_attempt(
            task,
            reason="initial" if ordinal == 1 else "retry",
            from_stage="transcript",
            now=now,
        )
        retry_at = next_retry_time(running, failure, now=now)
        observed.append(retry_at)
        task = finish_attempt(
            running,
            status="failed",
            now=now,
            failed_stage="transcript",
            failure=failure,
            next_retry_at=retry_at,
        )

        if expected_minutes is None:
            assert retry_at is None
        else:
            assert retry_at == now + timedelta(minutes=expected_minutes)

    assert observed[-1] is None
    assert (
        classify_failure(RuntimeError("HTTP 404 not found")).disposition == "terminal"
    )
    assert (
        classify_failure(ValueError("MIMO_API_KEY is missing")).disposition == "manual"
    )


def test_retry_backoff_is_independent_for_each_failed_stage() -> None:
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    failure = classify_failure(RuntimeError("HTTP 503 unavailable"))
    task = _task()
    for ordinal in range(1, 4):
        running = begin_attempt(
            task,
            reason="initial" if ordinal == 1 else "retry",
            from_stage="transcript",
            now=now,
        )
        task = finish_attempt(
            running,
            status="failed",
            now=now,
            failed_stage="transcript",
            failure=failure,
        )

    assert next_retry_time(task, failure, now=now, stage="ocr") == now + timedelta(
        minutes=1
    )
    assert next_retry_time(task, failure, now=now, stage="transcript") is None


def test_attempt_lifecycle_appends_without_rewriting_previous_attempt() -> None:
    started = begin_attempt(
        _task(),
        reason="initial",
        from_stage="source",
        now=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
    )
    first_id = started.active_attempt_id

    completed = finish_attempt(
        started,
        status="completed",
        now=datetime(2026, 7, 12, 10, 1, tzinfo=UTC),
    )
    retried = begin_attempt(
        completed,
        reason="retry",
        from_stage="ocr",
        now=datetime(2026, 7, 12, 10, 2, tzinfo=UTC),
    )

    assert completed.attempts[0].status == "completed"
    assert retried.attempts[0] == completed.attempts[0]
    assert retried.attempts[1].ordinal == 2
    assert retried.active_attempt_id != first_id


def test_paid_attempt_start_interrupts_stale_owner_after_task_lock_recovery(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "task"
    stale = begin_attempt(
        _task(),
        reason="retry",
        from_stage="note",
        now=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
    )
    write_task_atomic(task_dir, stale)

    started = begin_persisted_attempt(
        task_dir,
        reason="retry",
        from_stage="note",
        now=datetime(2026, 7, 12, 10, 1, tzinfo=UTC),
    )

    assert started == load_task(task_dir)
    assert [attempt.status for attempt in started.attempts] == [
        "interrupted",
        "running",
    ]


def test_recovery_plan_selects_earliest_invalid_stage_and_marks_paid_boundary(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    (task_dir / "source.json").write_text("{}", encoding="utf-8")
    (task_dir / "process_report.md").write_text("# report", encoding="utf-8")
    task = _task(
        stages={
            "source": StageStatus.COMPLETED,
            "transcript": StageStatus.COMPLETED,
            "frames": StageStatus.PENDING,
        },
        artifacts={
            "source": ["source.json", "process_report.md"],
            "transcript": ["transcript.json", "transcript.md"],
        },
    )
    write_task_atomic(task_dir, task)

    plan = plan_recovery(task_dir)

    assert plan.from_stage == "transcript"
    assert "missing artifact" in plan.reasons[0]
    assert plan.requires_paid is False

    upstream = (
        "source",
        "transcript",
        "frames",
        "ocr",
        "evidence",
        "content_pack",
    )
    paid = task.model_copy(
        update={
            "stages": {
                "source": StageStatus.COMPLETED,
                "transcript": StageStatus.COMPLETED,
                "frames": StageStatus.COMPLETED,
                "ocr": StageStatus.COMPLETED,
                "evidence": StageStatus.COMPLETED,
                "content_pack": StageStatus.COMPLETED,
                "note": StageStatus.FAILED,
            },
            "artifacts": {stage: list(stage_artifacts(stage)) for stage in upstream},
        }
    )
    for paths in paid.artifacts.values():
        for path in paths:
            destination = task_dir / path
            if destination.suffix:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("{}", encoding="utf-8")
            else:
                destination.mkdir(parents=True, exist_ok=True)
    write_task_atomic(task_dir, paid)

    paid_plan = plan_recovery(task_dir)
    assert paid_plan.from_stage == "note"
    assert paid_plan.requires_paid is True


def test_persisted_attempt_cycles_preserve_created_and_first_completed_time(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "task"
    created = datetime(2026, 9, 7, 2, 30, tzinfo=UTC)
    completed_at = datetime(2026, 9, 7, 3, 0, tzinfo=UTC)
    retry_start = datetime(2026, 9, 7, 3, 10, tzinfo=UTC)
    retry_finish_write = datetime(2026, 9, 7, 3, 20, tzinfo=UTC)
    task = create_task(
        task_id="20260907-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        now=created,
    )
    write_task_atomic(task_dir, task, now=created)
    write_task_atomic(
        task_dir,
        complete_task_goal(load_task(task_dir), now=completed_at),
        now=completed_at,
    )

    begin_persisted_attempt(
        task_dir, reason="retry", from_stage="note", now=retry_start
    )
    completed = complete_persisted_attempt(task_dir, now=retry_finish_write)
    write_task_atomic(task_dir, completed, now=retry_finish_write)

    persisted = load_task(task_dir)
    assert persisted.created_at == created
    assert persisted.completed_at == completed_at
    assert persisted.updated_at == retry_finish_write
