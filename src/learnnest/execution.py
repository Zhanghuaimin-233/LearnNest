"""Attempt lifecycle, safe failure classification, and recovery planning."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from learnnest.execution_models import (
    AttemptReason,
    FailureInfo,
    TaskAttempt,
)
from learnnest.models import StageName, StageStatus, TaskRecord
from learnnest.stages import PAID_STAGES, STAGES, stage_artifacts
from learnnest.task_store import load_task, write_task_atomic

_RETRYABLE_PATTERN = re.compile(
    r"(?i)(HTTP\s*(?:408|409|425|429|5\d\d)\b|timeout|timed out|connection|temporar|resource busy)"
)
_TERMINAL_PATTERN = re.compile(
    r"(?i)(HTTP\s*(?:400|404|405|410|415|422)\b|unsupported|invalid (?:input|media|schema|evidence))"
)
_MANUAL_PATTERN = re.compile(
    r"(?i)(api[_ ]?key.*missing|missing.*api[_ ]?key|login|cookie|permission|verification required|source file.*missing)"
)
_SENSITIVE_PATTERN = re.compile(
    r"(?i)(authorization\s*:|bearer\s+|api[_ -]?key\s*[:=]\s*\S+|cookie\s*:|sk-[a-z0-9_-]{8,})"
)


class RecoveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    from_stage: StageName | None
    reasons: list[str]
    requires_paid: bool


_RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
)
RETRY_DELAYS = _RETRY_DELAYS
MAX_STAGE_RETRIES = 3
MAX_STAGE_ATTEMPTS = MAX_STAGE_RETRIES + 1


class StageAttemptLimitError(RuntimeError):
    """A stage has exhausted its four persisted execution opportunities."""


def stage_attempt_count(task: TaskRecord, stage: StageName) -> int:
    """Count executions of one stage from the task's existing attempts."""
    count = 0
    for attempt in task.attempts:
        if attempt.executed_stages:
            count += stage in attempt.executed_stages
            continue
        count += stage in _legacy_attempt_stages(task, attempt)
    return count


def ensure_stage_attempts_available(
    task: TaskRecord,
    stages: tuple[StageName, ...],
    *,
    max_attempts: int = MAX_STAGE_ATTEMPTS,
) -> None:
    if not 1 <= max_attempts <= MAX_STAGE_ATTEMPTS:
        raise ValueError(f"max_attempts must be between 1 and {MAX_STAGE_ATTEMPTS}")
    exhausted = [
        stage for stage in stages if stage_attempt_count(task, stage) >= max_attempts
    ]
    if exhausted:
        raise StageAttemptLimitError(
            "stage attempt limit exhausted: " + ", ".join(exhausted)
        )


def record_stage_attempt(
    task_dir: str | Path, task: TaskRecord, stage: StageName
) -> TaskRecord:
    """Persist one stage execution in the active existing TaskAttempt."""
    active_id = task.active_attempt_id
    if active_id is None:
        raise ValueError("stage execution requires an active task attempt")
    if stage_attempt_count(task, stage) >= MAX_STAGE_ATTEMPTS:
        raise StageAttemptLimitError(f"stage attempt limit exhausted: {stage}")
    attempts = list(task.attempts)
    index = next(
        (i for i, attempt in enumerate(attempts) if attempt.attempt_id == active_id),
        None,
    )
    if index is None:
        raise ValueError("active attempt is missing")
    current = attempts[index]
    if stage not in current.executed_stages:
        attempts[index] = current.model_copy(
            update={"executed_stages": [*current.executed_stages, stage]}
        )
    updated = TaskRecord.model_validate(
        task.model_copy(update={"attempts": attempts}).model_dump(mode="python")
    )
    write_task_atomic(task_dir, updated)
    return updated


