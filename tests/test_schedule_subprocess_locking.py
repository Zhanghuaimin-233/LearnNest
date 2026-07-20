from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from learnnest.adapters.folder import FolderAdapter
from learnnest.schedule_models import ScheduleRecord
from learnnest.schedule_store import load_schedule, schedule_path, write_schedule_atomic

_WORKER = Path(__file__).parent / "helpers" / "schedule_tick_worker.py"


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


def _spawn(*arguments: str | Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, str(_WORKER), *(str(argument) for argument in arguments)],
        cwd=Path(__file__).parents[1],
        env=_environment(),
    )


def _batch_dirs(root: Path) -> list[Path]:
    return sorted(path.parent for path in (root / "视频学习批次").glob("*/batch.json"))


def test_two_tick_processes_allow_only_one_batch_and_cursor_commit(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "videos"
    folder.mkdir()
    (folder / "lesson.mp4").write_bytes(b"schedule-lock-video")
    root = tmp_path / "vault"
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    schedule = ScheduleRecord(
        schedule_id="folder-concurrent",
        status="enabled",
        source={"kind": "folder", "path": str(folder), "recursive": False},
        trigger={"kind": "interval", "every_seconds": 300},
        profile="evidence",
        next_tick_at=now,
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(root, schedule)

    first_ready = tmp_path / "first-ready"
    release_first = tmp_path / "release-first"
    first_advanced = tmp_path / "first-advanced"
    second_advanced = tmp_path / "second-advanced"
    first_result = tmp_path / "first-result.json"
    second_result = tmp_path / "second-result.json"
    first = _spawn(
        "hold",
        root,
        first_ready,
        release_first,
        first_advanced,
        first_result,
        now.isoformat(),
    )
    second: subprocess.Popen[bytes] | None = None
    try:
        _wait_for(first_ready)
        first_batches = _batch_dirs(root)
        assert len(first_batches) == 1

        second = _spawn(
            "run",
            root,
            tmp_path / "unused-ready",
            tmp_path / "unused-release",
            second_advanced,
            second_result,
            now.isoformat(),
        )
        second.wait(timeout=10)
        assert second.returncode == 0
        assert json.loads(second_result.read_text(encoding="utf-8")) == {
            "status": "busy",
            "batch_id": None,
        }
        assert not second_advanced.exists()
        assert load_schedule(schedule_path(root, schedule.schedule_id)).cursor is None
        assert _batch_dirs(root) == first_batches

        release_first.write_text("release", encoding="utf-8")
        first.wait(timeout=10)
        assert first.returncode == 0
        first_payload = json.loads(first_result.read_text(encoding="utf-8"))
        assert first_payload["status"] == "completed"

        persisted = load_schedule(schedule_path(root, schedule.schedule_id))
        expected_cursor = FolderAdapter(folder).discover(None).cursor_after
        assert persisted.cursor == expected_cursor
        assert persisted.last_batch_id == first_payload["batch_id"]
        assert _batch_dirs(root) == first_batches
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
