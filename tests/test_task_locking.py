from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from learnnest.task_store import load_task

_WORKER = Path(__file__).parent / "helpers" / "pipeline_lock_worker.py"


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


def _spawn(
    mode: str,
    source: Path,
    root: Path,
    ready: Path,
    release: Path,
    result: Path,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            str(_WORKER),
            mode,
            str(source),
            str(root),
            str(ready),
            str(release),
            str(result),
        ],
        cwd=Path(__file__).parents[1],
        env=_environment(),
    )


def test_two_processes_cannot_advance_the_same_task(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"cross-process-video")
    root = tmp_path / "vault"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    first_result = tmp_path / "first.json"
    second_result = tmp_path / "second.json"
    first = _spawn("hold", source, root, ready, release, first_result)
    second: subprocess.Popen[bytes] | None = None
    try:
        _wait_for(ready)
        second = _spawn("run", source, root, ready, release, second_result)
        second.wait(timeout=10)
        assert second.returncode == 0
        assert json.loads(second_result.read_text(encoding="utf-8")) == {
            "status": "blocked"
        }
        release.write_text("release", encoding="utf-8")
        first.wait(timeout=10)
        assert first.returncode == 0
        first_payload = json.loads(first_result.read_text(encoding="utf-8"))
        task_dir = next((root / "视频学习素材").iterdir())
        task = load_task(task_dir)
        assert first_payload == {"status": "completed", "task_id": task.task_id}
        assert len(task.attempts) == 1
        assert task.attempts[0].status == "completed"
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_two_paths_with_identical_bytes_cannot_create_two_tasks(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "first.mp4"
    second_source = tmp_path / "second.mp4"
    first_source.write_bytes(b"same-cross-process-content")
    second_source.write_bytes(first_source.read_bytes())
    root = tmp_path / "vault"
    ready = tmp_path / "content-ready"
    release = tmp_path / "content-release"
    first_result = tmp_path / "content-first.json"
    second_result = tmp_path / "content-second.json"
    first = _spawn("hold", first_source, root, ready, release, first_result)
    second: subprocess.Popen[bytes] | None = None
    try:
        _wait_for(ready)
        second = _spawn("run", second_source, root, ready, release, second_result)
        second.wait(timeout=10)
        assert second.returncode == 0
        assert json.loads(second_result.read_text(encoding="utf-8")) == {
            "status": "blocked"
        }
        release.write_text("release", encoding="utf-8")
        first.wait(timeout=10)
        assert first.returncode == 0
        assert len(list((root / "视频学习素材").glob("*/task.json"))) == 1
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_force_derived_match_waits_for_the_canonical_task_lock(tmp_path: Path) -> None:
    canonical_source = tmp_path / "canonical.mp4"
    derived_source = tmp_path / "derived.mp4"
    canonical_source.write_bytes(b"same-force-content")
    derived_source.write_bytes(canonical_source.read_bytes())
    root = tmp_path / "vault"

    setup = _spawn(
        "run",
        canonical_source,
        root,
        tmp_path / "setup-ready",
        tmp_path / "setup-release",
        tmp_path / "setup-result.json",
    )
    setup.wait(timeout=10)
    assert setup.returncode == 0

    ready = tmp_path / "rerun-ready"
    release = tmp_path / "rerun-release"
    owner_result = tmp_path / "owner-result.json"
    force_result = tmp_path / "force-result.json"
    owner = _spawn(
        "hold_rerun",
        canonical_source,
        root,
        ready,
        release,
        owner_result,
    )
    contender: subprocess.Popen[bytes] | None = None
    try:
        _wait_for(ready)
        contender = _spawn(
            "force",
            derived_source,
            root,
            ready,
            release,
            force_result,
        )
        contender.wait(timeout=10)
        assert contender.returncode == 0
        assert json.loads(force_result.read_text(encoding="utf-8")) == {
            "status": "blocked"
        }
        release.write_text("release", encoding="utf-8")
        owner.wait(timeout=10)
        assert owner.returncode == 0
    finally:
        for process in (owner, contender):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
