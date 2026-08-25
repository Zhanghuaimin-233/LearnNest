from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from learnnest.source_models import SourceItem


class FakeYdl:
    def __init__(self, options: dict[str, object], *, fail: bool = False) -> None:
        self.options = options
        self.fail = fail

    def __enter__(self) -> FakeYdl:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
        if self.fail:
            raise RuntimeError("transport included private headers")
        output_template = Path(str(self.options["outtmpl"]))
        output_template.parent.mkdir(parents=True, exist_ok=True)
        (output_template.parent / "source.mp4").write_bytes(b"video")
        (output_template.parent / "source.zh.vtt").write_text(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好\n",
            encoding="utf-8",
        )
        return {"id": "video-123", "title": "公开课程"}


def test_ytdlp_downloader_returns_task_relative_media_and_subtitle(
    tmp_path: Path,
) -> None:
    from learnnest.downloader import YtDlpDownloader

    options_seen: list[dict[str, object]] = []

    def factory(options: dict[str, object]) -> FakeYdl:
        options_seen.append(options)
        return FakeYdl(options)

    source = SourceItem(
        input="https://example.com/watch?v=1",
        input_type="url",
    )
    acquired = YtDlpDownloader(ydl_factory=factory).acquire(
        source,
        tmp_path,
        source_fingerprint="a1b2c3d4",
    )

    assert acquired.media_path == "downloads/source.mp4"
    assert acquired.subtitle_path == "downloads/source.zh.vtt"
    assert acquired.platform_id == "video-123"
    assert acquired.platform_title == "公开课程"
    options = options_seen[0]
    assert options["noplaylist"] is True
    assert options["writesubtitles"] is True
    assert options["writeautomaticsub"] is True
    assert options["noprogress"] is True
    assert options["logger"].error("must stay internal") is None
    assert "cookiefile" not in options
    assert "http_headers" not in options


def test_ytdlp_downloader_keeps_runtime_cookie_in_memory_http_headers(
    tmp_path: Path,
) -> None:
    from learnnest.downloader import YtDlpDownloader

    options_seen: list[dict[str, object]] = []

    def factory(options: dict[str, object]) -> FakeYdl:
        options_seen.append(options)
        return FakeYdl(options)

    source = SourceItem(
        input="https://www.douyin.com/video/101",
        input_type="url",
    )
    YtDlpDownloader(
        cookie=SecretStr("sessionid=runtime-only"),
        ydl_factory=factory,
    ).acquire(source, tmp_path, source_fingerprint="a1b2c3d4")

    assert options_seen[0]["http_headers"] == {"Cookie": "sessionid=runtime-only"}
    assert options_seen[0]["cachedir"] is False
    assert "cookiefile" not in options_seen[0]


def test_ytdlp_downloader_returns_a_safe_local_fallback_error(tmp_path: Path) -> None:
    from learnnest.downloader import DownloaderError, YtDlpDownloader

    source = SourceItem(
        input="https://example.com/watch?v=1",
        input_type="url",
    )

    with pytest.raises(DownloaderError) as captured:
        YtDlpDownloader(
            ydl_factory=lambda options: FakeYdl(options, fail=True)
        ).acquire(source, tmp_path, source_fingerprint="a1b2c3d4")

    message = str(captured.value)
    assert "手动下载" in message
    assert "RuntimeError" in message
    assert "private headers" not in message


def test_ytdlp_downloader_explains_when_douyin_requires_fresh_cookies(
    tmp_path: Path,
) -> None:
    from learnnest.downloader import DownloaderError, YtDlpDownloader

    class FreshCookieYdl(FakeYdl):
        def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
            del url, download
            raise RuntimeError(
                "ERROR: [Douyin] Fresh cookies (not necessarily logged in) are needed"
            )

    source = SourceItem(
        input="https://www.douyin.com/video/101",
        input_type="url",
    )

    with pytest.raises(DownloaderError) as captured:
        YtDlpDownloader(ydl_factory=FreshCookieYdl).acquire(
            source,
            tmp_path,
            source_fingerprint="a1b2c3d4",
        )

    assert str(captured.value) == (
        "抖音下载需要有效登录状态；请在来源页确认已连接抖音后重试。"
    )


def test_ytdlp_downloader_rejects_local_source_items(tmp_path: Path) -> None:
    from learnnest.downloader import YtDlpDownloader

    source = SourceItem(input="C:/video.mp4", input_type="local_file")

    with pytest.raises(ValueError, match="URL source"):
        YtDlpDownloader(ydl_factory=lambda options: FakeYdl(options)).acquire(
            source,
            tmp_path,
            source_fingerprint="a1b2c3d4",
        )
