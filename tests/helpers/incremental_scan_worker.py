from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import learnnest.batch as batch_module
import learnnest.schedules as schedule_module
from learnnest.adapters.folder import FolderAdapter
from learnnest.locks import LockUnavailable
from learnnest.models import StageStatus, TaskRecord
from learnnest.scanning import run_incremental_scan
from learnnest.schedule_store import load_schedule, schedule_path


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _record(source, profile, marker: Path) -> TaskRecord:
    return TaskRecord(
        task_id=f"subprocess-{marker.stem}",
        source_path=source.input,
        source_fingerprint=f"subprocess-{marker.stem}",
        title=Path(source.input).stem,
        profile=profile,
        stages={"content_pack": StageStatus.COMPLETED},
    )


def _concurrent_hold(arguments: list[str]) -> None:
    folder, root, go, ready, release, advanced, result = map(Path, arguments[:7])
    now = datetime.fromisoformat(arguments[7])

    def processor(source, output_root, profile):
        del output_root
        advanced.write_text("advanced", encoding="utf-8")
        ready.write_text("ready", encoding="utf-8")
        _wait_for(release)
        return _record(source, profile, advanced)

    _wait_for(go)
    try:
        manifest = run_incremental_scan(
            FolderAdapter(folder),
            root,
            processor=processor,
            now=now,
        )
    except LockUnavailable:
        payload = {"status": "blocked"}
    else:
        payload = {"status": manifest.status, "batch_id": manifest.batch_id}
    result.write_text(json.dumps(payload), encoding="utf-8")


def _kill_before_claim(arguments: list[str]) -> None:
    folder, root, ready = map(Path, arguments[:3])
    now = datetime.fromisoformat(arguments[3])
    original_write = batch_module.write_batch_atomic

    def write_then_hold(batch_dir, manifest):
        destination = original_write(batch_dir, manifest)
        if not ready.exists() and manifest.schedule_id is not None:
            ready.write_text("batch-written", encoding="utf-8")
            _wait_for(ready.with_suffix(".never"), timeout=300.0)
        return destination

    batch_module.write_batch_atomic = write_then_hold
    run_incremental_scan(FolderAdapter(folder), root, now=now)


def _kill_before_commit(arguments: list[str]) -> None:
    folder, root, ready, advanced = map(Path, arguments[:4])
    now = datetime.fromisoformat(arguments[4])

    def processor(source, output_root, profile):
        del output_root
        advanced.write_text("advanced", encoding="utf-8")
        return _record(source, profile, advanced)

    def hold_commit(*args, **kwargs):
        del args, kwargs
        ready.write_text("terminal-before-commit", encoding="utf-8")
        _wait_for(ready.with_suffix(".never"), timeout=300.0)

    schedule_module._commit_manifest = hold_commit
    run_incremental_scan(
        FolderAdapter(folder),
        root,
        processor=processor,
        now=now,
    )


def _resume(arguments: list[str]) -> None:
    folder, root, advanced, result = map(Path, arguments[:4])
    expected_batch_id = arguments[4]
    now = datetime.fromisoformat(arguments[5])
    adapter = FolderAdapter(folder)

    def processor(source, output_root, profile):
        del output_root
        current = load_schedule(schedule_path(root, adapter.scan_key))
        if current.active_batch_id != expected_batch_id:
            raise AssertionError("orphan batch was not claimed before resume")
        advanced.write_text("advanced", encoding="utf-8")
        return _record(source, profile, advanced)

    manifest = run_incremental_scan(
        adapter,
        root,
        processor=processor,
        now=now,
    )
    result.write_text(
        json.dumps({"status": manifest.status, "batch_id": manifest.batch_id}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    if mode == "concurrent-hold":
        _concurrent_hold(args)
    elif mode == "kill-before-claim":
        _kill_before_claim(args)
    elif mode == "kill-before-commit":
        _kill_before_commit(args)
    elif mode == "resume":
        _resume(args)
    else:
        raise ValueError(f"unknown worker mode: {mode}")
