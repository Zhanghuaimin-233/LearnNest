from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from learnnest.discovery_models import DiscoveredLink
from learnnest.discovery_store import (
    claim_pending_discoveries,
    load_discovery_manifest,
    mark_discovery_artifact_result,
    mark_discovery_results,
    persist_discovery_links,
)


def _link(platform_id: str, *folder_ids: str) -> DiscoveredLink:
    return DiscoveredLink(
        platform_id=platform_id,
        source={
            "input": f"https://www.douyin.com/video/{platform_id}",
            "input_type": "url",
            "title": f"视频 {platform_id}",
            "content_type": "video",
        },
        content_kind="video",
        folder_ids=list(folder_ids),
    )


def test_discovery_store_merges_folder_memberships_and_claims_idempotently(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    persist_discovery_links(root, "douyin-test", [_link("101", "folder-a")], now=now)
    persist_discovery_links(
        root,
        "douyin-test",
        [_link("101", "folder-b"), _link("102")],
        now=now + timedelta(minutes=1),
    )

    manifest = load_discovery_manifest(root, "douyin-test")
    assert manifest is not None
    assert [record.link.platform_id for record in manifest.records] == ["101", "102"]
    assert manifest.records[0].link.folder_ids == ["folder-a", "folder-b"]

    claims = claim_pending_discoveries(root, max_items=1, now=now)
    assert len(claims) == 1
    assert claims[0].link.platform_id == "101"
    claimed = load_discovery_manifest(root, "douyin-test")
    assert claimed is not None
    assert claimed.records[0].status == "running"

    mark_discovery_results(
        root,
        [(claims[0], "downloaded", "task-101", None)],
        now=now + timedelta(minutes=2),
    )
    completed = load_discovery_manifest(root, "douyin-test")
    assert completed is not None
    assert completed.records[0].status == "downloaded"
    assert completed.records[0].task_id == "task-101"


def test_discovery_store_consumes_a_snapshot_oldest_first_by_default(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    now = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
    newest = _link("7662221810011802932")
    older = _link("7388356571291454757")

    persist_discovery_links(root, "douyin-test", [newest, older], now=now)

    first_claim = claim_pending_discoveries(root, max_items=1, now=now)
    assert [claim.link.platform_id for claim in first_claim] == [older.platform_id]

    mark_discovery_results(
        root,
        [(first_claim[0], "downloaded", "task-older", None)],
        now=now + timedelta(minutes=1),
    )
    second_claim = claim_pending_discoveries(
        root,
        max_items=1,
        now=now + timedelta(minutes=2),
    )
    assert [claim.link.platform_id for claim in second_claim] == [newest.platform_id]


def test_discovery_store_latest_selector_is_scoped_to_one_fresh_snapshot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    now = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
    newest = _link("7662221810011802932")
    older = _link("7388356571291454757")
    persist_discovery_links(root, "douyin-test", [newest, older], now=now)

    claims = claim_pending_discoveries(
        root,
        max_items=1,
        now=now,
        schedule_id="douyin-test",
        selection="latest_observed",
    )

    assert [claim.link.platform_id for claim in claims] == [newest.platform_id]
    manifest = load_discovery_manifest(root, "douyin-test")
    assert manifest is not None
    assert manifest.latest_observation_at == now
    assert [record.observed_position for record in manifest.records] == [0, 1]


def test_discovery_store_allows_downloaded_non_task_artifact(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    link = DiscoveredLink(
        platform_id="102",
        source={
            "input": "https://www.douyin.com/video/102",
            "input_type": "url",
            "title": "图文 102",
            "content_type": "image_text",
        },
        content_kind="image_text",
    )
    persist_discovery_links(root, "douyin-test", [link], now=now)
    claim = claim_pending_discoveries(root, max_items=1, now=now)[0]

    mark_discovery_artifact_result(
        root,
        claim,
        "downloaded",
        "抖音图文素材/图文 102--102/image_text.json",
        None,
        now=now,
    )

    manifest = load_discovery_manifest(root, "douyin-test")
    assert manifest is not None
    assert manifest.records[0].task_id is None
    assert manifest.records[0].artifact_path == (
        "抖音图文素材/图文 102--102/image_text.json"
    )
