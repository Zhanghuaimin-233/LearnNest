from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from PIL import Image

import learnnest.pipeline as pipeline
from learnnest.batch import resume_batch, run_batch
from learnnest.locks import LockUnavailable
from learnnest.models import StageStatus, TaskRecord
from learnnest.sources import parse_source


def _wait_for(path: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _worker(args: list[str]) -> None:
    kind = args[0]
    if kind == "asr":
        output = Path(args[2])
        output.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "provider": "fake-asr",
                    "model": "fake",
                    "segments": [
                        {
                            "id": "tr_0001",
                            "start_ms": 0,
                            "end_ms": 500,
                            "text": "test",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return
    if kind == "ocr":
        output = Path(args[2])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps({"schema_version": "1.0", "provider": "fake-ocr", "items": []}),
            encoding="utf-8",
        )
        return
    raise ValueError(f"unknown worker: {kind}")


def _install_fake_pipeline() -> None:
    def probe(_: Path) -> dict[str, object]:
        return {
            "format": {"duration": "1.0"},
            "streams": [
                {"codec_type": "video", "duration": "1.0"},
                {"codec_type": "audio", "duration": "1.0"},
            ],
        }

    def frame(*, video_path: Path, timestamp_ms: int, output_path: Path) -> None:
        del video_path, timestamp_ms
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), color="white").save(output_path)

    pipeline.probe_video = probe
    pipeline.extract_frame = frame
    pipeline.providers.run_worker = _worker


def _start_before_callback(arguments: list[str]) -> None:
    source, root, ready = map(Path, arguments)
    _install_fake_pipeline()
    original_write = pipeline.write_task_atomic

    def write_then_hold(task_dir: str | Path, task: TaskRecord) -> Path:
        destination = original_write(task_dir, task)
        if not ready.exists() and task.active_attempt_id is not None:
            ready.write_text("task-written", encoding="utf-8")
            _wait_for(ready.with_suffix(".never"), timeout=300.0)
        return destination

    pipeline.write_task_atomic = write_then_hold
    run_batch([parse_source(str(source))], root)


def _resume_pipeline(arguments: list[str]) -> None:
    batch_dir, result = map(Path, arguments)
    _install_fake_pipeline()
    try:
        manifest = resume_batch(batch_dir, lock_timeout=1.0)
    except LockUnavailable:
        payload = {"status": "blocked"}
    else:
        payload = {
            "status": manifest.status,
            "task_id": manifest.results[0].task_id,
        }
    result.write_text(json.dumps(payload), encoding="utf-8")


def _resume_custom(arguments: list[str], *, hold: bool) -> None:
    batch_dir, ready, release, advanced, result = map(Path, arguments)

    def processor(source, root, profile):
        del root
        advanced.write_text("advanced", encoding="utf-8")
        if hold:
            ready.write_text("ready", encoding="utf-8")
            _wait_for(release)
        return TaskRecord(
            task_id=f"subprocess-{advanced.stem}",
            source_path=source.input,
            source_fingerprint=f"subprocess-{advanced.stem}",
            title=Path(source.input).stem,
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )

    try:
        manifest = resume_batch(
            batch_dir,
            processor=processor,
            lock_timeout=0.2,
        )
    except LockUnavailable:
        payload = {"status": "blocked"}
    else:
        payload = {"status": manifest.status}
    result.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    if mode == "start-before-callback":
        _start_before_callback(args)
    elif mode == "resume-pipeline":
        _resume_pipeline(args)
    elif mode == "resume-custom-hold":
        _resume_custom(args, hold=True)
    elif mode == "resume-custom-run":
        _resume_custom(args, hold=False)
    else:
        raise ValueError(f"unknown worker mode: {mode}")
