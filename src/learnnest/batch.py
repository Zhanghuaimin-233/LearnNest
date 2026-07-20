"""Serial, failure-isolated batch orchestration for normalized sources."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from learnnest.batch_models import BatchItemResult, BatchKind, BatchManifest
from learnnest.batch_store import load_batch, write_batch_atomic
from learnnest.discovery_models import DiscoveredLink
from learnnest.discovery_store import persist_discovery_links
from learnnest.execution import classify_failure
from learnnest.locks import LockUnavailable, batch_lock
from learnnest.models import TaskProfile, TaskRecord
from learnnest.pipeline import process_source, recover_task
from learnnest.publication import atomic_replace_bytes
from learnnest.source_models import SourceItem
from learnnest.sources import source_item_fingerprint
from learnnest.task_store import find_task_by_fingerprint, find_task_by_id


def run_batch(
    sources: Sequence[SourceItem],
    output_root: Path,
    profile: TaskProfile = "evidence",
    *,
    processor: Callable[[SourceItem, Path, TaskProfile], TaskRecord] = process_source,
    now: datetime | None = None,
    kind: BatchKind = "manual",
    schedule_id: str | None = None,
    cursor_before: str | None = None,
    cursor_after: str | None = None,
    downloader: object | None = None,
    on_batch_started: Callable[[Path, BatchManifest], None] | None = None,
    error_sanitizer: Callable[[Exception], str] | None = None,
) -> BatchManifest:
    """Run sources serially while atomically persisting a recoverable ledger."""
    selected_time = now or datetime.now(UTC)
    fingerprints = [source_item_fingerprint(source) for source in sources]
    batch_id = _batch_id(selected_time, fingerprints)
    root = output_root.resolve()
    results = [
        BatchItemResult(
            position=position,
            source=source,
            input=source.input,
            input_type=source.input_type,
            source_fingerprint=fingerprint,
            status="pending",
        )
        for position, (source, fingerprint) in enumerate(
            zip(sources, fingerprints, strict=True),
            start=1,
        )
    ]
    batch_dir = root / "视频学习批次" / batch_id
    manifest = BatchManifest(
        batch_id=batch_id,
        kind=kind,
        status="running",
        created_at=selected_time,
        updated_at=selected_time,
        profile=profile,
        schedule_id=schedule_id,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        results=results,
    )
    with batch_lock(root, batch_id, timeout=0):
        return _execute_manifest(
            manifest,
            batch_dir,
            root,
            processor,
            selected_time,
            downloader=downloader,
            on_batch_started=on_batch_started,
            error_sanitizer=error_sanitizer,
        )


def resume_batch(
    batch_dir: str | Path,
    *,
    processor: Callable[[SourceItem, Path, TaskProfile], TaskRecord] = process_source,
    now: datetime | None = None,
    lock_timeout: float = 0.0,
    downloader: object | None = None,
    error_sanitizer: Callable[[Exception], str] | None = None,
) -> BatchManifest:
    """Resume one persisted batch without repeating final items."""
    directory = Path(batch_dir).resolve()
    manifest = load_batch(directory)
    if manifest.purpose == "discovery":
        return resume_discovery_batch(directory, now=now, lock_timeout=lock_timeout)
    root = directory.parents[1]
    selected_time = now or datetime.now(UTC)
    with batch_lock(root, manifest.batch_id, timeout=lock_timeout):
        current = load_batch(directory)
        if current.status == "completed":
            return current
        running = current.model_copy(
            update={
                "status": "running",
                "updated_at": selected_time,
                "finished_at": None,
            }
        )
        return _execute_manifest(
            running,
            directory,
            root,
            processor,
            selected_time,
            downloader=downloader,
            error_sanitizer=error_sanitizer,
        )


def run_discovery_batch(
    sources: Sequence[SourceItem],
    links: Sequence[DiscoveredLink],
    output_root: Path,
    profile: TaskProfile = "evidence",
    *,
    now: datetime | None = None,
    kind: BatchKind = "scheduled",
    schedule_id: str,
    cursor_before: str | None = None,
    cursor_after: str | None = None,
    on_batch_started: Callable[[Path, BatchManifest], None] | None = None,
) -> BatchManifest:
    """Persist discovered links as a completed discovery batch without downloading."""
    selected_time = now or datetime.now(UTC)
    fingerprints = [source_item_fingerprint(source) for source in sources]
    batch_id = _batch_id(selected_time, fingerprints)
    root = output_root.resolve()
    results = [
        BatchItemResult(
            position=position,
            source=source,
            input=source.input,
            input_type=source.input_type,
            source_fingerprint=fingerprint,
            status="pending",
        )
        for position, (source, fingerprint) in enumerate(
            zip(sources, fingerprints, strict=True),
            start=1,
        )
    ]
    batch_dir = root / "视频学习批次" / batch_id
    manifest = BatchManifest(
        batch_id=batch_id,
        kind=kind,
        purpose="discovery",
        status="running",
        created_at=selected_time,
        updated_at=selected_time,
        profile=profile,
        schedule_id=schedule_id,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        discovery_links=list(links),
        results=results,
    )
    with batch_lock(root, batch_id, timeout=0):
        write_batch_atomic(batch_dir, manifest)
        if on_batch_started is not None:
            on_batch_started(batch_dir, manifest)
        try:
            persist_discovery_links(
                root,
                schedule_id,
                manifest.discovery_links,
                now=selected_time,
            )
        except BaseException:
            interrupted = manifest.model_copy(
                update={
                    "status": "interrupted",
                    "updated_at": selected_time,
                    "finished_at": selected_time,
                }
            )
            write_batch_atomic(batch_dir, interrupted)
            _write_batch_report(batch_dir, interrupted)
            raise
        completed = manifest.model_copy(
            update={
                "status": "completed",
                "updated_at": selected_time,
                "finished_at": selected_time,
                "results": [
                    item.model_copy(update={"status": "completed"}) for item in results
                ],
            }
        )
        write_batch_atomic(batch_dir, completed)
        _write_batch_report(batch_dir, completed)
        return completed


def resume_discovery_batch(
    batch_dir: str | Path,
    *,
    now: datetime | None = None,
    lock_timeout: float = 0.0,
) -> BatchManifest:
    """Resume an interrupted discovery batch by replaying its atomic ledger write."""
    directory = Path(batch_dir).resolve()
    root = directory.parents[1]
    selected_time = now or datetime.now(UTC)
    with batch_lock(root, load_batch(directory).batch_id, timeout=lock_timeout):
        current = load_batch(directory)
        if current.purpose != "discovery":
            raise ValueError("batch is not a discovery batch")
        if current.status == "completed":
            return current
        running = current.model_copy(
            update={
                "status": "running",
                "updated_at": selected_time,
                "finished_at": None,
            }
        )
        write_batch_atomic(directory, running)
        try:
            if current.schedule_id is None:
                raise ValueError("discovery batch has no schedule owner")
            persist_discovery_links(
                root,
                current.schedule_id,
                current.discovery_links,
                now=selected_time,
            )
        except BaseException:
            interrupted = running.model_copy(
                update={
                    "status": "interrupted",
                    "updated_at": selected_time,
                    "finished_at": selected_time,
                }
            )
            write_batch_atomic(directory, interrupted)
            _write_batch_report(directory, interrupted)
            raise
        completed = running.model_copy(
            update={
                "status": "completed",
                "updated_at": selected_time,
                "finished_at": selected_time,
                "results": [
                    item.model_copy(update={"status": "completed"})
                    for item in running.results
                ],
            }
        )
        write_batch_atomic(directory, completed)
        _write_batch_report(directory, completed)
        return completed


def _execute_manifest(
    manifest: BatchManifest,
    batch_dir: Path,
    root: Path,
    processor: Callable[[SourceItem, Path, TaskProfile], TaskRecord],
    selected_time: datetime,
    *,
    downloader: object | None = None,
    on_batch_started: Callable[[Path, BatchManifest], None] | None = None,
    error_sanitizer: Callable[[Exception], str] | None = None,
) -> BatchManifest:
    results = list(manifest.results)
    seen = {
        item.source_fingerprint
        for item in results
        if item.status in {"completed", "skipped_duplicate"}
    }
    manifest = manifest.model_copy(
        update={
            "status": "running",
            "updated_at": selected_time,
            "finished_at": None,
            "results": results,
        }
    )
    write_batch_atomic(batch_dir, manifest)
    if on_batch_started is not None:
        on_batch_started(batch_dir, manifest)
    try:
        for index, item in enumerate(results):
            if item.status in {"completed", "skipped_duplicate"}:
                continue
            if item.status == "failed" and (
                item.failure is None or item.failure.disposition != "retryable"
            ):
                continue
            source = item.source
            fingerprint = item.source_fingerprint
            was_running = item.status == "running"
            results[index] = item.model_copy(update={"status": "running"})
            manifest = _persist_progress(batch_dir, manifest, results, selected_time)
            linked = None
            if processor is process_source:
                if item.task_id is not None:
                    linked = find_task_by_id(root, item.task_id)
                elif was_running:
                    linked = find_task_by_fingerprint(root, fingerprint)
            historical = (
                find_task_by_fingerprint(
                    root,
                    fingerprint,
                    require_completed_content_pack=True,
                )
                if linked is None
                else None
            )
            if linked is None and (fingerprint in seen or historical is not None):
                results[index] = results[index].model_copy(
                    update={
                        "status": "skipped_duplicate",
                        "task_id": (
                            historical[1].task_id if historical is not None else None
                        ),
                        "error": None,
                        "failure": None,
                    }
                )
                seen.add(fingerprint)
                manifest = _persist_progress(
                    batch_dir, manifest, results, selected_time
                )
                continue
            seen.add(fingerprint)

            def attempt_started(task: TaskRecord) -> None:
                nonlocal manifest
                results[index] = results[index].model_copy(
                    update={
                        "task_id": task.task_id,
                        "attempt_id": task.active_attempt_id,
                    }
                )
                manifest = _persist_progress(
                    batch_dir, manifest, results, selected_time
                )

            try:
                if linked is not None:
                    linked_dir, _ = linked
                    task = recover_task(
                        linked_dir,
                        downloader=downloader,
                        batch_id=manifest.batch_id,
                        on_attempt_started=attempt_started,
                    )
                elif processor is process_source:
                    task = processor(
                        source,
                        root,
                        manifest.profile,
                        downloader=downloader,
                        batch_id=manifest.batch_id,
                        on_attempt_started=attempt_started,
                        initial_attempt_reason=(
                            "scheduled" if manifest.kind == "scheduled" else "initial"
                        ),
                    )
                else:
                    task = processor(source, root, manifest.profile)
            except LockUnavailable:
                raise
            except Exception as error:
                safe_error = (
                    error_sanitizer(error)
                    if error_sanitizer is not None
                    else _safe_batch_error(error)
                )
                persisted = find_task_by_fingerprint(root, fingerprint)
                results[index] = results[index].model_copy(
                    update={
                        "status": "failed",
                        "task_id": (
                            persisted[1].task_id if persisted is not None else None
                        ),
                        "error": safe_error,
                        "failure": classify_failure(RuntimeError(safe_error)),
                    }
                )
                manifest = _persist_progress(
                    batch_dir, manifest, results, selected_time
                )
                continue
            actual_task_id = results[index].task_id
            if actual_task_id is not None and actual_task_id != task.task_id:
                actual = find_task_by_id(root, actual_task_id)
                if actual is not None:
                    actual_task = actual[1]
                    actual_attempt = (
                        actual_task.attempts[-1] if actual_task.attempts else None
                    )
                    if (
                        actual_task.duplicate_of_task_id == task.task_id
                        and actual_attempt is not None
                        and actual_attempt.status == "skipped_duplicate"
                    ):
                        results[index] = results[index].model_copy(
                            update={
                                "status": "skipped_duplicate",
                                "task_id": actual_task.task_id,
                                "attempt_id": actual_attempt.attempt_id,
                                "error": None,
                                "failure": None,
                            }
                        )
                        manifest = _persist_progress(
                            batch_dir, manifest, results, selected_time
                        )
                        continue
            results[index] = results[index].model_copy(
                update={
                    "status": "completed",
                    "task_id": task.task_id,
                    "attempt_id": (
                        task.attempts[-1].attempt_id if task.attempts else None
                    ),
                    "error": None,
                    "failure": None,
                }
            )
            manifest = _persist_progress(batch_dir, manifest, results, selected_time)
    except BaseException:
        interrupted = manifest.model_copy(
            update={
                "status": "interrupted",
                "updated_at": selected_time,
                "finished_at": selected_time,
                "results": list(results),
            }
        )
        write_batch_atomic(batch_dir, interrupted)
        _write_batch_report(batch_dir, interrupted)
        raise

    manifest = manifest.model_copy(
        update={
            "status": "partial"
            if any(item.status == "failed" for item in results)
            else "completed",
            "updated_at": selected_time,
            "finished_at": selected_time,
            "results": list(results),
        }
    )
    write_batch_atomic(batch_dir, manifest)
    _write_batch_report(batch_dir, manifest)
    return manifest


def _persist_progress(
    batch_dir: Path,
    manifest: BatchManifest,
    results: list[BatchItemResult],
    selected_time: datetime,
) -> BatchManifest:
    updated = manifest.model_copy(
        update={"updated_at": selected_time, "results": list(results)}
    )
    write_batch_atomic(batch_dir, updated)
    return updated


def _write_batch_report(batch_dir: Path, manifest: BatchManifest) -> None:
    atomic_replace_bytes(
        batch_dir / "batch_report.md",
        _render_batch_report(manifest).encode("utf-8"),
    )


def _batch_id(timestamp: datetime, fingerprints: list[str]) -> str:
    material = "\0".join(fingerprints).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:8]
    invocation = uuid4().hex[:8]
    return f"{timestamp.astimezone(UTC):%Y%m%dT%H%M%SZ}-{digest}-{invocation}"


def _safe_batch_error(error: Exception) -> str:
    return classify_failure(error).safe_summary


def _render_batch_report(manifest: BatchManifest) -> str:
    lines = [
        f"# 批次报告：{manifest.batch_id}",
        "",
        f"- profile: {manifest.profile}",
        f"- completed: {manifest.completed_count}",
        f"- failed: {manifest.failed_count}",
        f"- skipped: {manifest.skipped_count}",
        "",
        "## 任务",
        "",
    ]
    for index, result in enumerate(manifest.results, start=1):
        line = f"{index}. `{result.status}` {result.input}"
        if result.task_id is not None:
            line += f" ({result.task_id})"
        lines.append(line)
        if result.error is not None:
            lines.append(f"   - {result.error}")
    return "\n".join(lines) + "\n"
