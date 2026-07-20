from __future__ import annotations

import json
from pathlib import Path

from learnnest.batch_store import load_batch


def test_legacy_batch_loads_as_2_0_without_rewriting_disk(tmp_path: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "batch_id": "20260711T120000Z-a1b2c3d4",
        "profile": "evidence",
        "results": [
            {
                "input": "C:/videos/lesson.mp4",
                "input_type": "local_file",
                "source_fingerprint": "a1b2c3d4",
                "status": "completed",
                "task_id": "20260711-a1b2c3d4",
                "error": None,
            }
        ],
    }
    path = tmp_path / "batch.json"
    original = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(original, encoding="utf-8", newline="\n")

    batch = load_batch(path)

    assert batch.schema_version == "2.0"
    assert batch.kind == "manual"
    assert batch.status == "completed"
    assert batch.results[0].position == 1
    assert path.read_text(encoding="utf-8") == original


def test_early_2_0_batch_item_metadata_migrates_into_complete_source(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": "2.0",
        "batch_id": "20260712T120000Z-early200",
        "kind": "manual",
        "status": "completed",
        "created_at": "2026-07-12T12:00:00Z",
        "updated_at": "2026-07-12T12:00:00Z",
        "finished_at": "2026-07-12T12:00:00Z",
        "profile": "evidence",
        "schedule_id": None,
        "cursor_before": None,
        "cursor_after": None,
        "results": [
            {
                "position": 1,
                "input": "https://example.com/watch?v=1",
                "input_type": "url",
                "title": "课程一",
                "content_type": "course",
                "tags": ["python"],
                "source_fingerprint": "early-fingerprint",
                "status": "completed",
                "task_id": "20260712-early200",
                "attempt_id": None,
                "error": None,
                "failure": None,
            }
        ],
    }
    path = tmp_path / "batch.json"
    original = json.dumps(payload, ensure_ascii=False)
    path.write_text(original, encoding="utf-8")

    batch = load_batch(path)

    assert batch.results[0].source.model_dump(mode="json") == {
        "schema_version": "1.0",
        "input": "https://example.com/watch?v=1",
        "input_type": "url",
        "title": "课程一",
        "content_type": "course",
        "tags": ["python"],
    }
    assert path.read_text(encoding="utf-8") == original
