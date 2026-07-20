from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from learnnest.execution_models import FailureInfo, SourceIdentities, TaskAttempt
from learnnest.identities import normalize_local_source
from learnnest.models import TaskRecord
from learnnest.task_store import create_task, load_task


def _running_attempt() -> TaskAttempt:
    return TaskAttempt(
        attempt_id="attempt-0001",
        ordinal=1,
        reason="initial",
        from_stage="source",
        status="running",
        started_at=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
    )


def test_task_attempt_accepts_one_timezone_aware_running_attempt() -> None:
    attempt = _running_attempt()

    assert attempt.started_at.tzinfo is not None
    assert attempt.finished_at is None


def test_task_attempt_rejects_naive_time_and_incomplete_failure() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _running_attempt().model_copy(
            update={"started_at": datetime(2026, 7, 12, 10, 0)}
        ).model_validate(
            {
                **_running_attempt().model_dump(),
                "started_at": datetime(2026, 7, 12, 10, 0),
            }
        )

    with pytest.raises(ValidationError, match="failed attempt requires"):
        TaskAttempt(
            attempt_id="attempt-0001",
            ordinal=1,
            reason="retry",
            from_stage="transcript",
            status="failed",
            started_at=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
            finished_at=datetime(2026, 7, 12, 10, 1, tzinfo=UTC),
        )


def test_task_record_enforces_active_attempt_and_contiguous_ordinals() -> None:
    attempt = _running_attempt()
    task = TaskRecord(
        task_id="20260712-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        active_attempt_id=attempt.attempt_id,
        attempts=[attempt],
    )

    assert task.schema_version == "2.0"

    with pytest.raises(ValidationError, match="active_attempt_id"):
        TaskRecord(
            task_id=task.task_id,
            source_path=task.source_path,
            source_fingerprint=task.source_fingerprint,
            title=task.title,
            active_attempt_id="missing",
            attempts=[attempt],
        )

    with pytest.raises(ValidationError, match="contiguous"):
        TaskRecord(
            task_id=task.task_id,
            source_path=task.source_path,
            source_fingerprint=task.source_fingerprint,
            title=task.title,
            attempts=[
                attempt.model_copy(
                    update={
                        "ordinal": 2,
                        "status": "completed",
                        "finished_at": datetime(2026, 7, 12, 10, 1, tzinfo=UTC),
                    }
                )
            ],
        )


def test_legacy_task_load_migrates_in_memory_without_rewriting_disk(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": "1.0",
        "task_id": "20260711-a1b2c3d4",
        "source_path": "C:/videos/lesson.mp4",
        "source_fingerprint": "a1b2c3d4",
        "title": "lesson",
    }
    task_json = tmp_path / "task.json"
    original = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    task_json.write_text(original, encoding="utf-8", newline="\n")

    task = load_task(task_json)

    assert task.schema_version == "2.0"
    assert task.identities.normalized_source == normalize_local_source(
        "C:/videos/lesson.mp4"
    )
    assert task.attempts == []
    assert task_json.read_text(encoding="utf-8") == original


def test_new_task_uses_task_record_2_0_identity_contract() -> None:
    task = create_task(
        task_id="20260712-a1b2c3d4",
        source_path="https://example.com/watch?v=1",
        source_input="https://example.com/watch?v=1",
        source_type="url",
        source_fingerprint="a1b2c3d4",
        title="lesson",
    )

    assert task.schema_version == "2.0"
    assert task.identities == SourceIdentities(
        normalized_source="https://example.com/watch?v=1"
    )


def test_failure_info_rejects_secrets_in_safe_summary() -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        FailureInfo(
            code="provider_error",
            category="provider",
            disposition="manual",
            safe_summary="Authorization: Bearer secret-token",
        )
