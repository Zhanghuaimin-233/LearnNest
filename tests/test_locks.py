from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from learnnest.locks import (
    LockUnavailable,
    batch_lock,
    rebuild_lock,
    resource_slot,
    schedule_lock,
    task_lock,
)

_WORKER = Path(__file__).parent / "helpers" / "lock_worker.py"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (source_root, environment.get("PYTHONPATH")) if value
    )
    return environment


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.01)


def test_task_lock_blocks_another_process_and_releases_after_termination(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    process = subprocess.Popen(
        [
            sys.executable,
            str(_WORKER),
            "task",
            str(tmp_path),
            "task-1",
            str(ready),
            str(release),
        ],
        cwd=Path(__file__).parents[1],
        env=_environment(),
    )
    try:
        _wait_for(ready)
        with pytest.raises(LockUnavailable, match="task-1"):
            with task_lock(tmp_path, "task-1", timeout=0.1):
                pass
        process.terminate()
        process.wait(timeout=5)
        stale = (
            tmp_path
            / ".learnnest"
            / "locks"
            / "tasks"
            / f"{hashlib.sha256(b'task-1').hexdigest()}.lock"
        )
        assert stale.exists()
        with task_lock(tmp_path, "task-1", timeout=1.0):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize(
    ("resource", "expected_max"),
    [
        ("asr", 1),
        ("ocr", 1),
        ("llm", 1),
        ("tts", 1),
        ("network", 2),
        ("ffmpeg", 2),
    ],
)
def test_resource_slots_enforce_cross_process_limits(
    tmp_path: Path, resource: str, expected_max: int
) -> None:
    start = tmp_path / f"start-{resource}"
    results = [tmp_path / f"{resource}-{index}.json" for index in range(3)]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(_WORKER),
                "resource",
                str(tmp_path),
                resource,
                str(start),
                str(result),
                "0.25",
            ],
            cwd=Path(__file__).parents[1],
            env=_environment(),
        )
        for result in results
    ]
    try:
        start.write_text("start", encoding="utf-8")
        for process in processes:
            process.wait(timeout=10)
            assert process.returncode == 0
        intervals = [
            json.loads(result.read_text(encoding="utf-8")) for result in results
        ]
        events = sorted(
            [
                *((item["entered"], 1) for item in intervals),
                *((item["exited"], -1) for item in intervals),
            ],
            key=lambda event: (event[0], event[1]),
        )
        active = 0
        maximum = 0
        for _, delta in events:
            active += delta
            maximum = max(maximum, active)
        assert maximum == expected_max
        assert len({item["slot"] for item in intervals}) <= expected_max
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_unknown_resource_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown resource"):
        with resource_slot(tmp_path, "gpu", timeout=0):
            pass


@pytest.mark.parametrize("kind", ["batch", "schedule", "rebuild"])
def test_batch_and_rebuild_ownership_are_cross_process(
    tmp_path: Path, kind: str
) -> None:
    ready = tmp_path / f"{kind}-ready"
    release = tmp_path / f"{kind}-release"
    arguments = [str(tmp_path), str(ready), str(release)]
    if kind in {"batch", "schedule"}:
        arguments.insert(1, f"{kind}-1")
    process = subprocess.Popen(
        [sys.executable, str(_WORKER), kind, *arguments],
        cwd=Path(__file__).parents[1],
        env=_environment(),
    )
    try:
        _wait_for(ready)
        if kind == "batch":
            with pytest.raises(LockUnavailable, match="batch-1"):
                with batch_lock(tmp_path, "batch-1", timeout=0.1):
                    pass
        elif kind == "schedule":
            with pytest.raises(LockUnavailable, match="schedule-1"):
                with schedule_lock(tmp_path, "schedule-1", timeout=0.1):
                    pass
        else:
            with pytest.raises(LockUnavailable, match="rebuild"):
                with rebuild_lock(tmp_path, timeout=0.1):
                    pass
        release.write_text("release", encoding="utf-8")
        process.wait(timeout=5)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