def _legacy_attempt_stages(
    task: TaskRecord, attempt: TaskAttempt
) -> tuple[StageName, ...]:
    """Conservatively infer stage execution for pre-contract task records."""
    try:
        start = STAGES.index(attempt.from_stage)
    except ValueError:
        return ()
    if attempt.failed_stage is not None:
        end = STAGES.index(attempt.failed_stage)
    elif attempt.status == "completed":
        end = STAGES.index("content_pack" if task.profile == "evidence" else "publish")
    elif attempt.status == "running":
        return ()
    else:
        end = start
    return STAGES[start : end + 1]


def classify_failure(error: Exception) -> FailureInfo:
    raw = " ".join(str(error).split())
    safe_summary = (
        f"{type(error).__name__}"
        if _SENSITIVE_PATTERN.search(raw)
        else (raw or type(error).__name__)
    )[:500]
    if _MANUAL_PATTERN.search(raw):
        disposition: Literal["retryable", "manual", "terminal"] = "manual"
    elif _TERMINAL_PATTERN.search(raw):
        disposition = "terminal"
    elif _RETRYABLE_PATTERN.search(raw):
        disposition = "retryable"
    else:
        disposition = "manual"
    return FailureInfo(
        code=_failure_code(raw, disposition),
        category=_failure_category(raw),
        disposition=disposition,
        safe_summary=safe_summary,
    )


def next_retry_time(
    task: TaskRecord,
    failure: FailureInfo,
    *,
    now: datetime,
    stage: StageName | None = None,
) -> datetime | None:
    """Return the next bounded automatic retry time for one retryable failure."""
    if failure.disposition != "retryable":
        return None
    previous = sum(
        attempt.failure is not None
        and attempt.failure.disposition == "retryable"
        and attempt.status == "failed"
        and (stage is None or attempt.failed_stage == stage)
        for attempt in task.attempts
    )
    return now + _RETRY_DELAYS[previous] if previous < len(_RETRY_DELAYS) else None


def begin_attempt(
    task: TaskRecord,
    *,
    reason: AttemptReason,
    from_stage: StageName,
    now: datetime,
    batch_id: str | None = None,
) -> TaskRecord:
    if task.active_attempt_id is not None:
        raise ValueError("task already has an active attempt")
    ordinal = len(task.attempts) + 1
    attempt_id = (
        f"{now.astimezone(UTC):%Y%m%dT%H%M%S%fZ}-{ordinal:04d}-{task.task_id[-8:]}"
    )
    attempt = TaskAttempt(
        attempt_id=attempt_id,
        ordinal=ordinal,
        reason=reason,
        from_stage=from_stage,
        status="running",
        started_at=now,
        batch_id=batch_id,
    )
    return TaskRecord.model_validate(
        task.model_copy(
            update={
                "attempts": [*task.attempts, attempt],
                "active_attempt_id": attempt_id,
            }
        ).model_dump(mode="python")
    )


def finish_attempt(
    task: TaskRecord,
    *,
    status: Literal["completed", "failed", "interrupted", "skipped_duplicate"],
    now: datetime,
    failed_stage: StageName | None = None,
    failure: FailureInfo | None = None,
    next_retry_at: datetime | None = None,
) -> TaskRecord:
    active_id = task.active_attempt_id
    if active_id is None:
        raise ValueError("task has no active attempt")
    attempts = list(task.attempts)
    index = next(
        (i for i, attempt in enumerate(attempts) if attempt.attempt_id == active_id),
        None,
    )
    if index is None:
        raise ValueError("active attempt is missing")
    attempts[index] = attempts[index].model_copy(
        update={
            "status": status,
            "finished_at": now,
            "failed_stage": failed_stage,
            "failure": failure,
            "next_retry_at": next_retry_at,
        }
    )
    return TaskRecord.model_validate(
        task.model_copy(
            update={"attempts": attempts, "active_attempt_id": None}
        ).model_dump(mode="python")
    )


