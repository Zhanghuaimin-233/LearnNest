"""Official-page synchronization and safe local projection for Douyin favorites."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import SecretStr

from learnnest.adapters.douyin import DouyinAdapterError
from learnnest.adapters.douyin_http import (
    DouyinAuthenticationError,
    DouyinHttpRequestError,
)
from learnnest.adapters.douyin_official_page import (
    DouyinOfficialPageError,
    DouyinOfficialPageTransport,
)

_FAVORITES_DIRECTORY = Path(".learnnest") / "douyin"
_FACTS_FILENAME = "favorites.json"
_SYNC_STATUS_FILENAME = "sync_status.json"
_THUMBNAIL_DIRECTORY = "thumbnails"
DEFAULT_DOUYIN_FOLDER_ID = "default"
_IMAGE_SUFFIXES = frozenset({".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"})
_CONTENT_TYPES = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024
_DEFAULT_PAGE_SIZE = 20
_DEFAULT_MAX_PAGES = 50
_SYNC_STATUS_STATES = frozenset(
    {"idle", "syncing", "success", "reconnect_required", "failed"}
)


class DouyinFavoritesError(RuntimeError):
    """A safe synchronization or local snapshot error."""


class FavoritesTransport(Protocol):
    def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]: ...


class FavoritesTransportFactory(Protocol):
    def __call__(self, cookie: SecretStr) -> FavoritesTransport: ...


class ThumbnailOpener(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> Any: ...


@dataclass(frozen=True)
class DouyinFavorite:
    aweme_id: str
    title: str
    url: str
    synced_at: str
    thumbnail_path: str | None
    folder_ids: tuple[str, ...] = (DEFAULT_DOUYIN_FOLDER_ID,)

    def payload(self) -> dict[str, Any]:
        return {
            "aweme_id": self.aweme_id,
            "title": self.title,
            "url": self.url,
            "synced_at": self.synced_at,
            "thumbnail_path": self.thumbnail_path,
            "folder_ids": list(self.folder_ids),
        }


@dataclass(frozen=True)
class DouyinFavoritesFolder:
    folder_id: str
    name: str
    item_count: int

    def payload(self) -> dict[str, Any]:
        return {
            "folder_id": self.folder_id,
            "name": self.name,
            "item_count": self.item_count,
        }


@dataclass(frozen=True)
class DouyinFavoritesSnapshot:
    synced_at: str | None
    items: tuple[DouyinFavorite, ...]
    folders: tuple[DouyinFavoritesFolder, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "synced_at": self.synced_at,
            "folders": [folder.payload() for folder in self.folders],
            "items": [item.payload() for item in self.items],
        }


@dataclass(frozen=True)
class DouyinFavoritesSyncStatus:
    """A safe, human-facing fact about the latest automatic sync attempt.

    Never carries cookies, signatures, request bodies, or raw provider
    responses; only a limited state name, a localized next step, and a time.
    """

    state: str = "idle"
    message: str = ""
    checked_at: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "message": self.message,
            "checked_at": self.checked_at,
        }


class DouyinFavoritesStore:
    """Keep only stable favorite facts and local thumbnail paths on disk."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        transport_factory: FavoritesTransportFactory | None = None,
        thumbnail_opener: ThumbnailOpener | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
        max_pages: int = _DEFAULT_MAX_PAGES,
        timeout: float = 20.0,
    ) -> None:
        if page_size < 1:
            raise ValueError("Douyin favorite page size must be positive")
        if max_pages < 1:
            raise ValueError("Douyin favorite max pages must be positive")
        if timeout <= 0:
            raise ValueError("Douyin thumbnail timeout must be positive")
        self.output_root = Path(output_root).resolve()
        self.directory = self.output_root / _FAVORITES_DIRECTORY
        self.facts_path = self.directory / _FACTS_FILENAME
        self.thumbnail_directory = self.directory / _THUMBNAIL_DIRECTORY
        self._uses_default_transport = transport_factory is None
        self._transport_factory = transport_factory or _default_transport_factory
        self._thumbnail_opener = thumbnail_opener or urlopen
        self._page_size = page_size
        self._max_pages = max_pages
        self._timeout = timeout

    def read_snapshot(self) -> DouyinFavoritesSnapshot:
        try:
            payload = json.loads(self.facts_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return DouyinFavoritesSnapshot(synced_at=None, items=())
        return _safe_snapshot(payload, self.thumbnail_directory)

    def read_sync_status(self) -> DouyinFavoritesSyncStatus:
        try:
            payload = json.loads(
                self.directory.joinpath(_SYNC_STATUS_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return DouyinFavoritesSyncStatus()
        return _safe_sync_status(payload)

    def write_sync_status(
        self,
        state: str,
        message: str,
        *,
        checked_at: str | None = None,
    ) -> None:
        if state not in _SYNC_STATUS_STATES:
            raise ValueError("invalid douyin sync status state")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("douyin sync status message must be safe text")
        _atomic_write_json(
            self.directory / _SYNC_STATUS_FILENAME,
            DouyinFavoritesSyncStatus(
                state=state,
                message=message.strip(),
                checked_at=checked_at or _now(),
            ).payload(),
        )

    def sync(
        self,
        cookie: SecretStr,
        *,
        browser_storage_state: Mapping[str, Any] | None = None,
        on_authentication_failure: Callable[[], None] | None = None,
        on_collected: Callable[[DouyinFavoritesSnapshot, DouyinFavoritesSnapshot], None]
        | None = None,
    ) -> DouyinFavoritesSnapshot:
        previous = self.read_snapshot()
        previous_by_id = {item.aweme_id: item for item in previous.items}
        try:
            transport = (
                DouyinOfficialPageTransport(
                    cookie,
                    storage_state=browser_storage_state,
                    include_custom_folders=True,
                )
                if self._uses_default_transport
                else self._transport_factory(cookie)
            )
            raw_items = self._collect_pages(transport)
            custom_folders = self._collect_custom_folders(transport)
        except DouyinAuthenticationError as error:
            if (
                error.reason != "request_rejected"
                and on_authentication_failure is not None
            ):
                on_authentication_failure()
            raise
        except DouyinOfficialPageError as error:
            if error.reason == "pagination_stalled":
                raise DouyinFavoritesError(
                    "抖音官方页面已返回首批收藏，但继续加载下一批时没有响应；"
                    "本次收藏未更新，请重试。"
                ) from None
            if error.reason == "no_response":
                raise DouyinFavoritesError(
                    "抖音官方页面没有发起可验证的收藏请求；"
                    "本次收藏未更新，请重新连接或稍后重试。"
                ) from None
            if error.reason == "http_error":
                raise DouyinFavoritesError(
                    f"抖音官方页收藏请求返回 HTTP {error.status_code or '未知'}；"
                    "本次收藏未更新，请稍后重试。"
                ) from None
            if error.reason == "business_error":
                raise DouyinFavoritesError(
                    f"抖音官方页收藏请求返回业务状态 {error.status_code or '未知'}；"
                    "本次收藏未更新，请重新验证登录或稍后重试。"
                ) from None
            if error.reason == "runtime_unavailable":
                raise DouyinFavoritesError(
                    "抖音官方网页的请求组件已变化，无法生成本次 HTTP 请求签名；"
                    "本次收藏未更新。"
                ) from None
            if error.reason == "network_error":
                raise DouyinFavoritesError(
                    "抖音官方运行时生成首批收藏请求时网络连接失败；"
                    "本次收藏未更新，请重试。"
                ) from None
            raise DouyinFavoritesError(
                "抖音官方页收藏响应格式已变化；本次收藏未更新。"
            ) from None
        except DouyinHttpRequestError as error:
            if error.reason == "http_error":
                raise DouyinFavoritesError(
                    f"后端直接请求收藏下一页时返回 HTTP "
                    f"{error.status_code or '未知'}；本次收藏未更新。"
                ) from None
            if error.reason == "network_error":
                raise DouyinFavoritesError(
                    "后端直接请求收藏下一页时网络连接失败；本次收藏未更新，请重试。"
                ) from None
            raise DouyinFavoritesError(
                "后端直接请求收藏下一页时收到非 JSON 或非对象响应；"
                "抖音接口格式可能已变化，本次收藏未更新。"
            ) from None
        except DouyinAdapterError as error:
            if _is_authentication_error(error):
                if on_authentication_failure is not None:
                    on_authentication_failure()
                raise DouyinAuthenticationError(
                    "Douyin authentication failed"
                ) from None
            raise DouyinFavoritesError(
                "抖音官方页面没有返回可验证的收藏结果；收藏未更新，请稍后重试。"
            ) from None
        except (OSError, ValueError, TypeError) as error:
            raise DouyinFavoritesError("抖音收藏同步失败。") from error

        ordered_ids: list[str] = []
        raw_by_id: dict[str, Mapping[str, Any]] = {}
        folder_ids_by_item: dict[str, list[str]] = {}
        default_ids: set[str] = set()
        for raw in raw_items:
            item_id = _aweme_id(raw)
            if item_id is None:
                continue
            if item_id not in raw_by_id:
                ordered_ids.append(item_id)
                raw_by_id[item_id] = raw
            default_ids.add(item_id)
            folder_ids_by_item.setdefault(item_id, []).append(DEFAULT_DOUYIN_FOLDER_ID)
        folder_facts: list[DouyinFavoritesFolder] = [
            DouyinFavoritesFolder(
                folder_id=DEFAULT_DOUYIN_FOLDER_ID,
                name="默认收藏夹",
                item_count=len(default_ids),
            )
        ]
        for folder_id, name, folder_items in custom_folders:
            contained: set[str] = set()
            for raw in folder_items:
                item_id = _aweme_id(raw)
                if item_id is None or item_id in contained:
                    continue
                contained.add(item_id)
                if item_id not in raw_by_id:
                    ordered_ids.append(item_id)
                    raw_by_id[item_id] = raw
                elif _cover_url(raw_by_id[item_id]) is None and _cover_url(raw):
                    raw_by_id[item_id] = raw
                memberships = folder_ids_by_item.setdefault(item_id, [])
                if folder_id not in memberships:
                    memberships.append(folder_id)
            folder_facts.append(
                DouyinFavoritesFolder(
                    folder_id=folder_id,
                    name=name,
                    item_count=len(contained),
                )
            )

        synced_at = _now()
        items: list[DouyinFavorite] = []
        for item_id in ordered_ids:
            raw = raw_by_id[item_id]
            title = _title(raw, item_id)
            thumbnail_path = self._download_thumbnail(
                item_id,
                _cover_url(raw),
                previous_by_id.get(item_id),
            )
            items.append(
                DouyinFavorite(
                    aweme_id=item_id,
                    title=title,
                    url=f"https://www.douyin.com/video/{item_id}",
                    synced_at=synced_at,
                    thumbnail_path=thumbnail_path,
                    folder_ids=tuple(folder_ids_by_item[item_id]),
                )
            )
        snapshot = DouyinFavoritesSnapshot(
            synced_at=synced_at,
            items=tuple(items),
            folders=tuple(folder_facts),
        )
        # Any durable intent for a new favorite is persisted before the
        # baseline advances: a publish failure must never consume new
        # identities without leaving a recoverable task intent behind.
        if on_collected is not None:
            on_collected(previous, snapshot)
        _atomic_write_json(self.facts_path, snapshot.payload())
        return snapshot

    def _collect_custom_folders(
        self,
        transport: FavoritesTransport,
    ) -> list[tuple[str, str, list[Mapping[str, Any]]]]:
        list_folders = getattr(transport, "list_folders", None)
        list_folder_items = getattr(transport, "list_folder_items", None)
        if not callable(list_folders) or not callable(list_folder_items):
            return []
        cursor = 0
        folders: list[tuple[str, str, list[Mapping[str, Any]]]] = []
        seen_folders: set[str] = set()
        has_more = False
        for _ in range(self._max_pages):
            payload = list_folders(cursor=cursor, count=self._page_size)
            if not isinstance(payload, Mapping):
                raise DouyinFavoritesError("抖音收藏夹响应格式无效。")
            _validate_status(payload)
            raw_folders = payload.get("collects_list", [])
            if not isinstance(raw_folders, list):
                raise DouyinFavoritesError("抖音收藏夹响应格式无效。")
            page_ids: list[str] = []
            for raw_folder in raw_folders:
                if not isinstance(raw_folder, Mapping):
                    continue
                folder_id = _folder_id(raw_folder)
                if folder_id is None or folder_id in seen_folders:
                    continue
                name = _folder_name(raw_folder, folder_id)
                page_ids.append(folder_id)
                seen_folders.add(folder_id)
                folders.append(
                    (
                        folder_id,
                        name,
                        self._collect_folder_items(list_folder_items, folder_id),
                    )
                )
            has_more = bool(payload.get("has_more"))
            if not has_more:
                break
            if not page_ids:
                raise DouyinFavoritesError(
                    "抖音收藏夹下一页没有返回有效目录；本次收藏未更新。"
                )
            next_cursor = _cursor_value(payload.get("cursor"))
            if next_cursor == cursor:
                raise DouyinFavoritesError(
                    "抖音收藏夹下一页游标没有前进；本次收藏未更新。"
                )
            cursor = next_cursor
        if has_more:
            raise DouyinFavoritesError(
                f"抖音收藏夹超过安全分页上限（{self._max_pages} 页）；本次收藏未更新。"
            )
        return folders

    def _collect_folder_items(
        self,
        fetch: Callable[..., Mapping[str, Any]],
        folder_id: str,
    ) -> list[Mapping[str, Any]]:
        cursor = 0
        items: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        has_more = False
        for _ in range(self._max_pages):
            payload = fetch(folder_id, cursor=cursor, count=self._page_size)
            if not isinstance(payload, Mapping):
                raise DouyinFavoritesError("抖音收藏夹内容响应格式无效。")
            _validate_status(payload)
            raw_items = payload.get("aweme_list", [])
            if not isinstance(raw_items, list):
                raise DouyinFavoritesError("抖音收藏夹内容响应格式无效。")
            page_ids: list[str] = []
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    continue
                item_id = _aweme_id(raw)
                if item_id is None:
                    continue
                page_ids.append(item_id)
                if item_id not in seen:
                    seen.add(item_id)
                    items.append(raw)
            has_more = bool(payload.get("has_more"))
            if not has_more:
                break
            if not page_ids:
                raise DouyinFavoritesError(
                    "抖音收藏夹内容下一页没有返回有效作品；本次收藏未更新。"
                )
            next_cursor = _cursor_value(payload.get("cursor"))
            if next_cursor == cursor:
                raise DouyinFavoritesError(
                    "抖音收藏夹内容下一页游标没有前进；本次收藏未更新。"
                )
            cursor = next_cursor
        if has_more:
            raise DouyinFavoritesError(
                f"抖音收藏夹内容超过安全分页上限（{self._max_pages} 页）；"
                "本次收藏未更新。"
            )
        return items

    def thumbnail_file(self, relative_path: str) -> Path:
        snapshot = self.read_snapshot()
        declared = {
            item.thumbnail_path
            for item in snapshot.items
            if item.thumbnail_path is not None
        }
        if relative_path not in declared:
            raise FileNotFoundError(relative_path)
        relative = _safe_thumbnail_path(relative_path)
        if relative is None:
            raise FileNotFoundError(relative_path)
        root = self.thumbnail_directory.resolve()
        candidate = (self.directory / relative).resolve()
        if (
            not candidate.is_relative_to(root)
            or not candidate.is_file()
            or candidate.suffix.lower() not in _IMAGE_SUFFIXES
        ):
            raise FileNotFoundError(relative_path)
        return candidate

    def _collect_pages(self, transport: FavoritesTransport) -> list[Mapping[str, Any]]:
        cursor = 0
        collected: list[Mapping[str, Any]] = []
        seen_ids: set[str] = set()
        has_more = False
        for _ in range(self._max_pages):
            payload = transport.list_video_favorites(
                cursor=cursor,
                count=self._page_size,
            )
            if not isinstance(payload, Mapping):
                raise DouyinFavoritesError("抖音收藏响应格式无效。")
            _validate_status(payload)
            raw_items = payload.get("aweme_list", [])
            if not isinstance(raw_items, list):
                raise DouyinFavoritesError("抖音收藏响应格式无效。")
            page_ids: list[str] = []
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    continue
                item_id = _aweme_id(raw)
                if item_id is None:
                    continue
                page_ids.append(item_id)
                if item_id not in seen_ids:
                    seen_ids.add(item_id)
                    collected.append(raw)
            has_more = bool(payload.get("has_more"))
            if not has_more:
                break
            if not page_ids:
                raise DouyinFavoritesError(
                    "抖音收藏下一页标记为仍有内容，但没有返回有效作品；本次收藏未更新。"
                )
            next_cursor = _cursor_value(payload.get("cursor"))
            if next_cursor == cursor:
                raise DouyinFavoritesError(
                    "抖音收藏下一页游标没有前进；本次收藏未更新。"
                )
            cursor = next_cursor
        if has_more:
            raise DouyinFavoritesError(
                f"抖音收藏超过安全分页上限（{self._max_pages} 页）；本次收藏未更新。"
            )
        if not collected:
            raise DouyinFavoritesError("抖音收藏响应未返回有效作品。")
        return collected

    def _download_thumbnail(
        self,
        item_id: str,
        remote_url: str | None,
        previous: DouyinFavorite | None,
    ) -> str | None:
        if remote_url is None:
            return _existing_thumbnail(previous, self.thumbnail_directory)
        try:
            request = Request(
                remote_url,
                headers={
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LearnNest",
                },
                method="GET",
            )
            with self._thumbnail_opener(request, timeout=self._timeout) as response:
                status = getattr(response, "status", None)
                if status is not None and not 200 <= int(status) < 300:
                    raise OSError
                data = response.read(_MAX_THUMBNAIL_BYTES + 1)
                if (
                    not isinstance(data, bytes)
                    or not data
                    or len(data) > _MAX_THUMBNAIL_BYTES
                ):
                    raise OSError
                content_type = _content_type(response)
            suffix = _thumbnail_suffix(remote_url, content_type, data)
            relative = f"{_THUMBNAIL_DIRECTORY}/{item_id}{suffix}"
            _atomic_write_bytes(self.directory / relative, data)
            return relative
        except (HTTPError, OSError, URLError, TypeError, ValueError):
            return _existing_thumbnail(previous, self.thumbnail_directory)
        except Exception:
            return _existing_thumbnail(previous, self.thumbnail_directory)


def _default_transport_factory(cookie: SecretStr) -> FavoritesTransport:
    return DouyinOfficialPageTransport(cookie)


def _safe_snapshot(
    payload: object, thumbnail_directory: Path
) -> DouyinFavoritesSnapshot:
    if not isinstance(payload, Mapping):
        return DouyinFavoritesSnapshot(synced_at=None, items=())
    synced_at = payload.get("synced_at")
    safe_synced_at = synced_at if isinstance(synced_at, str) else None
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return DouyinFavoritesSnapshot(synced_at=safe_synced_at, items=())
    raw_folders = payload.get("folders")
    folders_by_id: dict[str, str] = {}
    folder_order: list[str] = []
    if isinstance(raw_folders, list):
        for raw_folder in raw_folders:
            if not isinstance(raw_folder, Mapping):
                continue
            folder_id = raw_folder.get("folder_id")
            name = raw_folder.get("name")
            if (
                not _safe_folder_id(folder_id)
                or folder_id in folders_by_id
                or not isinstance(name, str)
                or not name.strip()
            ):
                continue
            folders_by_id[folder_id] = name.strip()
            folder_order.append(folder_id)
    if DEFAULT_DOUYIN_FOLDER_ID not in folders_by_id:
        folders_by_id = {
            DEFAULT_DOUYIN_FOLDER_ID: "默认收藏夹",
            **folders_by_id,
        }
        folder_order.insert(0, DEFAULT_DOUYIN_FOLDER_ID)

    items: list[DouyinFavorite] = []
    seen: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        item_id = raw.get("aweme_id")
        title = raw.get("title")
        item_synced_at = raw.get("synced_at")
        if (
            not isinstance(item_id, str)
            or not item_id.isdigit()
            or item_id in seen
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(item_synced_at, str)
        ):
            continue
        thumbnail_path = raw.get("thumbnail_path")
        safe_thumbnail = (
            thumbnail_path
            if isinstance(thumbnail_path, str)
            and _safe_thumbnail_path(thumbnail_path) is not None
            and (thumbnail_directory.parent / thumbnail_path)
            .resolve()
            .is_relative_to(thumbnail_directory.resolve())
            else None
        )
        raw_item_folders = raw.get("folder_ids")
        safe_item_folders = (
            tuple(
                dict.fromkeys(
                    folder_id
                    for folder_id in raw_item_folders
                    if isinstance(folder_id, str) and folder_id in folders_by_id
                )
            )
            if isinstance(raw_item_folders, list)
            else (DEFAULT_DOUYIN_FOLDER_ID,)
        )
        if not safe_item_folders:
            safe_item_folders = (DEFAULT_DOUYIN_FOLDER_ID,)
        items.append(
            DouyinFavorite(
                aweme_id=item_id,
                title=title,
                url=f"https://www.douyin.com/video/{item_id}",
                synced_at=item_synced_at,
                thumbnail_path=safe_thumbnail,
                folder_ids=safe_item_folders,
            )
        )
        seen.add(item_id)
    counts = {
        folder_id: sum(folder_id in item.folder_ids for item in items)
        for folder_id in folder_order
    }
    folders = tuple(
        DouyinFavoritesFolder(
            folder_id=folder_id,
            name=folders_by_id[folder_id],
            item_count=counts[folder_id],
        )
        for folder_id in folder_order
    )
    return DouyinFavoritesSnapshot(
        synced_at=safe_synced_at,
        items=tuple(items),
        folders=folders,
    )


def _safe_sync_status(payload: object) -> DouyinFavoritesSyncStatus:
    if not isinstance(payload, Mapping):
        return DouyinFavoritesSyncStatus()
    state = payload.get("state")
    message = payload.get("message")
    checked_at = payload.get("checked_at")
    return DouyinFavoritesSyncStatus(
        state=state
        if isinstance(state, str) and state in _SYNC_STATUS_STATES
        else "idle",
        message=message if isinstance(message, str) else "",
        checked_at=checked_at if isinstance(checked_at, str) else None,
    )


def _validate_status(payload: Mapping[str, Any]) -> None:
    status = payload.get("status_code")
    if status == 0 and not isinstance(status, bool):
        return
    if str(status) in {"401", "403", "-1", "1001", "1002"}:
        raise DouyinAuthenticationError(
            "Douyin authentication failed",
            reason="business_rejected",
            status_code=str(status),
        )
    raise DouyinFavoritesError(f"抖音收藏接口返回业务状态 {status}；本次收藏未更新。")


def _is_authentication_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in ("authentication", "status_code=401", "status_code=403")
    )


def _aweme_id(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("aweme_id") or raw.get("aweme_id_str")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value.isdigit() else None


def _folder_id(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("collects_id_str") or raw.get("collects_id")
    if isinstance(value, int):
        value = str(value)
    if not _safe_folder_id(value) or value == DEFAULT_DOUYIN_FOLDER_ID:
        return None
    return value


def _safe_folder_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and (value == DEFAULT_DOUYIN_FOLDER_ID or value.isdigit())
    )


def _folder_name(raw: Mapping[str, Any], folder_id: str) -> str:
    value = raw.get("collects_name")
    return value.strip() if isinstance(value, str) and value.strip() else folder_id


def _title(raw: Mapping[str, Any], item_id: str) -> str:
    for key in ("desc", "item_title", "title"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"抖音作品 {item_id}"


def _cover_url(raw: Mapping[str, Any]) -> str | None:
    candidates: list[object] = []
    video = raw.get("video")
    if isinstance(video, Mapping):
        candidates.extend(
            video.get(key) for key in ("cover", "origin_cover", "dynamic_cover")
        )
    candidates.extend(raw.get(key) for key in ("cover", "cover_url", "thumbnail"))
    for candidate in candidates:
        url = _first_url(candidate)
        if url is None:
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme == "https" and parsed.netloc and not parsed.username:
            return url
    return None


def _first_url(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for key in ("url_list", "url", "uri"):
            result = _first_url(value.get(key))
            if result is not None:
                return result
        return None
    if isinstance(value, list):
        for item in value:
            result = _first_url(item)
            if result is not None:
                return result
    return None


def _cursor_value(value: object) -> int:
    try:
        cursor = int(value or 0)
    except (TypeError, ValueError) as error:
        raise DouyinFavoritesError("抖音收藏响应格式无效。") from error
    if cursor < 0:
        raise DouyinFavoritesError("抖音收藏响应格式无效。")
    return cursor


def _content_type(response: Any) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get_content_type()
    except AttributeError:
        value = headers.get("Content-Type") if hasattr(headers, "get") else None
    if not isinstance(value, str):
        return None
    return value.split(";", 1)[0].strip().lower()


def _thumbnail_suffix(remote_url: str, content_type: str | None, data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if content_type in _CONTENT_TYPES:
        return _CONTENT_TYPES[content_type]
    suffix = Path(urlsplit(remote_url).path).suffix.lower()
    return suffix if suffix in _IMAGE_SUFFIXES else ".jpg"


def _safe_thumbnail_path(value: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        path = PurePosixPath(value)
    except (TypeError, ValueError):
        return None
    if (
        path.is_absolute()
        or "\\" in value
        or len(path.parts) != 2
        or path.parts[0] != _THUMBNAIL_DIRECTORY
        or path.parts[1] in {"", ".", ".."}
        or Path(path.parts[1]).suffix.lower() not in _IMAGE_SUFFIXES
    ):
        return None
    return path.as_posix()


def _existing_thumbnail(
    previous: DouyinFavorite | None, thumbnail_directory: Path
) -> str | None:
    if previous is None or previous.thumbnail_path is None:
        return None
    relative = _safe_thumbnail_path(previous.thumbnail_path)
    if relative is None:
        return None
    if (thumbnail_directory.parent / relative).resolve().is_file():
        return relative
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _atomic_write_bytes(path, encoded)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
