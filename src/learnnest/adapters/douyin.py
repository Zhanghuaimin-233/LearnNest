"""Pure parsing and pagination rules for the Douyin favorites monitor."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from learnnest.adapters.base import ScanDiscovery
from learnnest.discovery_models import DiscoveredLink
from learnnest.source_models import SourceItem

_CURSOR_VERSION = "douyin-favorites-v1"


class DouyinTransport(Protocol):
    """Runtime HTTP/API transport; it owns credentials and signatures."""

    def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]: ...

    def list_folders(self, *, cursor: int, count: int) -> Mapping[str, Any]: ...

    def list_folder_items(
        self,
        folder_id: str,
        *,
        cursor: int,
        count: int,
    ) -> Mapping[str, Any]: ...


class DouyinAdapterError(RuntimeError):
    """A safe adapter failure without raw response or credential material."""


class DouyinFavoritesAdapter:
    """Discover default video favorites and selected/all user folders.

    The transport is deliberately injected.  The normal implementation is a
    pure HTTP client; a browser/page implementation may remain an optional
    fallback, but the adapter only receives JSON-shaped response data and
    emits stable logical links.
    """

    discovery_only = True

    def __init__(
        self,
        transport: DouyinTransport,
        *,
        folder_ids: Sequence[str] | None = None,
        include_default_video: bool = True,
        count: int = 20,
        max_pages: int = 50,
    ) -> None:
        if not include_default_video and folder_ids == ():
            raise ValueError("Douyin monitor must include video or a folder scope")
        if count < 1:
            raise ValueError("Douyin page count must be positive")
        if max_pages < 1:
            raise ValueError("Douyin max_pages must be positive")
        normalized_folders = None
        if folder_ids is not None:
            normalized_folders = tuple(dict.fromkeys(str(item) for item in folder_ids))
            if any(not item for item in normalized_folders):
                raise ValueError("Douyin folder IDs must not be empty")
        self.transport = transport
        self.folder_ids = normalized_folders
        self.include_default_video = include_default_video
        self.count = count
        self.max_pages = max_pages

    @property
    def scan_key(self) -> str:
        scope = "all" if self.folder_ids is None else ",".join(self.folder_ids)
        material = f"{int(self.include_default_video)}\0{scope}".encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()[:16]
        return f"douyin-{digest}"

    def discover(self, cursor: str | None) -> ScanDiscovery:
        state = _parse_cursor(cursor)
        seen_before = set(state["video_seen"])
        seen_before.update(
            item_id for values in state["folder_seen"].values() for item_id in values
        )
        new_ids: list[str] = []
        observed: dict[str, DiscoveredLink] = {}
        observed_order: list[str] = []

        if self.include_default_video:
            video_seen = set(state["video_seen"])
            for raw in _paged_items(
                lambda page_cursor: self.transport.list_video_favorites(
                    cursor=page_cursor,
                    count=self.count,
                ),
                item_key="aweme_list",
                seen=video_seen,
                max_pages=self.max_pages,
            ):
                link = _link_from_item(raw)
                if link is None:
                    continue
                _observe(observed, observed_order, link)
                if (
                    link.platform_id not in seen_before
                    and link.platform_id not in new_ids
                ):
                    new_ids.append(link.platform_id)
            state["video_seen"] = sorted(video_seen)

        folders = self._folder_catalog(state)
        for folder_id, folder_name in folders:
            folder_seen = set(state["folder_seen"].get(folder_id, []))
            for raw in _paged_items(
                lambda page_cursor, selected=folder_id: (
                    self.transport.list_folder_items(
                        selected,
                        cursor=page_cursor,
                        count=self.count,
                    )
                ),
                item_key="aweme_list",
                seen=folder_seen,
                max_pages=self.max_pages,
            ):
                link = _link_from_item(
                    raw, folder_id=folder_id, folder_name=folder_name
                )
                if link is None:
                    continue
                _observe(observed, observed_order, link)
                if (
                    link.platform_id not in seen_before
                    and link.platform_id not in new_ids
                ):
                    new_ids.append(link.platform_id)
            state["folder_seen"][folder_id] = sorted(folder_seen)

        links = [observed[item_id] for item_id in observed_order]
        by_id = {link.platform_id: link for link in links}
        sources = [by_id[item_id].source for item_id in new_ids if item_id in by_id]
        return ScanDiscovery(
            items=sources,
            cursor_after=_render_cursor(state),
            links=links,
        )

    def _folder_catalog(
        self,
        state: dict[str, Any],
    ) -> list[tuple[str, str]]:
        if self.folder_ids == ():
            return []
        catalog: dict[str, str] = {}
        if self.folder_ids is None or self.folder_ids:
            for raw in _paged_items(
                lambda page_cursor: self.transport.list_folders(
                    cursor=page_cursor,
                    count=self.count,
                ),
                item_key="collects_list",
                seen=set(),
                max_pages=self.max_pages,
            ):
                folder_id = _text(raw.get("collects_id_str") or raw.get("collects_id"))
                if folder_id is None:
                    continue
                folder_name = _text(raw.get("collects_name")) or folder_id
                catalog[folder_id] = folder_name
        selected = (
            sorted(catalog) if self.folder_ids is None else sorted(self.folder_ids)
        )
        for folder_id in selected:
            state["folder_seen"].setdefault(folder_id, [])
        return [
            (folder_id, catalog.get(folder_id, folder_id)) for folder_id in selected
        ]


def _paged_items(
    fetch: Any,
    *,
    item_key: str,
    seen: set[str],
    max_pages: int,
) -> list[Mapping[str, Any]]:
    cursor = 0
    collected: list[Mapping[str, Any]] = []
    for _ in range(max_pages):
        payload = fetch(cursor)
        if not isinstance(payload, Mapping):
            raise DouyinAdapterError("Douyin transport returned a non-object response")
        _validate_status(payload)
        raw_items = payload.get(item_key, [])
        if not isinstance(raw_items, list):
            raise DouyinAdapterError(f"Douyin response field {item_key} is not a list")
        page_ids: list[str] = []
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            item_id = _aweme_id(raw)
            if item_id is not None:
                page_ids.append(item_id)
                collected.append(raw)
        was_new_page = any(item_id not in seen for item_id in page_ids)
        seen.update(page_ids)
        if not bool(payload.get("has_more")) or not page_ids or not was_new_page:
            break
        next_cursor = _cursor_value(payload.get("cursor"))
        if next_cursor == cursor:
            break
        cursor = next_cursor
    return collected


def _link_from_item(
    raw: Mapping[str, Any],
    *,
    folder_id: str | None = None,
    folder_name: str | None = None,
) -> DiscoveredLink | None:
    item_id = _aweme_id(raw)
    if item_id is None:
        return None
    content_kind = _content_kind(raw)
    title = _text(raw.get("desc")) or _text(raw.get("item_title")) or f"抖音-{item_id}"
    tags = ["douyin"]
    if content_kind == "image_text":
        tags.append("image_text")
    folders = [folder_id] if folder_id is not None else []
    names = {folder_id: folder_name or folder_id} if folder_id is not None else {}
    return DiscoveredLink(
        platform_id=item_id,
        source=SourceItem(
            input=f"https://www.douyin.com/video/{item_id}",
            input_type="url",
            title=title,
            content_type=content_kind,
            tags=tags,
        ),
        content_kind=content_kind,
        folder_ids=folders,
        folder_names=names,
    )


def _observe(
    observed: dict[str, DiscoveredLink],
    order: list[str],
    link: DiscoveredLink,
) -> None:
    previous = observed.get(link.platform_id)
    if previous is None:
        observed[link.platform_id] = link
        order.append(link.platform_id)
        return
    folder_ids = sorted(set(previous.folder_ids) | set(link.folder_ids))
    folder_names = dict(previous.folder_names)
    folder_names.update(link.folder_names)
    source = previous.source
    if link.source.title and not source.title:
        source = source.model_copy(update={"title": link.source.title})
    content_kind = (
        "image_text"
        if "image_text" in {previous.content_kind, link.content_kind}
        else "video"
    )
    if source.content_type != content_kind:
        source = source.model_copy(update={"content_type": content_kind})
    observed[link.platform_id] = DiscoveredLink(
        platform_id=previous.platform_id,
        source=source,
        content_kind=content_kind,
        folder_ids=folder_ids,
        folder_names=folder_names,
    )


def _parse_cursor(cursor: str | None) -> dict[str, Any]:
    if cursor is None:
        return {"version": _CURSOR_VERSION, "video_seen": [], "folder_seen": {}}
    try:
        payload = json.loads(cursor)
    except json.JSONDecodeError as error:
        raise DouyinAdapterError("Douyin cursor is not valid JSON") from error
    if not isinstance(payload, dict) or payload.get("version") != _CURSOR_VERSION:
        raise DouyinAdapterError("unsupported Douyin cursor version")
    video_seen = payload.get("video_seen", [])
    folder_seen = payload.get("folder_seen", {})
    if not _valid_id_list(video_seen) or not isinstance(folder_seen, dict):
        raise DouyinAdapterError("Douyin cursor contains invalid seen IDs")
    if any(
        not isinstance(key, str) or not _valid_id_list(value)
        for key, value in folder_seen.items()
    ):
        raise DouyinAdapterError("Douyin cursor contains invalid folder IDs")
    return {
        "version": _CURSOR_VERSION,
        "video_seen": list(dict.fromkeys(video_seen)),
        "folder_seen": {
            key: list(dict.fromkeys(value)) for key, value in folder_seen.items()
        },
    }


def _render_cursor(state: Mapping[str, Any]) -> str:
    return json.dumps(
        {
            "version": _CURSOR_VERSION,
            "video_seen": sorted(state["video_seen"]),
            "folder_seen": {
                key: sorted(value)
                for key, value in sorted(state["folder_seen"].items())
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _valid_id_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and item for item in value
    )


def _validate_status(payload: Mapping[str, Any]) -> None:
    status = payload.get("status_code")
    if status not in (None, 0, "0"):
        raise DouyinAdapterError(f"Douyin API returned status_code={status}")


def _aweme_id(raw: Mapping[str, Any]) -> str | None:
    return _text(raw.get("aweme_id") or raw.get("aweme_id_str"))


def _content_kind(raw: Mapping[str, Any]) -> str:
    if _is_article_image_text(raw):
        return "image_text"
    if any(
        bool(raw.get(key)) for key in ("is_new_text_mode", "is_text_mode", "is_slides")
    ):
        return "image_text"
    images = raw.get("images") or raw.get("image_list") or raw.get("imageInfos")
    if isinstance(images, list) and images and not raw.get("video"):
        return "image_text"
    return "video"


def _is_article_image_text(raw: Mapping[str, Any]) -> bool:
    article = raw.get("article_info")
    if not isinstance(article, Mapping):
        return False
    if str(article.get("article_type")) == "999":
        return True
    article_content = _json_mapping(article.get("article_content"))
    if isinstance(article_content, Mapping) and isinstance(
        article_content.get("markdown"), str
    ):
        return True
    fe_data = _json_mapping(article.get("fe_data"))
    return isinstance(fe_data, Mapping) and bool(fe_data.get("image_list"))


def _json_mapping(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _cursor_value(value: object) -> int:
    try:
        selected = int(value or 0)
    except (TypeError, ValueError) as error:
        raise DouyinAdapterError("Douyin response cursor is invalid") from error
    if selected < 0:
        raise DouyinAdapterError("Douyin response cursor must not be negative")
    return selected


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
