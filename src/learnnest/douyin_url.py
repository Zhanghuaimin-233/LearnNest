"""Shared Douyin URL admission: one boundary before any job or pipeline fact.

Three accepted shapes collapse to one canonical ``https://www.douyin.com/video/<id>``:

- official ``douyin.com``/``www.douyin.com`` ``/video/<纯数字>`` pages (trailing slash,
  fragment and unrelated query allowed);
- official pages carrying a single, numeric, non-conflicting ``modal_id``;
- ``https://v.douyin.com/<code>`` short links, resolved only at submit time with
  zero cookies, zero retries, HTTPS + exact-host allowlist on every hop, at most
  five hops and one total 5s deadline.

Account paths, signature/query markers and cookies never leave this boundary on
success; every rejection is a safe, specific, user-actionable error.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ShortUrlResolver = Callable[[str], str]
HopFetch = Callable[[str, float], tuple[int, str | None]]


class DouyinUrlError(ValueError):
    """A safe, specific, user-actionable Douyin URL admission failure."""


_VIDEO_PATH = re.compile(r"^/video/([0-9]+)/?$")
_CANONICAL_VIDEO_PATH = re.compile(r"^/video/([0-9]+)$")


def _is_official_host(host: str) -> bool:
    """True for ``douyin.com`` and its official subdomains only."""
    return host == "douyin.com" or host.endswith(".douyin.com")


def canonicalize_douyin_url(
    raw: str,
    *,
    resolver: ShortUrlResolver | None = None,
) -> str | None:
    """Return one canonical video URL, ``None`` for non-Douyin URLs.

    Douyin URLs that cannot be reduced raise :class:`DouyinUrlError` with an
    actionable message. The resolver is only consulted for ``v.douyin.com``
    short links and is injectable for offline tests.
    """
    candidate = str(raw).strip()
    if not candidate:
        raise DouyinUrlError("抖音链接为空；请复制网页完整链接后重试。")
    try:
        parsed = urlsplit(candidate)
    except ValueError as error:
        raise DouyinUrlError(
            "抖音链接无法解析；请复制网页地址栏中的完整链接后重试。"
        ) from error
    host = (parsed.hostname or "").rstrip(".").lower()
    if _is_official_host(host):
        if parsed.username is not None or parsed.password is not None:
            raise DouyinUrlError(
                "抖音链接不能包含账户信息；请只复制地址栏中干净的网页链接。"
            )
        if parsed.scheme.lower() != "https":
            raise DouyinUrlError("抖音链接必须使用 https；请复制地址栏中的官方链接。")
    elif "douyin" in host:
        raise DouyinUrlError("无法确认是抖音官方域名；请只使用 douyin.com 官方链接。")
    else:
        return None
    if host == "v.douyin.com":
        return _resolve_short_link(candidate, resolver)
    return _extract_official_id(parsed)


def is_douyin_canonical_url(value: str) -> bool:
    """True only for the exact canonical video URL used by download and facts."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() != "https" or host != "www.douyin.com":
        return False
    return bool(_CANONICAL_VIDEO_PATH.match(parsed.path))


