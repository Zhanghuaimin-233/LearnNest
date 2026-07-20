from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from learnnest.locks import (
    batch_lock,
    rebuild_lock,
    resource_slot,
    schedule_lock,
    task_lock,
)


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _hold_task(args: list[str]) -> None:
    root, task_id, ready, release = Path(args[0]), args[1], Path(args[2]), Path(args[3])
    with task_lock(root, task_id, timeout=2.0):
        ready.write_text("ready", encoding="utf-8")
        _wait_for(release)


def _hold_batch(args: list[str]) -> None:
    root, batch_id, ready, release = (
        Path(args[0]),
        args[1],
        Path(args[2]),
        Path(args[3]),
    )
    with batch_lock(root, batch_id, timeout=2.0):
        ready.write_text("ready", encoding="utf-8")
        _wait_for(release)


def _hold_schedule(args: list[str]) -> None:
    root, schedule_id, ready, release = (
        Path(args[0]),
        args[1],
        Path(args[2]),
        Path(args[3]),
    )
    with schedule_lock(root, schedule_id, timeout=2.0):
        ready.write_text("ready", encoding="utf-8")
        _wait_for(release)


def _hold_rebuild(args: list[str]) -> None:
    root, ready, release = Path(args[0]), Path(args[1]), Path(args[2])
    with rebuild_lock(root, timeout=2.0):
        ready.write_text("ready", encoding="utf-8")
        _wait_for(release)


def _hold_resource(args: list[str]) -> None:
    root, resource = Path(args[0]), args[1]
    start, result = Path(args[2]), Path(args[3])
    hold_seconds = float(args[4])
    _wait_for(start)
    with resource_slot(root, resource, timeout=10.0) as slot:
        entered = time.time_ns()
        time.sleep(hold_seconds)
        exited = time.time_ns()
    result.write_text(
        json.dumps({"entered": entered, "exited": exited, "slot": slot}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    mode, *arguments = sys.argv[1:]
    if mode == "task":
        _hold_task(arguments)
    elif mode == "batch":
        _hold_batch(arguments)
    elif mode == "schedule":
        _hold_schedule(arguments)
    elif mode == "rebuild":
        _hold_rebuild(arguments)
    elif mode == "resource":
        _hold_resource(arguments)
    else:
        raise ValueError(f"unknown worker mode: {mode}")
