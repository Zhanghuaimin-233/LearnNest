from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from learnnest.adapters.douyin_media import (
    DouyinImageTextDownloader,
    DouyinMediaError,
)
from learnnest.source_models import SourceItem


class DetailTransport:
    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        self.aweme_ids: list[str] = []

    def get_aweme_detail(self, aweme_id: str) -> dict[str, Any]:
        self.aweme_ids.append(aweme_id)
        return self.detail


class AssetResponse:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> AssetResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class AssetOpener:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.requests: list[Any] = []

    def __call__(self, request: Any, *, timeout: float) -> AssetResponse:
        del timeout
        self.requests.append(request)
        return AssetResponse(self.payloads[request.full_url])


def test_image_text_downloader_saves_assets_and_never_persists_remote_urls(
    tmp_path: Path,
) -> None:
    first_url = "https://cdn.example/first.jpg?signature=secret"
    second_url = "https://cdn.example/second.png?signature=secret"
    transport = DetailTransport(
        {
            "aweme_id": "101",
            "desc": "图文作品",
            "images": [
                {"url_list": [first_url]},
                {"url_list": [second_url]},
            ],
        }
    )
    opener = AssetOpener({first_url: b"first-image", second_url: b"second-image"})

    result = DouyinImageTextDownloader(
        transport,
        opener=opener,
    ).download(
        SourceItem(
            input="https://www.douyin.com/video/101",
            input_type="url",
            title="图文作品",
            content_type="image_text",
        ),
        tmp_path,
    )

    assert transport.aweme_ids == ["101"]
    assert [path.name for path in result.files] == ["image_0001.jpg", "image_0002.png"]
    assert result.manifest_path == tmp_path / "image_text.json"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert first_url not in serialized
    assert second_url not in serialized
    assert [item["path"] for item in manifest["files"]] == [
        "images/image_0001.jpg",
        "images/image_0002.png",
    ]
    assert all(request.get_header("Cookie") is None for request in opener.requests)
    assert (tmp_path / "images" / "image_0001.jpg").read_bytes() == b"first-image"


def test_image_text_downloader_localizes_long_article_and_images(
    tmp_path: Path,
) -> None:
    origin_url = "https://cdn.example/origin.jpg?signature=secret"
    markdown_url = "https://cdn.example/markdown.jpg?signature=secret"
    detail = {
        "aweme_id": "103",
        "article_info": {
            "article_type": 999,
            "article_content": json.dumps(
                {
                    "long_article_abstract": "文章摘要",
                    "markdown": f"# 文章标题\n\n![配图]({markdown_url})",
                },
                ensure_ascii=False,
            ),
            "fe_data": json.dumps(
                {
                    "image_list": [
                        {
                            "origin_image_url": origin_url,
                            "markdown_url": markdown_url,
                        }
                    ]
                }
            ),
        },
    }
    transport = DetailTransport(detail)
    opener = AssetOpener({origin_url: b"article-image"})

    result = DouyinImageTextDownloader(transport, opener=opener).download(
        SourceItem(
            input="https://www.douyin.com/video/103",
            input_type="url",
            title="文章型图文",
            content_type="image_text",
        ),
        tmp_path,
    )

    assert result.text_path == tmp_path / "article.md"
    article = result.text_path.read_text(encoding="utf-8")
    assert "![配图](images/image_0001.jpg)" in article
    assert origin_url not in article
    assert markdown_url not in article
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["variant"] == "article"
    assert manifest["text_path"] == "article.md"
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert origin_url not in serialized
    assert markdown_url not in serialized


def test_image_text_downloader_rejects_non_image_source(tmp_path: Path) -> None:
    downloader = DouyinImageTextDownloader(DetailTransport({}))

    with pytest.raises(ValueError, match="image_text"):
        downloader.download(
            SourceItem(
                input="https://www.douyin.com/video/101",
                input_type="url",
                content_type="video",
            ),
            tmp_path,
        )


def test_image_text_downloader_fails_closed_when_detail_has_no_images(
    tmp_path: Path,
) -> None:
    downloader = DouyinImageTextDownloader(
        DetailTransport({"aweme_id": "101", "video": {}})
    )

    with pytest.raises(DouyinMediaError, match="images"):
        downloader.download(
            SourceItem(
                input="https://www.douyin.com/video/101",
                input_type="url",
                content_type="image_text",
            ),
            tmp_path,
        )
