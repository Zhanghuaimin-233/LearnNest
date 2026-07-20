from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from PIL import Image

import learnnest.pipeline as pipeline
from learnnest.locks import LockUnavailable
from learnnest.task_store import find_task_by_fingerprint
from learnnest.util import source_fingerprint


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


def main() -> None:
    mode, source_value, root_value, ready_value, release_value, result_value = sys.argv[
        1:
    ]
    source = Path(source_value)
    root = Path(root_value)
    ready = Path(ready_value)
    release = Path(release_value)
    result = Path(result_value)

    def probe(_: Path) -> dict[str, object]:
        if mode.startswith("hold"):
            ready.write_text("ready", encoding="utf-8")
            _wait_for(release)
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
    try:
        if mode == "hold_rerun":
            found = find_task_by_fingerprint(root, source_fingerprint(source))
            if found is None:
                raise RuntimeError("canonical task was not created")
            task = pipeline.rerun_task(
                found[0],
                "source",
                task_lock_timeout=0.2,
            )
        else:
            task = pipeline.process_video(
                source,
                root,
                force=mode == "force",
                task_lock_timeout=0.2,
            )
    except LockUnavailable:
        result.write_text(json.dumps({"status": "blocked"}), encoding="utf-8")
    else:
        result.write_text(
            json.dumps({"status": "completed", "task_id": task.task_id}),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
