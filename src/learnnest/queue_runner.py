"""Bounded foreground execution for the derived retryable failure queue."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from learnnest.execution import classify_failure
from learnnest.index import query_failure_queue, rebuild_index
from learnnest.pipeline import recover_task
from learnnest.task_store import find_task_by_id


@dataclass(frozen=True)
class QueueRunResult:
    task_id: str
    status: str
    error: str | None = None


def run_failure_queue(
    output_root: str | Path,
    *,
    workers: int = 2,
    max_items: int = 10,
    max_stage_attempts: int = 4,
    now: datetime | None = None,
    recoverer: Callable[..., Any] | None = None,
) -> list[QueueRunResult]:
    """Run due free recoveries in parallel; paid stages remain explicit failures."""
    if workers < 1:
        raise ValueError("workers must be positive")
    if max_items < 1:
        raise ValueError("max_items must be positive")
    root = Path(output_root).resolve()
    rebuild_index(root)
    rows = query_failure_queue(
        root,
        now=now,
        max_stage_attempts=max_stage_attempts,
    )[:max_items]
    selected_recoverer = recoverer or recover_task

    def execute(row: dict[str, Any]) -> QueueRunResult:
        task_id = str(row["task_id"])
        if row.get("failure_disposition") not in {None, "retryable"}:
            return QueueRunResult(task_id, "blocked", "non-retryable failure")
        linked = find_task_by_id(root, task_id)
        if linked is None:
            return QueueRunResult(task_id, "failed", "task fact is missing")
        try:
            selected_recoverer(
                linked[0],
                reason="retry",
                max_stage_attempts=max_stage_attempts,
            )
        except Exception as error:
            return QueueRunResult(task_id, "failed", _safe_error(error))
        return QueueRunResult(task_id, "completed")

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(execute, rows))
    finally:
        rebuild_index(root)


def _safe_error(error: Exception) -> str:
    return classify_failure(error).safe_summary
