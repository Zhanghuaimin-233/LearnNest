from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from learnnest.discovery_models import DiscoveredLink
from learnnest.adapters.douyin_media import ImageTextDownload
from learnnest.discovery_store import load_discovery_manifest, persist_discovery_links
from learnnest.download_queue import run_pending_downloads
from learnnest.execution_models import SourceIdentities
from learnnest.models import StageStatus, TaskRecord
from learnnest.task_store import write_task_atomic


def test_pending_downloads_use_a_separate_processing_batch(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    link = DiscoveredLink(
        platform_id="101",
        source={
            "input": "https://www.douyin.com/video/101",
            "input_type": "url",
            "title": "待下载视频",
        },
        content_kind="video",
    )
    persist_discovery_links(root, "douyin-test", [link], now=now)

    def processor(source, output_root, profile):
        task = TaskRecord(
            task_id="task-101",
            source_path=source.input,
            source_input=source.input,
            source_type="url",
            source_fingerprint="fingerprint-101",
            title=source.title or "待下载视频",
            profile=profile,
            identities=SourceIdentities(
                normalized_source=source.input,
                platform="douyin",
                platform_id="101",
            ),
            stages={"content_pack": StageStatus.COMPLETED},
        )
        write_task_atomic(
            output_root / "视频学习素材" / "待下载视频--task-101",
            task,
        )
        return task

    outcome = run_pending_downloads(
        root,
        max_items=1,
        processor=processor,
        now=now,
    )

    assert outcome.claimed_count == 1
    assert outcome.completed_count == 1
    assert outcome.batch_id is not None
    assert outcome.task_ids == ("task-101",)
    manifest = load_discovery_manifest(root, "douyin-test")
    assert manifest is not None
    assert manifest.records[0].status == "downloaded"
    assert manifest.records[0].task_id == "task-101"


def test_pending_downloads_route_image_text_to_local_asset_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    link = DiscoveredLink(
        platform_id="102",
        source={
            "input": "https://www.douyin.com/video/102",
            "input_type": "url",
            "title": "图文作品",
            "content_type": "image_text",
        },
        content_kind="image_text",
    )
    persist_discovery_links(root, "douyin-test", [link], now=now)

    def image_downloader(source, output_dir):
        assert source.content_type == "image_text"
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = output_dir / "image_text.json"
        manifest.write_text('{"schema_version":"1.0"}\n', encoding="utf-8")
        return ImageTextDownload(manifest_path=manifest, files=())

    outcome = run_pending_downloads(
        root,
        max_items=1,
        image_text_downloader=image_downloader,
        now=now,
    )

    assert outcome.batch_id is None
    assert outcome.claimed_count == 1
    assert outcome.completed_count == 1
    manifest = load_discovery_manifest(root, "douyin-test")
    assert manifest is not None
    assert manifest.records[0].status == "downloaded"
    assert manifest.records[0].task_id is None
    assert manifest.records[0].artifact_path == (
        "抖音图文素材/图文作品--102/image_text.json"
    )
