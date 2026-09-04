"""Douyin URL admission contract: accept/reject matrix, short-link hops, dedup.

The classifier/canonicalizer under test is never mocked; only the short-link
transport seam (``fetch``) is injected so the resolver runs fully offline.
"""

from __future__ import annotations

import pytest

from learnnest.douyin_url import (
    DouyinUrlError,
    HttpShortUrlResolver,
    canonicalize_douyin_url,
    is_douyin_canonical_url,
)

CANONICAL_123 = "https://www.douyin.com/video/123"


class _FakeFetch:
    def __init__(self, routes: dict[str, tuple[int, str | None]]) -> None:
        self.routes = dict(routes)
        self.requested: list[str] = []
        self.error: Exception | None = None

    def __call__(self, url: str, timeout: float) -> tuple[int, str | None]:
        del timeout
        self.requested.append(url)
        if self.error is not None:
            raise self.error
        return self.routes[url]


def _fake_resolver(target: str):
    return lambda _short: target


# ---------------------------------------------------------------- accept matrix


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.douyin.com/video/123",
        "https://www.douyin.com/video/123/",
        "https://www.douyin.com/video/123?previous_page=app_code_link",
        "https://www.douyin.com/video/123#fragment",
        "https://www.douyin.com/video/123/?x=1#f",
        "https://douyin.com/video/123",
        "https://www.douyin.com/video/123?modal_id=123",
        "https://www.douyin.com/?modal_id=123",
        "https://www.douyin.com/user/some.handle?modal_id=123",
    ],
)
def test_official_video_links_reduce_to_one_canonical_url(raw: str) -> None:
    assert canonicalize_douyin_url(raw) == CANONICAL_123


@pytest.mark.parametrize(
    "raw",
    [
        "https://v.douyin.com/AbC123",
        "https://v.douyin.com/AbC123/",
    ],
)
def test_short_links_resolve_to_canonical_with_injected_resolver(raw: str) -> None:
    resolver = _fake_resolver("https://www.douyin.com/video/999")
    assert canonicalize_douyin_url(raw, resolver=resolver) == (
        "https://www.douyin.com/video/999"
    )


def test_non_douyin_urls_pass_through_untouched() -> None:
    value = "https://example.com/watch?v=1#chapter"
    assert canonicalize_douyin_url(value) is None


# ---------------------------------------------------------------- reject matrix


@pytest.mark.parametrize(
    ("raw", "keyword"),
    [
        ("https://www.douyin.com/", "视频 ID"),
        ("https://www.douyin.com/user/MS4wLjABAAAA", "视频 ID"),
        ("https://live.douyin.com/123", "视频 ID"),
        ("https://www.douyin.com/collection/123", "视频 ID"),
        ("https://www.douyin.com/search/abc", "视频 ID"),
        ("https://www.douyin.com/video/", "视频 ID"),
        ("https://www.douyin.com/video/abc", "数字"),
        ("https://www.douyin.com/video/123abc", "数字"),
        ("https://www.douyin.com/video/123 复制打开抖音看精彩内容", "数字"),
        ("https://www.douyin.com/video/123?modal_id=456", "多个"),
        ("https://user:pass@www.douyin.com/video/123", "账户"),
        ("http://www.douyin.com/video/123", "https"),
        ("http://v.douyin.com/AbC123", "https"),
        ("https://douyin.com.evil.com/video/123", "官方域名"),
        ("https://evil-douyin.com/video/123", "官方域名"),
        ("https://video.douyin.com.cn/video/123", "官方域名"),
        ("https://www.douyin.com.video.example.com/video/123", "官方域名"),
        ("https://v.douyin.com/", "编码"),
        ("https://v.douyin.com", "编码"),
    ],
)
def test_disallowed_douyin_links_fail_closed_with_actionable_reason(
    raw: str, keyword: str
) -> None:
    with pytest.raises(DouyinUrlError, match=keyword):
        canonicalize_douyin_url(raw)


# ------------------------------------------------------- short-link hop contract


def test_short_link_follows_every_hop_with_request_decoration() -> None:
    fetch = _FakeFetch(
        {
            "https://v.douyin.com/abc": (302, "https://www.douyin.com/video/888"),
            "https://www.douyin.com/video/888": (200, None),
        }
    )
    resolver = HttpShortUrlResolver(fetch=fetch)

    assert resolver.resolve("https://v.douyin.com/abc") == (
        "https://www.douyin.com/video/888"
    )
    assert fetch.requested == [
        "https://v.douyin.com/abc",
        "https://www.douyin.com/video/888",
    ]


def test_short_link_hops_must_stay_https_and_inside_allowlist() -> None:
    for target in (
        "http://www.douyin.com/video/777",
        "https://evil.com/video/777",
        "https://douyin.com.evil.com/video/777",
        "https://video.douyin.com.cn/video/777",
    ):
        fetch = _FakeFetch(
            {
                "https://v.douyin.com/abc": (302, target),
            }
        )
        resolver = HttpShortUrlResolver(fetch=fetch)
        with pytest.raises(DouyinUrlError):
            resolver.resolve("https://v.douyin.com/abc")
        assert fetch.requested == ["https://v.douyin.com/abc"]


def test_short_link_final_target_must_yield_one_official_video_id() -> None:
    with pytest.raises(DouyinUrlError, match="视频 ID"):
        canonicalize_douyin_url(
            "https://v.douyin.com/abc",
            resolver=_fake_resolver("https://www.douyin.com/user/MS4wLjABAAAA"),
        )


def test_short_link_loops_and_hop_exhaustion_stop_after_five_requests() -> None:
    fetch = _FakeFetch(
        {
            "https://v.douyin.com/abc": (302, "https://v.douyin.com/def"),
            "https://v.douyin.com/def": (302, "https://v.douyin.com/abc"),
        }
    )
    resolver = HttpShortUrlResolver(fetch=fetch)

    with pytest.raises(DouyinUrlError, match="超过"):
        resolver.resolve("https://v.douyin.com/abc")
    assert len(fetch.requested) == 5


def test_short_link_timeout_maps_to_actionable_failure() -> None:
    fetch = _FakeFetch({"https://v.douyin.com/abc": (302, None)})
    fetch.error = TimeoutError("timed out")
    resolver = HttpShortUrlResolver(fetch=fetch)

    with pytest.raises(DouyinUrlError, match="超时"):
        resolver.resolve("https://v.douyin.com/abc")


def test_short_link_failure_status_fails_closed_without_body() -> None:
    fetch = _FakeFetch({"https://v.douyin.com/abc": (404, None)})
    resolver = HttpShortUrlResolver(fetch=fetch)

    with pytest.raises(DouyinUrlError, match="解析"):
        resolver.resolve("https://v.douyin.com/abc")


def test_short_link_resolution_never_uses_cookies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fetch = _FakeFetch(
        {
            "https://v.douyin.com/abc": (302, "https://www.douyin.com/video/888"),
            "https://www.douyin.com/video/888": (200, None),
        }
    )
    resolver = HttpShortUrlResolver(fetch=fetch)
    resolver.resolve("https://v.douyin.com/abc")

    assert "cookie" not in str(caplog.text).casefold()


# ------------------------------------------------- canonical marker for download


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.douyin.com/video/123", True),
        ("https://www.douyin.com/video/123/", False),
        ("https://v.douyin.com/AbC123", False),
        ("https://www.douyin.com/user/123", False),
        ("http://www.douyin.com/video/123", False),
        ("https://example.com/video/123", False),
    ],
)
def test_canonical_douyin_marker(value: str, expected: bool) -> None:
    assert is_douyin_canonical_url(value) is expected