class HttpShortUrlResolver:
    """Zero-cookie, zero-retry redirect walker with per-hop checks and a deadline.

    ``fetch`` is a transport seam (``(url, timeout) -> (status, location)``) that
    is injected for offline tests; the real transport never reads response bodies.
    """

    def __init__(
        self,
        *,
        fetch: HopFetch | None = None,
        max_hops: int = 5,
        total_timeout: float = 5.0,
    ) -> None:
        self._fetch = fetch or _http_fetch
        self._max_hops = max_hops
        self._total_timeout = total_timeout

    def resolve(self, short_url: str) -> str:
        parsed = self._check_hop_url(short_url, kind="短链接")
        if parsed.hostname != "v.douyin.com":
            raise DouyinUrlError("抖音短链接只能使用 v.douyin.com 官方域名。")
        current = short_url
        started = time.monotonic()
        for _ in range(self._max_hops):
            location = self._next_hop(current, started)
            if location is None:
                return current
            current = location
        raise DouyinUrlError(
            "抖音短链接跳转次数超过上限；请打开视频页复制完整地址后重试。"
        )

    def _next_hop(self, url: str, started: float) -> str | None:
        self._check_hop_url(url, kind="跳转目标")
        elapsed = time.monotonic() - started
        remaining = self._total_timeout - elapsed
        if remaining <= 0:
            raise DouyinUrlError(
                "抖音短链接解析超时；请稍后重试，或打开视频页复制完整地址。"
            )
        try:
            status, location = self._fetch(url, remaining)
        except TimeoutError as error:
            raise DouyinUrlError(
                "抖音短链接解析超时；请稍后重试，或打开视频页复制完整地址。"
            ) from error
        except OSError as error:
            raise DouyinUrlError(
                "抖音短链接解析失败；请打开视频页复制 douyin.com/video/<数字> 形式的完整地址后重试。"
            ) from error
        if 200 <= status < 300:
            return None
        if 300 <= status < 400:
            if not location or not location.strip():
                raise DouyinUrlError(
                    "抖音短链接跳转缺少目标地址；请打开视频页复制完整地址后重试。"
                )
            return location.strip()
        raise DouyinUrlError(
            "抖音短链接解析失败；请打开视频页复制 douyin.com/video/<数字> 形式的完整地址后重试。"
        )

    @staticmethod
    def _check_hop_url(url: str, *, kind: str) -> object:
        try:
            parsed = urlsplit(url)
        except ValueError as error:
            raise DouyinUrlError(
                f"抖音短链接{kind}无法解析；请打开视频页复制完整地址后重试。"
            ) from error
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme.lower() != "https":
            raise DouyinUrlError(f"抖音短链接{kind}必须使用 https。")
        if not _is_official_host(host):
            raise DouyinUrlError(
                f"抖音短链接{kind}不在允许的抖音官方域名内；请只使用 douyin.com 官方链接。"
            )
        return parsed


def _resolve_short_link(short_url: str, resolver: ShortUrlResolver | None) -> str:
    parsed = urlsplit(short_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        raise DouyinUrlError("抖音短链接缺少视频编码；请复制包含编码的完整短链接。")
    effective = resolver or _default_resolver()
    try:
        target = effective(short_url)
    except DouyinUrlError:
        raise
    except Exception as error:
        raise DouyinUrlError(
            "抖音短链接解析失败；请打开视频页复制 douyin.com/video/<数字> 形式的完整地址后重试。"
        ) from error
    if not isinstance(target, str) or not target.strip():
        raise DouyinUrlError(
            "抖音短链接未返回有效地址；请打开视频页复制完整地址后重试。"
        )
    return canonicalize_douyin_url(target, resolver=resolver)


def _extract_official_id(parsed: object) -> str:
    query = parsed.query
    modal_id = _single_modal_id(query)
    match = _VIDEO_PATH.match(parsed.path)
    path_id = match.group(1) if match else None
    if modal_id is not None:
        if path_id is not None and path_id != modal_id:
            raise DouyinUrlError("抖音链接包含多个不同的视频 ID；无法确认目标视频。")
        return f"https://www.douyin.com/video/{modal_id}"
    if path_id is not None:
        return f"https://www.douyin.com/video/{path_id}"
    if parsed.path.startswith("/video/"):
        raise DouyinUrlError(
            "抖音视频 ID 必须是纯数字；请确认地址形如 douyin.com/video/<数字>。"
        )
    raise DouyinUrlError(
        "抖音链接中未找到视频 ID；请打开视频页复制 douyin.com/video/<数字> 形式的地址后重试。"
    )


def _single_modal_id(query: str) -> str | None:
    values: list[str] = []
    for pair in query.split("&"):
        if not pair:
            continue
        key, separator, value = pair.partition("=")
        if separator and key.casefold() == "modal_id":
            values.append(value)
    if not values:
        return None
    if len(values) > 1:
        raise DouyinUrlError("抖音链接包含多个 modal_id；请使用单一视频分享链接。")
    if not values[0].isdigit():
        raise DouyinUrlError("抖音链接中的 modal_id 必须是纯数字。")
    return values[0]


def _default_resolver() -> ShortUrlResolver:
    return _DEFAULT_RESOLVER


def _http_fetch(url: str, timeout: float) -> tuple[int, str | None]:
    """One HTTPS GET that never reads or persists the response body."""
    request = Request(url, headers={"User-Agent": "LearnNest/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.status, response.headers.get("Location")


_DEFAULT_RESOLVER = HttpShortUrlResolver()
