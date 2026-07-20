from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from learnnest.models import StageStatus, TaskRecord


def _video(path: Path, content: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _processor(calls: list[str]):
    def process(source, output_root, profile):
        calls.append(source.input)
        return TaskRecord(
            task_id=f"20260712-{len(calls):08d}",
            source_path=source.input,
            source_fingerprint=f"processed-{len(calls)}",
            title=Path(source.input).stem,
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )

    return process


def test_incremental_folder_scan_adds_zero_then_one_item(tmp_path: Path) -> None:
    from learnnest.adapters.folder import FolderAdapter
    from learnnest.scanning import run_incremental_scan

    source_dir = tmp_path / "videos"
    _video(source_dir / "first.mp4")
    root = tmp_path / "vault"
    adapter = FolderAdapter(source_dir)
    calls: list[str] = []
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    first = run_incremental_scan(adapter, root, processor=_processor(calls), now=now)
    second = run_incremental_scan(
        adapter,
        root,
        processor=_processor(calls),
        now=now + timedelta(minutes=1),
    )
    added = _video(source_dir / "second.mkv", b"second")
    third = run_incremental_scan(
        adapter,
        root,
        processor=_processor(calls),
        now=now + timedelta(minutes=2),
    )

    assert first.kind == second.kind == third.kind == "scan"
    assert (
        first.schedule_id == second.schedule_id == third.schedule_id == adapter.scan_key
    )
    assert len(first.results) == 1
    assert len(second.results) == 0
    assert [item.input for item in third.results] == [str(added.resolve())]
    assert second.cursor_before == first.cursor_after
    assert third.cursor_before == second.cursor_after
    assert calls == [str((source_dir / "first.mp4").resolve()), str(added.resolve())]


def test_interrupted_scan_resumes_without_discovery_or_cursor_advance(
    tmp_path: Path,
) -> None:
    from learnnest.adapters.folder import FolderAdapter
    from learnnest.batch_store import load_batch
    from learnnest.scanning import run_incremental_scan

    source_dir = tmp_path / "videos"
    video = _video(source_dir / "lesson.mp4")
    root = tmp_path / "vault"
    adapter = FolderAdapter(source_dir)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    def interrupt(source, output_root, profile):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_incremental_scan(adapter, root, processor=interrupt, now=now)

    batch_json = next((root / "视频学习批次").glob("*/batch.json"))
    interrupted = load_batch(batch_json)
    assert interrupted.status == "interrupted"
    assert interrupted.cursor_before is None
    prospective_cursor = interrupted.cursor_after

    _video(source_dir / "new.mp4", b"must-wait")

    class NoDiscoveryAdapter:
        scan_key = adapter.scan_key

        def discover(self, cursor):
            raise AssertionError(
                "an interrupted scan must resume its persisted manifest"
            )

    calls: list[str] = []
    resumed = run_incremental_scan(
        NoDiscoveryAdapter(),
        root,
        processor=_processor(calls),
        now=now + timedelta(minutes=1),
    )

    assert resumed.batch_id == interrupted.batch_id
    assert resumed.status == "completed"
    assert resumed.cursor_after == prospective_cursor
    assert calls == [str(video.resolve())]

    next_scan = run_incremental_scan(
        adapter,
        root,
        processor=_processor(calls),
        now=now + timedelta(minutes=2),
    )
    assert [item.input for item in next_scan.results] == [
        str((source_dir / "new.mp4").resolve())
    ]


def test_partial_scan_cursor_is_the_next_committed_cursor(tmp_path: Path) -> None:
    from learnnest.adapters.folder import FolderAdapter
    from learnnest.scanning import run_incremental_scan

    source_dir = tmp_path / "videos"
    first_video = _video(source_dir / "a.mp4")
    second_video = _video(source_dir / "b.mp4", b"second")
    root = tmp_path / "vault"
    adapter = FolderAdapter(source_dir)
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    def partial(source, output_root, profile):
        if source.input == str(second_video.resolve()):
            raise ValueError("manual failure")
        return TaskRecord(
            task_id="20260712-partial1",
            source_path=source.input,
            source_fingerprint="processed-first",
            title=Path(source.input).stem,
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )

    first = run_incremental_scan(adapter, root, processor=partial, now=now)
    calls: list[str] = []
    second = run_incremental_scan(
        adapter,
        root,
        processor=_processor(calls),
        now=now + timedelta(minutes=1),
    )

    assert first.status == "partial"
    assert first.cursor_after is not None
    assert second.cursor_before == first.cursor_after
    assert second.results == []
    assert calls == []
    assert first.results[0].input == str(first_video.resolve())
