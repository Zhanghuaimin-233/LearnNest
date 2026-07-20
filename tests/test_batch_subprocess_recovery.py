from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from learnnest.batch import run_batch
from learnnest.batch_store import load_batch
from learnnest.sources import parse_source
from learnnest.task_store import load_task

_WORKER = Path(__file__).parent / "helpers" / "batch_resume_worker.py"


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


def _video(path: Path) -> Path:
    path.write_bytes(b"subprocess-batch-video")
    return path


def test_hard_kill_before_attempt_callback_recovers_task_by_source_fingerprint(
    tmp_path: Path,
) -> None:
    source = _video(tmp_path / "callback-gap.mp4")
    root = tmp_path / "vault"
    ready = tmp_path / "task-written"
    starter = _spawn("start-before-callback", source, root, ready)
    try:
        _wait_for(ready)
        batch_dir = next((root / "视频学习批次").iterdir())
        before_kill = load_batch(batch_dir)
        task_dir = next((root / "视频学习素材").iterdir())
        original_task = load_task(task_dir)
        assert before_kill.results[0].status == "running"
        assert before_kill.results[0].task_id is None
        assert original_task.active_attempt_id is not None

        starter.kill()
        starter.wait(timeout=5)

        result = tmp_path / "resume-result.json"
        resumer = _spawn("resume-pipeline", batch_dir, result)
        try:
            resumer.wait(timeout=15)
            assert resumer.returncode == 0
        finally:
            if resumer.poll() is None:
                resumer.kill()
                resumer.wait(timeout=5)

        payload = json.loads(result.read_text(encoding="utf-8"))
        completed = load_batch(batch_dir)
        recovered_task = load_task(task_dir)
        assert payload == {
            "status": "completed",
            "task_id": original_task.task_id,
        }
        assert completed.results[0].task_id == original_task.task_id
        assert [attempt.status for attempt in recovered_task.attempts] == [
            "interrupted",
            "completed",
        ]
    finally:
        if starter.poll() is None:
            starter.kill()
            starter.wait(timeout=5)


def test_two_subprocess_resumers_allow_only_one_to_advance(tmp_path: Path) -> None:
    source = parse_source(str(_video(tmp_path / "double-resume.mp4")))
    root = tmp_path / "vault"

    def interrupt(*args):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_batch([source], root, processor=interrupt)
    batch_dir = next((root / "视频学习批次").iterdir())

    first_ready = tmp_path / "first-ready"
    release = tmp_path / "release-first"
    first_advanced = tmp_path / "first-advanced"
    second_advanced = tmp_path / "second-advanced"
    first_result = tmp_path / "first-result.json"
    second_result = tmp_path / "second-result.json"
    first = _spawn(
        "resume-custom-hold",
        batch_dir,
        first_ready,
        release,
        first_advanced,
        first_result,
    )
    second: subprocess.Popen[bytes] | None = None
    try:
        _wait_for(first_ready)
        second = _spawn(
            "resume-custom-run",
            batch_dir,
            tmp_path / "unused-ready",
            tmp_path / "unused-release",
            second_advanced,
            second_result,
        )
        second.wait(timeout=10)
        assert second.returncode == 0
        assert json.loads(second_result.read_text(encoding="utf-8")) == {
            "status": "blocked"
        }
        assert first_advanced.is_file()
        assert not second_advanced.exists()

        release.write_text("release", encoding="utf-8")
        first.wait(timeout=10)
        assert first.returncode == 0
        assert json.loads(first_result.read_text(encoding="utf-8")) == {
            "status": "completed"
        }
        assert load_batch(batch_dir).status == "completed"
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
