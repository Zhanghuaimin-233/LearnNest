from __future__ import annotations

import json
from pathlib import Path

import pytest


def _video(path: Path, content: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_folder_adapter_cursor_is_versioned_sorted_and_incremental(
    tmp_path: Path,
) -> None:
    from learnnest.adapters.folder import FolderAdapter

    second = _video(tmp_path / "b.MKV")
    first = _video(tmp_path / "A.mp4")
    _video(tmp_path / "nested" / "ignored.webm")
    adapter = FolderAdapter(tmp_path)

    initial = adapter.discover(None)

    assert [item.input for item in initial.items] == [
        str(first.resolve()),
        str(second.resolve()),
    ]
    payload = json.loads(initial.cursor_after)
    assert payload == {
        "entries": {
            "a.mp4": {
                "mtime_ns": first.stat().st_mtime_ns,
                "size": first.stat().st_size,
            },
            "b.mkv": {
                "mtime_ns": second.stat().st_mtime_ns,
                "size": second.stat().st_size,
            },
        },
        "recursive": False,
        "root_id": adapter.root_id,
        "version": "folder-v1",
    }
    assert initial.cursor_after == json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    unchanged = adapter.discover(initial.cursor_after)

    assert unchanged.items == []
    assert unchanged.cursor_after == initial.cursor_after

    third = _video(tmp_path / "c.mov", b"new-video")
    changed = adapter.discover(unchanged.cursor_after)

    assert [item.input for item in changed.items] == [str(third.resolve())]
    assert list(json.loads(changed.cursor_after)["entries"]) == [
        "a.mp4",
        "b.mkv",
        "c.mov",
    ]


def test_folder_adapter_recursive_scan_key_is_stable_and_mode_specific(
    tmp_path: Path,
) -> None:
    from learnnest.adapters.folder import FolderAdapter

    first = FolderAdapter(tmp_path, recursive=True)
    same = FolderAdapter(tmp_path / ".", recursive=True)
    flat = FolderAdapter(tmp_path, recursive=False)

    assert first.scan_key == same.scan_key
    assert first.scan_key.startswith("folder-")
    assert first.scan_key != flat.scan_key


def test_folder_adapter_rejects_cursor_from_other_root_or_mode(tmp_path: Path) -> None:
    from learnnest.adapters.folder import FolderAdapter

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _video(first_root / "lesson.mp4")
    _video(second_root / "lesson.mp4")
    cursor = FolderAdapter(first_root).discover(None).cursor_after

    with pytest.raises(ValueError, match="root"):
        FolderAdapter(second_root).discover(cursor)
    with pytest.raises(ValueError, match="recursive"):
        FolderAdapter(first_root, recursive=True).discover(cursor)
