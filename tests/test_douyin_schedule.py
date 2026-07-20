from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from learnnest.adapters.douyin import DouyinFavoritesAdapter
from learnnest.batch_store import load_batch
from learnnest.discovery_store import load_discovery_manifest
from learnnest.schedule_models import ScheduleRecord
from learnnest.schedule_store import load_schedule, schedule_path, write_schedule_atomic
from learnnest.schedules import (
    create_interval_douyin_schedule,
    run_schedule_once,
)


class FixtureTransport:
    def list_video_favorites(self, *, cursor: int, count: int) -> dict[str, Any]:
        del cursor, count
        return {
            "aweme_list": [
                {"aweme_id": "101", "desc": "视频一", "video": {}},
                {
                    "aweme_id": "102",
                    "desc": "图文一",
                    "is_new_text_mode": True,
                    "images": [{"url_list": ["https://example.test/image.jpg"]}],
                },
            ],
            "cursor": 0,
            "has_more": False,
        }

    def list_folders(self, *, cursor: int, count: int) -> dict[str, Any]:
        del cursor, count
        return {
            "collects_list": [{"collects_id_str": "folder-1", "collects_name": "算法"}],
            "cursor": 0,
            "has_more": False,
        }

    def list_folder_items(
        self,
        folder_id: str,
        *,
        cursor: int,
        count: int,
    ) -> dict[str, Any]:
        del cursor, count
        assert folder_id == "folder-1"
        return {
            "aweme_list": [
                {"aweme_id": "101", "desc": "视频一", "video": {}},
                {"aweme_id": "103", "item_title": "视频三", "video": {}},
            ],
            "cursor": 0,
            "has_more": False,
        }


def test_discovery_only_schedule_commits_cursor_without_creating_tasks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    schedule = ScheduleRecord(
        schedule_id="douyin-discovery",
        status="enabled",
        source={"kind": "douyin", "url": "https://douyin.example/favorites"},
        trigger={"kind": "manual"},
        profile="evidence",
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(root, schedule)

    def unexpected_processor(*args):
        raise AssertionError("discovery-only schedule must not download")

    outcome = run_schedule_once(
        root,
        schedule.schedule_id,
        adapter=DouyinFavoritesAdapter(FixtureTransport(), folder_ids=("folder-1",)),
        processor=unexpected_processor,
        now=now,
    )

    assert outcome.status == "completed"
    assert outcome.batch_id is not None
    batch = load_batch(root / "视频学习批次" / outcome.batch_id)
    assert batch.purpose == "discovery"
    assert not (root / "视频学习素材").exists()
    persisted = load_schedule(schedule_path(root, schedule.schedule_id))
    assert persisted.cursor is not None
    ledger = load_discovery_manifest(root, schedule.schedule_id)
    assert ledger is not None
    assert [record.status for record in ledger.records] == [
        "pending",
        "pending",
        "pending",
    ]

    second = run_schedule_once(
        root,
        schedule.schedule_id,
        adapter=DouyinFavoritesAdapter(FixtureTransport(), folder_ids=("folder-1",)),
        processor=unexpected_processor,
        now=now,
    )

    assert second.status == "completed"
    second_batch = load_batch(root / "视频学习批次" / second.batch_id)
    assert second_batch.results == []
    assert len(second_batch.discovery_links) == 3
    assert len(load_discovery_manifest(root, schedule.schedule_id).records) == 3


def test_create_interval_douyin_schedule_persists_video_only_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"

    schedule = create_interval_douyin_schedule(
        root,
        "douyin-video",
        "https://www.douyin.com/user/example/favorite",
        every_seconds=3600,
    )

    assert schedule.source.kind == "douyin"
    assert schedule.source.folder_ids == []
    assert schedule.source.include_default_video is True
    assert schedule.next_tick_at is not None
