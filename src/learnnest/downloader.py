"""Narrow yt-dlp adapter that acquires public URL media into one task."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from yt_dlp import YoutubeDL

from learnnest.douyin_url import is_douyin_canonical_url
from learnnest.source_models import AcquiredSource, SourceItem
from learnnest.sources import VIDEO_EXTENSIONS


class DownloaderError(RuntimeError):
    """A safe URL acquisition failure with a local-file fallback hint."""


class YtDlpDownloader:
    """Acquire one URL without shell command construction.

    Credentials are optional and runtime-only.  When supplied, yt-dlp receives
    the Cookie header in memory; no cookie file or persisted downloader config
    is created.
    """

    def __init__(
        self,
        *,
        cookie: SecretStr | None = None,
        ydl_factory: Callable[[dict[str, object]], Any] = YoutubeDL,
    ) -> None:
        if cookie is not None and not cookie.get_secret_value().strip():
            raise ValueError("yt-dlp runtime cookie must not be empty")
        self._cookie = cookie
        self._ydl_factory = ydl_factory

    def acquire(
        self,
        source: SourceItem,
        task_dir: Path,
        *,
        source_fingerprint: str,
    ) -> AcquiredSource:
        if source.input_type != "url":
            raise ValueError("yt-dlp downloader requires a URL source")
        root = task_dir.resolve()
        downloads = root / "downloads"
        options: dict[str, object] = {
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "noprogress": True,
            "no_warnings": True,
            "outtmpl": str(downloads / "source.%(ext)s"),
            "quiet": True,
            "logger": _SilentYtDlpLogger(),
            "subtitlesformat": "vtt/srt/best",
            "subtitleslangs": ["zh-Hans", "zh-CN", "zh", "en"],
            "writeautomaticsub": True,
            "writesubtitles": True,
        }
        if self._cookie is not None:
            options["http_headers"] = {
                "Cookie": self._cookie.get_secret_value(),
            }
            options["cachedir"] = False
        try:
            with self._ydl_factory(options) as downloader:
                info = downloader.extract_info(source.input, download=True)
            media = _find_downloaded_media(downloads)
            subtitle = _find_downloaded_subtitle(downloads)
        except Exception as error:
            if _douyin_requires_fresh_cookies(source.input, error):
                raise DownloaderError(
                    "抖音下载需要有效登录状态；请在来源页确认已连接抖音后重试。"
                ) from error
            raise DownloaderError(
                f"URL 下载失败；可手动下载视频后按本地文件处理 ({type(error).__name__})"
            ) from error
        if not isinstance(info, dict):
            raise DownloaderError(
                "URL 下载未返回有效元数据；可手动下载视频后按本地文件处理"
            )
        return AcquiredSource(
            source_input=source.input,
            source_type="url",
            source_fingerprint=source_fingerprint,
            media_path=media.relative_to(root).as_posix(),
            platform_id=_optional_text(info.get("id")),
            platform_title=_optional_text(info.get("title")),
            subtitle_path=(
                subtitle.relative_to(root).as_posix() if subtitle is not None else None
            ),
        )


def _find_downloaded_media(downloads: Path) -> Path:
    candidates = sorted(
        path
        for path in downloads.glob("source.*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )
    if len(candidates) != 1:
        raise DownloaderError("yt-dlp did not produce exactly one media file")
    return candidates[0]


def _find_downloaded_subtitle(downloads: Path) -> Path | None:
    subtitles = sorted(
        (
            path
            for path in downloads.glob("source.*")
            if path.is_file() and path.suffix.lower() in {".srt", ".vtt"}
        ),
        key=lambda path: (_subtitle_priority(path.name), path.name.casefold()),
    )
    return subtitles[0] if subtitles else None


def _subtitle_priority(name: str) -> int:
    lowered = name.casefold()
    for index, language in enumerate(("zh-hans", "zh-cn", ".zh.", ".en.")):
        if language in lowered:
            return index
    return 99


def _optional_text(value: object) -> str | None:
    return str(value).strip() if value is not None and str(value).strip() else None


def _douyin_requires_fresh_cookies(source: str, error: Exception) -> bool:
    """Only canonical video URLs claim the douyin login explanation."""
    return is_douyin_canonical_url(source) and (
        "fresh cookies" in str(error).casefold()
    )


class _SilentYtDlpLogger:
    """Keep provider output inside the adapter's stable error boundary."""

    def debug(self, message: str) -> None:
        del message

    def warning(self, message: str) -> None:
        del message

    def error(self, message: str) -> None:
        del message
