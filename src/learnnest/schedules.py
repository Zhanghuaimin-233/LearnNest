"""Foreground-only schedule orchestration over the existing batch runner."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from learnnest.batch import (
    resume_batch,
    resume_discovery_batch,
    run_batch,
    run_discovery_batch,
)
from learnnest.adapters.folder import FolderAdapter
from learnnest.batch_models import BatchManifest
from learnnest.batch_store import load_batch
from learnnest.execution import classify_failure
from learnnest.locks import LockUnavailable, schedule_lock
from learnnest.models import TaskProfile, TaskRecord
from learnnest.pipeline import process_source
from learnnest.schedule_models import ScheduleRecord
from learnnest.schedule_store import (
    list_schedules,
    load_schedule,
    schedule_path,
    write_schedule_atomic,
)
from learnnest.source_models import SourceItem


@dataclass(frozen=True)
class ScheduleOutcome:
    schedule_id: str
    status: str
    batch_id: str | None = None
    error: str | None = None


Processor = Callable[[SourceItem, Path, TaskProfile], TaskRecord]
AdapterFactory = Callable[[ScheduleRecord, SecretStr | None], Any]


def ensure_manual_folder_schedule(
    output_root: str | Path,
    adapter: FolderAdapter,
    *,
    profile: TaskProfile = "evidence",
    now: datetime | None = None,
) -> ScheduleRecord:
    """Create or validate the implicit cursor owner for `scan folder`."""
    root = Path(output_root).resolve()
    selected_time = now or datetime.now(UTC)
    destination = schedule_path(root, adapter.scan_key)
    expected_source = {
        "kind": "folder",
        "path": str(adapter.path),
        "recursive": adapter.recursive,
    }
    with schedule_lock(root, adapter.scan_key, timeout=0):
        if destination.is_file():
            existing = load_schedule(destination)
            if (
                existing.source.model_dump(mode="python") != expected_source
                or existing.trigger.kind != "manual"
                or existing.profile != profile
            ):
                raise ValueError(
                    "manual folder schedule identity conflicts with its fact"
                )
            return existing
        record = ScheduleRecord(
            schedule_id=adapter.scan_key,
            status="enabled",
            source=expected_source,
            trigger={"kind": "manual"},
            profile=profile,
            created_at=selected_time,
            updated_at=selected_time,
        )
        write_schedule_atomic(root, record)
        return record


def create_interval_folder_schedule(
    output_root: str | Path,
    schedule_id: str,
    path: str | Path,
    *,
    every_seconds: int,
    recursive: bool = False,
    profile: TaskProfile = "evidence",
    now: datetime | None = None,
) -> ScheduleRecord:
    """Persist one explicit foreground interval definition without running it."""
    root = Path(output_root).resolve()
    directory = Path(path).expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ValueError(f"schedule folder is not a directory: {directory}")
    destination = schedule_path(root, schedule_id)
    selected_time = now or datetime.now(UTC)
    with schedule_lock(root, schedule_id, timeout=0):
        if destination.exists():
            raise ValueError(f"schedule already exists: {schedule_id}")
        record = ScheduleRecord(
            schedule_id=schedule_id,
            status="enabled",
            source={
                "kind": "folder",
                "path": str(directory),
                "recursive": recursive,
            },
            trigger={"kind": "interval", "every_seconds": every_seconds},
            profile=profile,
            next_tick_at=selected_time,
            created_at=selected_time,
            updated_at=selected_time,
        )
        write_schedule_atomic(root, record)
        return record


def create_interval_douyin_schedule(
    output_root: str | Path,
    schedule_id: str,
    url: str,
    *,
    every_seconds: int,
    profile: TaskProfile = "evidence",
    now: datetime | None = None,
) -> ScheduleRecord:
    """Persist a foreground interval for the verified default-video scope."""
    root = Path(output_root).resolve()
    selected_time = now or datetime.now(UTC)
    destination = schedule_path(root, schedule_id)
    with schedule_lock(root, schedule_id, timeout=0):
        if destination.exists():
            raise ValueError(f"schedule already exists: {schedule_id}")
        record = ScheduleRecord(
            schedule_id=schedule_id,
            status="enabled",
            source={
                "kind": "douyin",
                "url": url,
                "include_default_video": True,
                "folder_ids": [],
            },
            trigger={"kind": "interval", "every_seconds": every_seconds},
            profile=profile,
            next_tick_at=selected_time,
            created_at=selected_time,
            updated_at=selected_time,
        )
        write_schedule_atomic(root, record)
        return record


def run_schedule_once(
    output_root: str | Path,
    schedule_id: str,
    *,
    adapter: Any | None = None,
    adapter_factory: AdapterFactory | None = None,
    processor: Processor = process_source,
    now: datetime | None = None,
    lock_timeout: float = 0.0,
    runtime_credentials: SecretStr | None = None,
    require_due: bool = False,
) -> ScheduleOutcome:
    """Discover or resume one schedule while serializing its cursor commit."""
    root = Path(output_root).resolve()
    requested_time = now or datetime.now(UTC)
    with schedule_lock(root, schedule_id, timeout=lock_timeout):
        record = load_schedule(schedule_path(root, schedule_id))
        selected_time = _logical_time(record, requested_time)
        if require_due and not _is_due(record, selected_time):
            return ScheduleOutcome(schedule_id, "skipped")
        linked = _linked_batch(root, record)
        if linked is not None:
            _validate_manifest_owner(record, linked[1])
            selected_time = _logical_time(record, selected_time, linked[1])
            if record.active_batch_id is None:
                record = _claim_active(root, record, linked[1], selected_time)
            manifest = _resume_or_reconcile(
                linked,
                processor=processor,
                now=selected_time,
                error_sanitizer=lambda error: _sanitize_error_text(
                    error, runtime_credentials
                ),
            )
            record = load_schedule(schedule_path(root, schedule_id))
            return _commit_manifest(root, record, manifest, selected_time)

        try:
            selected_adapter = adapter
            if adapter_factory is not None:
                selected_adapter = adapter_factory(record, runtime_credentials)
                latest = load_schedule(schedule_path(root, schedule_id))
                if latest != record:
                    selected_time = _logical_time(latest, selected_time)
                    if require_due and not _is_due(latest, selected_time):
                        return ScheduleOutcome(schedule_id, "skipped")
                    record = latest
                    selected_adapter = adapter_factory(record, runtime_credentials)
                    if load_schedule(schedule_path(root, schedule_id)) != record:
                        raise RuntimeError(
                            "schedule fact changed during adapter creation"
                        )
            if selected_adapter is None:
                raise ValueError("schedule adapter is required")
            discovery = selected_adapter.discover(record.cursor)
            _validate_discovery(discovery, runtime_credentials)
        except Exception as error:
            return _record_schedule_failure(
                root,
                record,
                error,
                selected_time,
                runtime_credentials,
            )

        def batch_started(batch_dir: Path, manifest: BatchManifest) -> None:
            del batch_dir
            current = load_schedule(schedule_path(root, schedule_id))
            logical_time = _logical_time(current, selected_time, manifest)
            _validate_manifest_owner(current, manifest)
            if (
                current.active_batch_id is not None
                and current.active_batch_id != manifest.batch_id
            ):
                raise RuntimeError("schedule already owns another active batch")
            claimed = current.model_copy(
                update={
                    "active_batch_id": manifest.batch_id,
                    "last_failure": None,
                    "updated_at": logical_time,
                }
            )
            write_schedule_atomic(root, claimed)

        batch_kind = "scan" if record.trigger.kind == "manual" else "scheduled"
        if getattr(selected_adapter, "discovery_only", False):
            if discovery.items and not discovery.links:
                raise RuntimeError(
                    "discovery-only adapter must return safe discovered links"
                )
            manifest = run_discovery_batch(
                discovery.items,
                discovery.links,
                root,
                record.profile,
                now=selected_time,
                kind=batch_kind,
                schedule_id=schedule_id,
                cursor_before=record.cursor,
                cursor_after=discovery.cursor_after,
                on_batch_started=batch_started,
            )
        else:
            manifest = run_batch(
                discovery.items,
                root,
                record.profile,
                processor=processor,
                now=selected_time,
                kind=batch_kind,
                schedule_id=schedule_id,
                cursor_before=record.cursor,
                cursor_after=discovery.cursor_after,
                on_batch_started=batch_started,
                error_sanitizer=lambda error: _sanitize_error_text(
                    error, runtime_credentials
                ),
            )
        current = load_schedule(schedule_path(root, schedule_id))
        return _commit_manifest(root, current, manifest, selected_time)


def tick_schedules(
    output_root: str | Path,
    *,
    adapter_factory: AdapterFactory,
    processor: Processor = process_source,
    runtime_credentials: SecretStr | None = None,
    now: datetime | None = None,
    lock_timeout: float = 0.0,
) -> list[ScheduleOutcome]:
    """Run due interval schedules once in the foreground, isolating each schedule."""
    root = Path(output_root).resolve()
    selected_time = now or datetime.now(UTC)
    outcomes: list[ScheduleOutcome] = []
    for record in list_schedules(root):
        if not _is_due(record, selected_time):
            continue
        try:
            outcome = run_schedule_once(
                root,
                record.schedule_id,
                adapter_factory=adapter_factory,
                processor=processor,
                now=selected_time,
                lock_timeout=lock_timeout,
                runtime_credentials=runtime_credentials,
                require_due=True,
            )
        except LockUnavailable as error:
            outcomes.append(
                ScheduleOutcome(record.schedule_id, "busy", error=str(error))
            )
            continue
        if outcome.status != "skipped":
            outcomes.append(outcome)
    return outcomes


def _is_due(record: ScheduleRecord, now: datetime) -> bool:
    if record.status != "enabled" or record.trigger.kind != "interval":
        return False
    return record.active_batch_id is not None or (
        record.next_tick_at is not None and record.next_tick_at <= now
    )


def _linked_batch(
    root: Path, record: ScheduleRecord
) -> tuple[Path, BatchManifest] | None:
    if record.active_batch_id is not None:
        directory = root / "视频学习批次" / record.active_batch_id
        if (directory / "batch.json").is_file():
            return directory, load_batch(directory)
        raise RuntimeError(f"active batch fact is missing: {record.active_batch_id}")
    candidates: list[tuple[Path, BatchManifest]] = []
    batch_root = root / "视频学习批次"
    if batch_root.is_dir():
        for path in batch_root.glob("*/batch.json"):
            try:
                manifest = load_batch(path)
            except (OSError, ValueError):
                continue
            if manifest.schedule_id == record.schedule_id and manifest.status in {
                "running",
                "interrupted",
            }:
                candidates.append((path.parent, manifest))
    if not candidates:
        return None
    if len(candidates) > 1:
        raise RuntimeError(
            f"multiple unfinished batches for schedule: {record.schedule_id}"
        )
    return candidates[0]


def _resume_or_reconcile(
    linked: tuple[Path, BatchManifest],
    *,
    processor: Processor,
    now: datetime,
    error_sanitizer: Callable[[Exception], str],
) -> BatchManifest:
    directory, manifest = linked
    if manifest.purpose == "discovery":
        return resume_discovery_batch(directory, now=now)
    if manifest.status in {"completed", "partial"}:
        return manifest
    return resume_batch(
        directory,
        processor=processor,
        now=now,
        error_sanitizer=error_sanitizer,
    )


def _commit_manifest(
    root: Path,
    record: ScheduleRecord,
    manifest: BatchManifest,
    now: datetime,
) -> ScheduleOutcome:
    _validate_manifest_owner(record, manifest)
    if manifest.status not in {"completed", "partial"}:
        return ScheduleOutcome(
            schedule_id=record.schedule_id,
            status=manifest.status,
            batch_id=manifest.batch_id,
        )
    updated = record.model_copy(
        update={
            "cursor": manifest.cursor_after,
            "active_batch_id": None,
            "last_batch_id": manifest.batch_id,
            "last_tick_at": now,
            "next_tick_at": _next_tick(record, now),
            "last_failure": None,
            "updated_at": now,
        }
    )
    write_schedule_atomic(root, updated)
    return ScheduleOutcome(
        schedule_id=record.schedule_id,
        status=manifest.status,
        batch_id=manifest.batch_id,
    )


def _next_tick(record: ScheduleRecord, now: datetime) -> datetime | None:
    if record.trigger.kind == "manual":
        return None
    return now + timedelta(seconds=record.trigger.every_seconds)


def _sanitize_error_text(
    error: Exception,
    runtime_credentials: SecretStr | None,
) -> str:
    message = " ".join(str(error).split()) or type(error).__name__
    if runtime_credentials is not None:
        secret = runtime_credentials.get_secret_value()
        if secret:
            message = message.replace(secret, "***")
    return message[:500]


def _record_schedule_failure(
    root: Path,
    record: ScheduleRecord,
    error: Exception,
    now: datetime,
    runtime_credentials: SecretStr | None,
) -> ScheduleOutcome:
    safe_error = RuntimeError(_sanitize_error_text(error, runtime_credentials))
    failure = classify_failure(safe_error)
    blocked = failure.disposition == "manual"
    updated = record.model_copy(
        update={
            "status": "blocked" if blocked else record.status,
            "last_failure": failure,
            "last_tick_at": now,
            "next_tick_at": _next_tick(record, now),
            "updated_at": now,
        }
    )
    write_schedule_atomic(root, updated)
    return ScheduleOutcome(
        schedule_id=record.schedule_id,
        status="blocked" if blocked else "failed",
        error=failure.safe_summary,
    )


def _validate_discovery(discovery: Any, credentials: SecretStr | None) -> None:
    if credentials is None:
        return
    secret = credentials.get_secret_value()
    if not secret:
        return
    links = getattr(discovery, "links", [])
    serialized_sources = json.dumps(
        {
            "sources": [item.model_dump(mode="json") for item in discovery.items],
            "links": [link.model_dump(mode="json") for link in links],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    if secret in discovery.cursor_after or secret in serialized_sources:
        raise RuntimeError("adapter output contains runtime credentials")


def _validate_manifest_owner(
    record: ScheduleRecord,
    manifest: BatchManifest,
) -> None:
    expected_kind = "scan" if record.trigger.kind == "manual" else "scheduled"
    if (
        record.active_batch_id is not None
        and manifest.batch_id != record.active_batch_id
    ):
        raise RuntimeError("active batch identity does not match schedule fact")
    if manifest.schedule_id != record.schedule_id:
        raise RuntimeError("active batch belongs to another schedule")
    if manifest.kind != expected_kind:
        raise RuntimeError("active batch kind does not match schedule trigger")
    if manifest.cursor_before != record.cursor:
        raise RuntimeError("active batch cursor base does not match schedule cursor")


def _claim_active(
    root: Path,
    record: ScheduleRecord,
    manifest: BatchManifest,
    now: datetime,
) -> ScheduleRecord:
    claimed = record.model_copy(
        update={"active_batch_id": manifest.batch_id, "updated_at": now}
    )
    write_schedule_atomic(root, claimed)
    return claimed


def _logical_time(
    record: ScheduleRecord,
    requested: datetime,
    manifest: BatchManifest | None = None,
) -> datetime:
    values = [
        requested.astimezone(UTC),
        record.created_at.astimezone(UTC),
        record.updated_at.astimezone(UTC),
    ]
    if record.last_tick_at is not None:
        values.append(record.last_tick_at.astimezone(UTC))
    if manifest is not None:
        values.extend(
            [
                manifest.created_at.astimezone(UTC),
                manifest.updated_at.astimezone(UTC),
            ]
        )
        if manifest.finished_at is not None:
            values.append(manifest.finished_at.astimezone(UTC))
    return max(values)
