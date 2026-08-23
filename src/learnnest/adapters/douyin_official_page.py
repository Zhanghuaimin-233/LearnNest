"""Douyin favorites transport bootstrapped by the current official web page.

One isolated page asks Douyin's own runtime for the first favorites request.
LearnNest retains that request's current query/header template in memory and
uses direct HTTP for cursor pagination. No URL, query, body, signature, or
credential value is persisted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, parse_qsl, urlsplit
from urllib.request import urlopen

from playwright.sync_api import sync_playwright
from pydantic import SecretStr

from learnnest.adapters.douyin import DouyinAdapterError
from learnnest.adapters.douyin_http import (
    DouyinAuthenticationError,
    DouyinHttpTransport,
    DouyinSignedRequest,
)

_FAVORITES_URL = (
    "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection"
)
_FAVORITES_PATH = "/aweme/v1/web/aweme/listcollection/"
_AUTH_STATUS_CODES = frozenset({"401", "403", "-1", "1001", "1002"})
_DEFAULT_TIMEOUT_MS = 20_000
_DEFAULT_SYNC_TIMEOUT_MS = 120_000
_DEFAULT_MAX_SCROLLS = 60
_SCROLL_DISTANCE = 6_000
_POLL_INTERVAL_MS = 100
_SCROLL_RETRY_MS = 500
_LOGGER = logging.getLogger(__name__)
_CAPTURED_HEADER_NAMES = frozenset(
    {
        "accept",
        "accept-language",
        "content-type",
        "origin",
        "priority",
        "referer",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "uifid",
        "user-agent",
    }
)
_SIGNATURE_PARAM_NAMES = frozenset({"a_bogus", "msToken", "x-secsdk-web-signature"})
_RUNTIME_FAVORITES_SCRIPT = r"""
async ({cursor, count, timeoutMs}) => {
  const deadline = Date.now() + timeoutMs;
  let lastError = "official runtime is not ready";
  while (Date.now() < deadline) {
    try {
      if (typeof window.axiosInstance !== "function") {
        lastError = "official request client unavailable";
        await new Promise((resolve) => setTimeout(resolve, 250));
        continue;
      }
      const chunkKey = Object.keys(window).find(
        (key) => key.startsWith("webpackChunk") && Array.isArray(window[key])
      );
      if (!chunkKey) throw new Error("webpack runtime unavailable");
      let webpackRequire;
      window[chunkKey].push([
        [`learnnest_${Date.now()}_${Math.random()}`],
        {},
        (runtimeRequire) => { webpackRequire = runtimeRequire; },
      ]);
      if (!webpackRequire || !webpackRequire.m) {
        throw new Error("webpack modules unavailable");
      }
      const factories = Object.entries(webpackRequire.m);
      const requestEntry = factories.find(([, factory]) => {
        const source = String(factory);
        return source.includes("ies.janus.proxy") && source.includes("v_");
      });
      const paramsEntry = factories.find(([, factory]) => {
        const source = String(factory);
        return source.includes("COMMON_SEARCH_PARAMS") &&
          source.includes("DISABLE_SECRET_VIDEO_PARAMS") &&
          source.includes("CHANNEL_PC_WEB");
      });
      if (!requestEntry || !paramsEntry) {
        throw new Error("official favorites modules unavailable");
      }
      const requestModule = webpackRequire(requestEntry[0]);
      const paramsModule = webpackRequire(paramsEntry[0]);
      const requestFunction = requestModule && requestModule.v_;
      if (typeof requestFunction !== "function") {
        throw new Error("official request function unavailable");
      }
      const exportedObjects = Object.values(paramsModule || {}).filter(
        (value) => value && typeof value === "object" && !Array.isArray(value)
      );
      const commonParams = exportedObjects.find(
        (value) => value.device_platform && value.aid && value.channel
      );
      const strategyParams = exportedObjects.find(
        (value) => value.publish_video_strategy_type !== undefined
      );
      if (!commonParams || !strategyParams) {
        throw new Error("official request parameters unavailable");
      }
      const result = await requestFunction(
        "/aweme/v1/web/aweme/listcollection/",
        {...commonParams, ...strategyParams},
        {cursor, count},
        undefined,
        null,
        {current: null}
      );
      return result && typeof result === "object" && result.data &&
        typeof result.data === "object" ? result.data : result;
    } catch (error) {
      lastError = error && error.message ? error.message : "official runtime failed";
      const status = error && error.response && error.response.status;
      if (Number.isInteger(status)) {
        return {__learnnest_http_error__: status};
      }
      if ((error && error.code === "ERR_NETWORK") ||
          String(lastError).toLowerCase().includes("network")) {
        return {__learnnest_network_error__: true};
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  return {__learnnest_runtime_error__: lastError};
}
"""


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
            "runtime_unavailable",
            "network_error",
        ],
        status_code: int | str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.status_code = status_code


class DouyinOfficialPageTransport:
    """Bootstrap one current signed request, then paginate through HTTP."""

    def __init__(
        self,
        cookie: SecretStr,
        *,
        storage_state: Mapping[str, Any] | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        timeout_ms: int = _DEFAULT_SYNC_TIMEOUT_MS,
        http_opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not cookie.get_secret_value().strip():
            raise ValueError("Douyin cookie must not be empty")
        if timeout_ms < 1:
            raise ValueError("Douyin official page timeout must be positive")
        if storage_state is not None and (
            not isinstance(storage_state.get("cookies"), list)
            or not isinstance(storage_state.get("origins"), list)
        ):
            raise ValueError("Douyin browser storage state is incomplete")
        self.cookie = cookie
        self._storage_state = dict(storage_state) if storage_state is not None else None
        self._playwright_factory = playwright_factory or _default_playwright_factory
        self._timeout_ms = timeout_ms
        self._http_opener = http_opener
        self._http_transport: DouyinHttpTransport | None = None

    def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]:
        if cursor < 0:
            raise ValueError("Douyin cursor must not be negative")
        if count < 1:
            raise ValueError("Douyin page count must be positive")
        if self._http_transport is not None:
            return self._http_transport.list_video_favorites(
                cursor=cursor,
                count=count,
            )
        if cursor != 0:
            raise DouyinAdapterError(
                "Douyin official page transport requires the initial cursor first"
            )
        first_payload, signer = self._bootstrap(cursor=cursor, count=count)
        self._http_transport = DouyinHttpTransport(
            self.cookie,
            signer=signer,
            opener=self._http_opener,
        )
        return first_payload

    def _bootstrap(
        self,
        *,
        cursor: int,
        count: int,
    ) -> tuple[Mapping[str, Any], _CapturedRequestSigner]:
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
            return bootstrap_official_favorites_request(
                page,
                cursor=cursor,
                count=count,
                timeout_ms=self._timeout_ms,
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


@dataclass(frozen=True)
class _CapturedRequestSigner:
    params: Mapping[str, str]
    headers: Mapping[str, str]

    def sign(
        self,
        *,
        method: str,
        path: str,
        params: Mapping[str, str],
        body: Mapping[str, str] | None,
        cookie: SecretStr,
    ) -> DouyinSignedRequest:
        del params, body, cookie
        if method != "POST" or path != _FAVORITES_PATH:
            raise DouyinAdapterError("Captured Douyin signer is favorites-only")
        return DouyinSignedRequest(params=dict(self.params), headers=dict(self.headers))


def bootstrap_official_favorites_request(
    page: Any,
    *,
    cursor: int,
    count: int,
    timeout_ms: int = _DEFAULT_SYNC_TIMEOUT_MS,
) -> tuple[Mapping[str, Any], _CapturedRequestSigner]:
    """Generate one fresh official request and retain only an in-memory template."""
    if cursor < 0:
        raise ValueError("Douyin cursor must not be negative")
    if count < 1:
        raise ValueError("Douyin page count must be positive")
    if timeout_ms < 1:
        raise ValueError("Douyin official page timeout must be positive")

    captured: list[tuple[str, Mapping[str, str]]] = []

    def observe(request: Any) -> None:
        try:
            request_url = str(request.url)
            if (
                str(getattr(request, "method", "")).upper() != "POST"
                or urlsplit(request_url).path != _FAVORITES_PATH
            ):
                return
            form = parse_qs(str(getattr(request, "post_data", "")))
            if form.get("cursor") != [str(cursor)] or form.get("count") != [str(count)]:
                return
            raw_headers = request.all_headers()
            headers = {
                str(name).lower(): str(value)
                for name, value in raw_headers.items()
                if str(name).lower() in _CAPTURED_HEADER_NAMES
            }
            captured.append((request_url, headers))
        except Exception:
            return

    page.goto(
        _FAVORITES_URL,
        wait_until="domcontentloaded",
        timeout=timeout_ms,
    )
    page.on("request", observe)
    result = page.evaluate(
        _RUNTIME_FAVORITES_SCRIPT,
        {"cursor": cursor, "count": count, "timeoutMs": timeout_ms},
    )
    if isinstance(result, Mapping) and result.get("__learnnest_runtime_error__"):
        raise DouyinOfficialPageError(
            "Douyin official request runtime is unavailable",
            reason="runtime_unavailable",
        )
    if isinstance(result, Mapping) and result.get("__learnnest_network_error__"):
        raise DouyinOfficialPageError(
            "Douyin official request failed over the network",
            reason="network_error",
        )
    if isinstance(result, Mapping) and isinstance(
        result.get("__learnnest_http_error__"), int
    ):
        status = int(result["__learnnest_http_error__"])
        if status in {401, 403}:
            raise DouyinAuthenticationError(
                f"Douyin official request returned HTTP {status}",
                reason=(
                    "request_rejected" if status == 403 else "credentials_rejected"
                ),
                status_code=status,
            )
        raise DouyinOfficialPageError(
            f"Douyin official request returned HTTP {status}",
            reason="http_error",
            status_code=status,
        )
    if not isinstance(result, Mapping):
        raise DouyinOfficialPageError(
            "Douyin official request returned a non-object response",
            reason="invalid_response",
        )
    payload = dict(result)
    _validate_payload_status(payload)
    if not isinstance(payload.get("aweme_list"), list):
        raise DouyinOfficialPageError(
            "Douyin official request response has no aweme_list",
            reason="invalid_response",
        )
    if not captured:
        raise DouyinOfficialPageError(
            "Douyin official request template was not captured",
            reason="no_response",
        )
    request_url, headers = captured[-1]
    query = dict(parse_qsl(urlsplit(request_url).query, keep_blank_values=True))
    if not _SIGNATURE_PARAM_NAMES.intersection(query):
        raise DouyinOfficialPageError(
            "Douyin official request has no current signature",
            reason="runtime_unavailable",
        )
    _LOGGER.info(
        "douyin official favorites bootstrap path=%s status_code=%s",
        _FAVORITES_PATH,
        payload.get("status_code", "unknown"),
    )
    return payload, _CapturedRequestSigner(params=query, headers=headers)


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
