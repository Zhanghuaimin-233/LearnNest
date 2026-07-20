from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from learnnest.adapters.folder import FolderAdapter
from learnnest.locks import LockUnavailable
from learnnest.models import StageStatus, TaskRecord
from learnnest.schedules import tick_schedules


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _tick(arguments: list[str], *, hold: bool) -> None:
    root, ready, release, advanced, result = map(Path, arguments[:5])
    now = datetime.fromisoformat(arguments[5])

    def adapter_factory(record, credentials):
        del credentials
        return FolderAdapter(record.source.path, recursive=record.source.recursive)

    def processor(source, output_root, profile):
        del output_root
        advanced.write_text("advanced", encoding="utf-8")
        if hold:
            ready.write_text("ready", encoding="utf-8")
            _wait_for(release)
        return TaskRecord(
            task_id=f"schedule-{advanced.stem}",
            source_path=source.input,
            source_fingerprint=f"schedule-{advanced.stem}",
            title=Path(source.input).stem,
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )

    try:
        outcomes = tick_schedules(
            root,
            adapter_factory=adapter_factory,
            processor=processor,
            lock_timeout=0.0,
            now=now,
        )
    except LockUnavailable:
        payload = {"status": "blocked"}
    else:
        payload = {
            "status": outcomes[0].status if outcomes else "not-due",
            "batch_id": outcomes[0].batch_id if outcomes else None,
        }
    result.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    if mode == "hold":
        _tick(args, hold=True)
    elif mode == "run":
        _tick(args, hold=False)
    else:
        raise ValueError(f"unknown worker mode: {mode}")
