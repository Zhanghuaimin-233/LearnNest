"""Incremental scan orchestration backed by recoverable batch manifests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from learnnest.adapters.base import SourceAdapter
from learnnest.adapters.folder import FolderAdapter
from learnnest.batch_models import BatchManifest
from learnnest.batch_store import load_batch
from learnnest.models import TaskProfile, TaskRecord
from learnnest.pipeline import process_source
from learnnest.schedule_store import schedule_path
from learnnest.schedules import ensure_manual_folder_schedule, run_schedule_once
from learnnest.source_models import SourceItem


def run_incremental_scan(
    adapter: SourceAdapter,
    output_root: str | Path,
    profile: TaskProfile = "evidence",
    *,
    processor: Callable[[SourceItem, Path, TaskProfile], TaskRecord] = process_source,
    now: datetime | None = None,
) -> BatchManifest:
    """Run a scan through its schedule-owned committed cursor and OS lock."""
    root = Path(output_root).resolve()
    if not schedule_path(root, adapter.scan_key).is_file():
        if not isinstance(adapter, FolderAdapter):
            raise ValueError("first incremental scan requires a FolderAdapter")
        ensure_manual_folder_schedule(root, adapter, profile=profile, now=now)
    outcome = run_schedule_once(
        root,
        adapter.scan_key,
        adapter=adapter,
        processor=processor,
        now=now,
    )
    if outcome.batch_id is None:
        raise RuntimeError("incremental scan did not create or resume a batch")
    return load_batch(root / "视频学习批次" / outcome.batch_id)
