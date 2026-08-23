"""Douyin favorites transport driven by the current official web page.

The page owns every rotating request parameter and signature. LearnNest only
observes the JSON response for the default video favorites endpoint, validates
it, and returns stable response data to the existing adapter/store boundary.
No request URL, query, body, signature, or credential value is persisted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from pydantic import SecretStr

from learnnest.adapters.douyin import DouyinAdapterError
from learnnest.adapters.douyin_http import DouyinAuthenticationError

_FAVORITES_URL = "https://www.douyin.com/user/self?showTab=favorite_collection"
_FAVORITES_PATH = "/aweme/v1/web/aweme/listcollection/"
_AUTH_STATUS_CODES = frozenset({"401", "403", "-1", "1001", "1002"})
_DEFAULT_TIMEOUT_MS = 20_000
_DEFAULT_SYNC_TIMEOUT_MS = 120_000
_DEFAULT_MAX_SCROLLS = 60
_SCROLL_DISTANCE = 6_000
_POLL_INTERVAL_MS = 100
_SCROLL_RETRY_MS = 500
_LOGGER = logging.getLogger(__name__)


class DouyinOfficialPageError(DouyinAdapterError):
    """A safe, structured failure of official-page response collection."""

    def __init__(
        self,
        message: str,
        *,
        reason: Literal[
            "no_response",
            "pagination_stalled",
            "http_error",
            "business_error",
            "invalid_response",
        ],
        status_code: int | str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


class DouyinOfficialPageTransport:
    """Collect default favorites through one isolated headed Edge context."""

    def __init__(
        self,
        cookie: SecretStr,
        *,
        storage_state: Mapping[str, Any] | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        timeout_ms: int = _DEFAULT_SYNC_TIMEOUT_MS,
        max_scrolls: int = _DEFAULT_MAX_SCROLLS,
    ) -> None:
        if not cookie.get_secret_value().strip():
            raise ValueError("Douyin cookie must not be empty")
        if timeout_ms < 1:
            raise ValueError("Douyin official page timeout must be positive")
        if max_scrolls < 1:
            raise ValueError("Douyin official page max_scrolls must be positive")
        if storage_state is not None and (
            not isinstance(storage_state.get("cookies"), list)
            or not isinstance(storage_state.get("origins"), list)
        ):
            raise ValueError("Douyin browser storage state is incomplete")
        self.cookie = cookie
        self._storage_state = dict(storage_state) if storage_state is not None else None
        self._playwright_factory = playwright_factory or _default_playwright_factory
        self._timeout_ms = timeout_ms
        self._max_scrolls = max_scrolls

    def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]:
        if cursor != 0:
            raise DouyinAdapterError(
                "Douyin official page transport only accepts the initial cursor"
            )
        if count < 1:
            raise ValueError("Douyin page count must be positive")
        runtime = browser = context = None
        try:
            runtime = self._playwright_factory()
            browser = runtime.chromium.launch(channel="msedge", headless=False)
            context = (
                browser.new_context(
                    locale="zh-CN",
                    storage_state=self._storage_state,
                )
                if self._storage_state is not None
                else browser.new_context(locale="zh-CN")
            )
            if self._storage_state is None:
                context.add_cookies(_cookie_records(self.cookie))
            page = context.new_page()
            return collect_official_favorites(
                page,
                timeout_ms=self._timeout_ms,
                max_scrolls=self._max_scrolls,
            )
        finally:
            _close_runtime(context, browser, runtime)

    def list_folders(self, *, cursor: int, count: int) -> Mapping[str, Any]:
        del cursor, count
        raise DouyinAdapterError(
            "Douyin custom folders are not verified on the official page transport"
        )

    def list_folder_items(
        self,
        folder_id: str,
        *,
        cursor: int,
        count: int,
    ) -> Mapping[str, Any]:
        del folder_id, cursor, count
        raise DouyinAdapterError(
            "Douyin custom folders are not verified on the official page transport"
        )


def collect_official_favorites(
    page: Any,
    *,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    max_scrolls: int = _DEFAULT_MAX_SCROLLS,
    first_page_only: bool = False,
) -> Mapping[str, Any]:
    """Navigate an official page and collect its signed favorites responses."""
    if timeout_ms < 1:
        raise ValueError("Douyin official page timeout must be positive")
    if max_scrolls < 1:
        raise ValueError("Douyin official page max_scrolls must be positive")

    responses: list[Mapping[str, Any]] = []
    failures: list[Exception] = []

    def observe(response: Any) -> None:
        try:
            if urlsplit(str(response.url)).path != _FAVORITES_PATH:
                return
            request = getattr(response, "request", None)
            if str(getattr(request, "method", "")).upper() != "POST":
                return
            status = int(getattr(response, "status", 0))
            if status in {401, 403}:
                failures.append(
                    DouyinAuthenticationError(
                        f"Douyin official page returned HTTP {status}",
                        reason=(
                            "request_rejected"
                            if status == 403
                            else "credentials_rejected"
                        ),
                        status_code=status,
                    )
                )
                return
            if not 200 <= status < 300:
                failures.append(
                    DouyinOfficialPageError(
                        f"Douyin official page returned HTTP {status or 'unknown'}",
                        reason="http_error",
                        status_code=status or None,
                    )
                )
                return
            payload = response.json()
            if not isinstance(payload, Mapping):
                failures.append(
                    DouyinOfficialPageError(
                        "Douyin official page returned a non-object response",
                        reason="invalid_response",
                    )
                )
                return
            responses.append(dict(payload))
            safe_code = payload.get("status_code")
            _LOGGER.info(
                "douyin official favorites path=%s http_status=%s status_code=%s",
                _FAVORITES_PATH,
                status,
                safe_code if isinstance(safe_code, (int, str)) else "unknown",
            )
        except Exception as error:
            failures.append(
                error
                if isinstance(error, DouyinAdapterError)
                else DouyinOfficialPageError(
                    "Douyin official page favorites response was unreadable",
                    reason="invalid_response",
                )
            )

    page.on("response", observe)
    page.goto(
        _FAVORITES_URL,
        wait_until="domcontentloaded",
        timeout=timeout_ms,
    )

    deadline = time.monotonic() + timeout_ms / 1000
    processed = 0
    collected: list[Mapping[str, Any]] = []
    seen_ids: set[str] = set()
    scrolls = 0
    awaiting_more = False
    next_scroll_at = 0.0
    while time.monotonic() < deadline:
        if failures:
            raise failures[0]
        if processed < len(responses):
            payload = responses[processed]
            processed += 1
            _validate_payload_status(payload)
            items = payload.get("aweme_list")
            if not isinstance(items, list):
                raise DouyinOfficialPageError(
                    "Douyin official page response has no aweme_list",
                    reason="invalid_response",
                )
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                item_id = _aweme_id(item)
                if item_id is None or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                collected.append(item)
            if first_page_only:
                return payload
            if not bool(payload.get("has_more")):
                return {
                    "status_code": 0,
                    "cursor": 0,
                    "has_more": False,
                    "aweme_list": collected,
                }
            awaiting_more = True
            next_scroll_at = 0.0
            continue
        now = time.monotonic()
        if awaiting_more and now >= next_scroll_at:
            if scrolls >= max_scrolls:
                raise DouyinOfficialPageError(
                    "Douyin official page did not finish loading favorites",
                    reason="pagination_stalled",
                )
            scrolls += 1
            page.mouse.wheel(0, _SCROLL_DISTANCE)
            next_scroll_at = now + _SCROLL_RETRY_MS / 1000
            continue
        page.wait_for_timeout(_POLL_INTERVAL_MS)

    if failures:
        raise failures[0]
    if awaiting_more:
        raise DouyinOfficialPageError(
            "Douyin official page did not finish loading favorites",
            reason="pagination_stalled",
        )
    raise DouyinOfficialPageError(
        "Douyin official page did not return favorites",
        reason="no_response",
    )


def _validate_payload_status(payload: Mapping[str, Any]) -> None:
    status = payload.get("status_code")
    if status == 0 and not isinstance(status, bool):
        return
    if str(status) in _AUTH_STATUS_CODES:
        raise DouyinAuthenticationError(
            "Douyin official page authentication failed",
            reason="business_rejected",
            status_code=str(status),
        )
    raise DouyinOfficialPageError(
        f"Douyin official page returned status_code={status}",
        reason="business_error",
        status_code=status if isinstance(status, (int, str)) else None,
    )


def _aweme_id(item: Mapping[str, Any]) -> str | None:
    value = item.get("aweme_id") or item.get("aweme_id_str")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    selected = value.strip()
    return selected if selected.isdigit() else None


def _cookie_records(cookie: SecretStr) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for part in cookie.get_secret_value().split(";"):
        name, separator, value = part.strip().partition("=")
        if not separator or not name:
            continue
        records.append(
            {
                "name": name,
                "value": value,
                "url": "https://www.douyin.com/",
            }
        )
    if not records:
        raise ValueError("Douyin cookie has no usable records")
    return records


def _default_playwright_factory() -> Any:
    return sync_playwright().start()


def _close_runtime(context: Any, browser: Any, runtime: Any) -> None:
    for resource, method in (
        (context, "close"),
        (browser, "close"),
        (runtime, "stop"),
    ):
        if resource is None:
            continue
        try:
            getattr(resource, method)()
        except Exception:
            continue
