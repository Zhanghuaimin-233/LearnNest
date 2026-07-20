from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from learnnest.adapters.folder import FolderAdapter
from learnnest.batch_store import load_batch
from learnnest.schedule_store import load_schedule, schedule_path

_WORKER = Path(__file__).parent / "helpers" / "incremental_scan_worker.py"


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


def _wait_for_one(paths: tuple[Path, ...], timeout: float = 10.0) -> Path:
    deadline = time.monotonic() + timeout
    while True:
        existing = [path for path in paths if path.exists()]
        if existing:
            return existing[0]
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for one of {paths}")
        time.sleep(0.01)


def _spawn(*arguments: str | Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, str(_WORKER), *(str(argument) for argument in arguments)],
        cwd=Path(__file__).parents[1],
        env=_environment(),
    )


def _batch_dirs(root: Path) -> list[Path]:
    return sorted(path.parent for path in (root / "视频学习批次").glob("*/batch.json"))


def _video_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "videos"
    folder.mkdir()
    (folder / "lesson.mp4").write_bytes(b"scan-subprocess-video")
    return folder


def test_two_first_incremental_scans_create_and_advance_only_one_batch(
    tmp_path: Path,
) -> None:
    folder = _video_folder(tmp_path)
    root = tmp_path / "vault"
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    go = tmp_path / "go"
    ready = tmp_path / "winner-ready"
    release = tmp_path / "release-winner"
    results = (tmp_path / "first.json", tmp_path / "second.json")
    advanced = (tmp_path / "first-advanced", tmp_path / "second-advanced")
    processes = [
        _spawn(
            "concurrent-hold",
            folder,
            root,
            go,
            ready,
            release,
            advanced[index],
            results[index],
            now.isoformat(),
        )
        for index in range(2)
    ]
    try:
        go.write_text("go", encoding="utf-8")
        _wait_for(ready)
        blocked_result = _wait_for_one(results)
        assert json.loads(blocked_result.read_text(encoding="utf-8")) == {
            "status": "blocked"
        }
        assert len(_batch_dirs(root)) == 1
        assert sum(path.exists() for path in advanced) == 1

        release.write_text("release", encoding="utf-8")
        for process in processes:
            process.wait(timeout=10)
            assert process.returncode == 0

        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in results]
        assert sorted(payload["status"] for payload in payloads) == [
            "blocked",
            "completed",
        ]
        completed = next(
            payload for payload in payloads if payload["status"] == "completed"
        )
        persisted = load_schedule(schedule_path(root, FolderAdapter(folder).scan_key))
        assert persisted.last_batch_id == completed["batch_id"]
        assert len(_batch_dirs(root)) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_hard_kill_after_batch_write_claims_orphan_before_resume(
    tmp_path: Path,
) -> None:
    folder = _video_folder(tmp_path)
    root = tmp_path / "vault"
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ready = tmp_path / "batch-written"
    starter = _spawn("kill-before-claim", folder, root, ready, now.isoformat())
    try:
        _wait_for(ready)
        batch_dirs = _batch_dirs(root)
        assert len(batch_dirs) == 1
        batch_dir = batch_dirs[0]
        orphan = load_batch(batch_dir)
        schedule = load_schedule(schedule_path(root, FolderAdapter(folder).scan_key))
        assert orphan.status == "running"
        assert schedule.active_batch_id is None

        starter.kill()
        starter.wait(timeout=5)
        (folder / "must-wait.mp4").write_bytes(b"next-scan-only")

        advanced = tmp_path / "resumed-advanced"
        result = tmp_path / "resume.json"
        resumer = _spawn(
            "resume",
            folder,
            root,
            advanced,
            result,
            orphan.batch_id,
            (now + timedelta(minutes=1)).isoformat(),
        )
        try:
            resumer.wait(timeout=15)
            assert resumer.returncode == 0
        finally:
            if resumer.poll() is None:
                resumer.kill()
                resumer.wait(timeout=5)

        assert json.loads(result.read_text(encoding="utf-8")) == {
            "status": "completed",
            "batch_id": orphan.batch_id,
        }
        assert advanced.is_file()
        assert _batch_dirs(root) == [batch_dir]
        persisted = load_schedule(schedule_path(root, schedule.schedule_id))
        assert persisted.last_batch_id == orphan.batch_id
        assert persisted.active_batch_id is None
        assert persisted.cursor == orphan.cursor_after
    finally:
        if starter.poll() is None:
            starter.kill()
            starter.wait(timeout=5)


def test_hard_kill_after_terminal_batch_reconciles_cursor_on_same_batch(
    tmp_path: Path,
) -> None:
    folder = _video_folder(tmp_path)
    root = tmp_path / "vault"
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ready = tmp_path / "terminal-before-commit"
    first_advanced = tmp_path / "first-advanced"
    starter = _spawn(
        "kill-before-commit",
        folder,
        root,
        ready,
        first_advanced,
        now.isoformat(),
    )
    try:
        _wait_for(ready)
        batch_dirs = _batch_dirs(root)
        assert len(batch_dirs) == 1
        batch_dir = batch_dirs[0]
        terminal = load_batch(batch_dir)
        schedule = load_schedule(schedule_path(root, FolderAdapter(folder).scan_key))
        assert terminal.status == "completed"
        assert schedule.active_batch_id == terminal.batch_id
        assert schedule.cursor is None
        assert first_advanced.is_file()

        starter.kill()
        starter.wait(timeout=5)

        second_advanced = tmp_path / "second-advanced"
        result = tmp_path / "reconcile.json"
        reconciler = _spawn(
            "resume",
            folder,
            root,
            second_advanced,
            result,
            terminal.batch_id,
            (now + timedelta(minutes=1)).isoformat(),
        )
        try:
            reconciler.wait(timeout=15)
            assert reconciler.returncode == 0
        finally:
            if reconciler.poll() is None:
                reconciler.kill()
                reconciler.wait(timeout=5)

        assert json.loads(result.read_text(encoding="utf-8")) == {
            "status": "completed",
            "batch_id": terminal.batch_id,
        }
        assert not second_advanced.exists()
        assert _batch_dirs(root) == [batch_dir]
        persisted = load_schedule(schedule_path(root, schedule.schedule_id))
        assert persisted.cursor == terminal.cursor_after
        assert persisted.active_batch_id is None
        assert persisted.last_batch_id == terminal.batch_id
    finally:
        if starter.poll() is None:
            starter.kill()
            starter.wait(timeout=5)
