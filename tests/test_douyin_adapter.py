from __future__ import annotations

import json
from typing import Any

from learnnest.adapters.douyin import DouyinFavoritesAdapter


class FixtureTransport:
    def __init__(self) -> None:
        self.video_items: list[dict[str, Any]] = [
            {"aweme_id": "101", "desc": "视频一", "video": {"duration": 1000}},
            {
                "aweme_id": "102",
                "desc": "图文一",
                "is_new_text_mode": True,
                "images": [{"url_list": ["https://example.test/image.jpg"]}],
            },
        ]

    def list_video_favorites(self, *, cursor: int, count: int) -> dict[str, Any]:
        del cursor, count
        return {"aweme_list": list(self.video_items), "cursor": 0, "has_more": False}

    def list_folders(self, *, cursor: int, count: int) -> dict[str, Any]:
        del cursor, count
        return {
            "collects_list": [
                {
                    "collects_id_str": "folder-1",
                    "collects_name": "算法",
                    "total_number": 2,
                }
            ],
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
                self.video_items[0],
                {"aweme_id": "103", "item_title": "视频三", "video": {}},
            ],
            "cursor": 0,
            "has_more": False,
        }


def test_douyin_adapter_merges_default_video_and_folder_without_duplicate_links() -> (
    None
):
    adapter = DouyinFavoritesAdapter(
        FixtureTransport(),
        folder_ids=("folder-1",),
    )

    discovery = adapter.discover(None)

    assert [item.input for item in discovery.items] == [
        "https://www.douyin.com/video/101",
        "https://www.douyin.com/video/102",
        "https://www.douyin.com/video/103",
    ]
    assert [link.platform_id for link in discovery.links] == ["101", "102", "103"]
    assert discovery.links[0].folder_ids == ["folder-1"]
    assert discovery.links[1].content_kind == "image_text"
    assert discovery.links[1].source.content_type == "image_text"
    assert discovery.links[0].source.input == "https://www.douyin.com/video/101"


def test_douyin_adapter_uses_seen_ids_to_accept_a_new_item_at_the_head() -> None:
    transport = FixtureTransport()
    adapter = DouyinFavoritesAdapter(transport, folder_ids=("folder-1",))
    first = adapter.discover(None)

    transport.video_items.insert(
        0,
        {"aweme_id": "104", "desc": "新视频", "video": {"duration": 2000}},
    )
    second = adapter.discover(first.cursor_after)

    assert [item.input for item in second.items] == ["https://www.douyin.com/video/104"]


def test_douyin_adapter_classifies_long_article_as_image_text() -> None:
    transport = FixtureTransport()
    transport.video_items = [
        {
            "aweme_id": "104",
            "desc": "文章型图文",
            "article_info": {
                "article_type": 999,
                "article_content": json.dumps(
                    {"long_article_abstract": "摘要", "markdown": "正文"}
                ),
                "fe_data": json.dumps(
                    {"image_list": [{"markdown_url": "https://cdn.example/a.jpg"}]}
                ),
            },
        }
    ]

    discovery = DouyinFavoritesAdapter(transport).discover(None)

    assert discovery.links[0].content_kind == "image_text"
    assert discovery.links[0].source.content_type == "image_text"


def test_douyin_adapter_rejects_unsupported_scope() -> None:
    try:
        DouyinFavoritesAdapter(FixtureTransport(), include_music=True)
    except TypeError as error:
        assert "include_music" in str(error)
    else:
        raise AssertionError("unsupported music scope must not be accepted")