def plan_recovery(task_dir: str | Path) -> RecoveryPlan:
    root = Path(task_dir).resolve()
    task = load_task(root)
    for stage in STAGES:
        status = task.stages.get(stage)
        if status is None or status is StageStatus.SKIPPED:
            continue
        if status is StageStatus.RUNNING:
            return _plan(task, stage, f"stage is still marked running: {stage}")
        if status in {StageStatus.PENDING, StageStatus.FAILED}:
            return _plan(task, stage, f"stage is {status.value}: {stage}")
        if status is StageStatus.COMPLETED:
            declared = task.artifacts.get(stage)
            if declared is None:
                return _plan(task, stage, f"completed stage has no artifacts: {stage}")
            declared_names = {Path(artifact).name for artifact in declared}
            required_artifacts = stage_artifacts(stage)
            if stage == "publish" and "published_note.md" in declared_names:
                # Historical V2/V3 task records point at their old publish
                # snapshot; new compact tasks point at note.md instead.
                required_artifacts = ("published_note.md",)
            for required in required_artifacts:
                if required not in declared_names:
                    return _plan(
                        task,
                        stage,
                        f"completed stage is missing required artifact: {required}",
                    )
            for artifact in declared:
                candidate = Path(artifact)
                resolved = (root / candidate).resolve()
                if candidate.is_absolute() or not resolved.is_relative_to(root):
                    return _plan(task, stage, f"invalid artifact path: {artifact}")
                if not resolved.exists():
                    return _plan(task, stage, f"missing artifact: {artifact}")
    return RecoveryPlan(
        task_id=task.task_id,
        from_stage=None,
        reasons=[],
        requires_paid=False,
    )


def begin_persisted_attempt(
    task_dir: str | Path,
    *,
    reason: AttemptReason,
    from_stage: StageName,
    now: datetime,
) -> TaskRecord:
    root = Path(task_dir).resolve()
    task = load_task(root)
    if task.active_attempt_id is not None:
        task = finish_attempt(
            task,
            status="interrupted",
            now=now,
        )
    updated = begin_attempt(
        task,
        reason=reason,
        from_stage=from_stage,
        now=now,
    )
    write_task_atomic(root, updated)
    return updated


def complete_persisted_attempt(
    task_dir: str | Path,
    *,
    now: datetime,
) -> TaskRecord:
    root = Path(task_dir).resolve()
    task = load_task(root)
    if task.active_attempt_id is None:
        return task
    updated = finish_attempt(task, status="completed", now=now)
    write_task_atomic(root, updated)
    return updated


def fail_persisted_attempt(
    task_dir: str | Path,
    error: Exception,
    *,
    failed_stage: StageName,
    now: datetime,
) -> TaskRecord:
    root = Path(task_dir).resolve()
    task = load_task(root)
    if task.active_attempt_id is None:
        return task
    failure = classify_failure(error)
    updated = finish_attempt(
        task,
        status="failed",
        now=now,
        failed_stage=failed_stage,
        failure=failure,
        next_retry_at=next_retry_time(task, failure, now=now, stage=failed_stage),
    ).model_copy(update={"error_summary": failure.safe_summary})
    write_task_atomic(root, updated)
    return updated


def _plan(task: TaskRecord, stage: StageName, reason: str) -> RecoveryPlan:
    return RecoveryPlan(
        task_id=task.task_id,
        from_stage=stage,
        reasons=[reason],
        requires_paid=stage in PAID_STAGES,
    )


def _failure_code(raw: str, disposition: str) -> str:
    match = re.search(r"(?i)HTTP\s*(\d{3})", raw)
    if match:
        return f"http_{match.group(1)}"
    return f"{disposition}_error"


def _failure_category(raw: str) -> str:
    if re.search(r"(?i)(HTTP|timeout|connection|api[_ ]?key)", raw):
        return "provider"
    if re.search(r"(?i)(file|path|permission)", raw):
        return "filesystem"
    return "pipeline"
