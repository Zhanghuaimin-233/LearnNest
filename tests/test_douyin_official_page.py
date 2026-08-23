from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from learnnest.adapters.douyin import DouyinAdapterError
from learnnest.adapters.douyin_http import DouyinAuthenticationError
from learnnest.adapters.douyin_official_page import (
    DouyinOfficialPageError,
    DouyinOfficialPageTransport,
    collect_official_favorites,
)


def _payload(item_id: str, *, cursor: int, has_more: bool) -> dict[str, Any]:
    return {
        "status_code": 0,
        "cursor": cursor,
        "has_more": has_more,
        "aweme_list": [{"aweme_id": item_id, "desc": f"作品 {item_id}"}],
    }


class FakeRequest:
    method = "POST"


class FakeResponse:
    request = FakeRequest()

    def __init__(self, payload: Mapping[str, Any], *, status: int = 200) -> None:
        self.url = (
            "https://www.douyin.com/aweme/v1/web/aweme/listcollection/"
            "?a_bogus=runtime-only"
        )
        self.status = status
        self._payload = payload

    def json(self) -> Mapping[str, Any]:
        return self._payload


class FakeMouse:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    def wheel(self, _x: int, _y: int) -> None:
        self.page.emit_next()


class FakePage:
    def __init__(self, batches: list[list[FakeResponse]]) -> None:
        self.batches = batches
        self.handlers: list[Any] = []
        self.batch_index = 0
        self.mouse = FakeMouse(self)
        self.goto_calls: list[tuple[str, str, int]] = []

    def on(self, event: str, handler: Any) -> None:
        assert event == "response"
        self.handlers.append(handler)

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        self.emit_next()

    def emit_next(self) -> None:
        if self.batch_index >= len(self.batches):
            return
        batch = self.batches[self.batch_index]
        self.batch_index += 1
        for response in batch:
            for handler in self.handlers:
                handler(response)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def evaluate(self, _script: str) -> None:
        raise AssertionError("official favorites must not call page.evaluate")


def test_official_page_collects_signed_favorite_pages_without_replaying_request() -> (
    None
):
    page = FakePage(
        [
            [FakeResponse(_payload("101", cursor=10, has_more=True))],
            [FakeResponse(_payload("102", cursor=20, has_more=False))],
        ]
    )

    result = collect_official_favorites(page, timeout_ms=1_000, max_scrolls=3)

    assert [item["aweme_id"] for item in result["aweme_list"]] == ["101", "102"]
    assert result["status_code"] == 0
    assert result["has_more"] is False
    assert page.goto_calls == [
        (
            "https://www.douyin.com/user/self?from_tab_name=main&showTab=favorite_collection",
            "domcontentloaded",
            1_000,
        )
    ]


def test_official_page_first_page_mode_is_a_bounded_login_smoke() -> None:
    page = FakePage(
        [
            [FakeResponse(_payload("101", cursor=10, has_more=True))],
            [FakeResponse(_payload("102", cursor=20, has_more=False))],
        ]
    )

    result = collect_official_favorites(
        page,
        timeout_ms=1_000,
        max_scrolls=3,
        first_page_only=True,
    )

    assert [item["aweme_id"] for item in result["aweme_list"]] == ["101"]
    assert page.batch_index == 1


def test_official_page_retries_scroll_until_the_next_page_responds() -> None:
    page = FakePage(
        [
            [FakeResponse(_payload("101", cursor=10, has_more=True))],
            [FakeResponse(_payload("102", cursor=20, has_more=False))],
        ]
    )

    class DelayedMouse:
        def __init__(self) -> None:
            self.calls = 0

        def wheel(self, _x: int, _y: int) -> None:
            self.calls += 1
            if self.calls >= 2:
                page.emit_next()

    mouse = DelayedMouse()
    page.mouse = mouse

    result = collect_official_favorites(page, timeout_ms=1_000, max_scrolls=3)

    assert [item["aweme_id"] for item in result["aweme_list"]] == ["101", "102"]
    assert mouse.calls == 2


def test_official_page_projects_http_403_without_response_body() -> None:
    class BodyMustNotBeRead(FakeResponse):
        def json(self) -> Mapping[str, Any]:
            raise AssertionError("403 body must not be read")

    page = FakePage([[BodyMustNotBeRead({}, status=403)]])

    with pytest.raises(DouyinAuthenticationError) as error:
        collect_official_favorites(page, timeout_ms=1_000, max_scrolls=1)

    assert error.value.reason == "request_rejected"
    assert error.value.status_code == 403


def test_official_page_distinguishes_stalled_pagination() -> None:
    page = FakePage([[FakeResponse(_payload("101", cursor=10, has_more=True))]])

    with pytest.raises(DouyinOfficialPageError) as error:
        collect_official_favorites(page, timeout_ms=100, max_scrolls=1)

    assert error.value.reason == "pagination_stalled"


@pytest.mark.parametrize(
    ("response", "reason", "status_code"),
    [
        (FakeResponse({}, status=429), "http_error", 429),
        (
            FakeResponse({"status_code": 9, "aweme_list": []}),
            "business_error",
            9,
        ),
        (
            FakeResponse({"status_code": 0, "unexpected": []}),
            "invalid_response",
            None,
        ),
    ],
)
def test_official_page_classifies_safe_non_authentication_failures(
    response: FakeResponse,
    reason: str,
    status_code: int | None,
) -> None:
    page = FakePage([[response]])

    with pytest.raises(DouyinOfficialPageError) as error:
        collect_official_favorites(page, timeout_ms=100, max_scrolls=1)

    assert error.value.reason == reason
    assert error.value.status_code == status_code


class FakeContext:
    def __init__(self, page: FakePage, closed: list[str]) -> None:
        self.page = page
        self.closed = closed
        self.cookies_added: list[dict[str, str]] = []

    def add_cookies(self, cookies: list[dict[str, str]]) -> None:
        self.cookies_added = cookies

    def new_page(self) -> FakePage:
        return self.page

    def close(self) -> None:
        self.closed.append("context")


class FakeBrowser:
    def __init__(self, context: FakeContext, closed: list[str]) -> None:
        self.context = context
        self.closed = closed

        self.context_options: list[dict[str, object]] = []

    def new_context(self, **kwargs: object) -> FakeContext:
        self.context_options.append(kwargs)
        return self.context

    def close(self) -> None:
        self.closed.append("browser")


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.launches: list[dict[str, object]] = []

    def launch(self, **kwargs: object) -> FakeBrowser:
        self.launches.append(kwargs)
        return self.browser


class FakeRuntime:
    def __init__(self, page: FakePage) -> None:
        self.closed: list[str] = []
        self.context = FakeContext(page, self.closed)
        self.browser = FakeBrowser(self.context, self.closed)
        self.chromium = FakeChromium(self.browser)

    def stop(self) -> None:
        self.closed.append("runtime")


class FakeBootstrapRequest:
    method = "POST"
    url = (
        "https://www.douyin.com/aweme/v1/web/aweme/listcollection/"
        "?aid=6383&a_bogus=runtime-only&msToken=runtime-only"
    )
    post_data = "count=20&cursor=0"

    def all_headers(self) -> dict[str, str]:
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": "https://www.douyin.com",
            "referer": "https://www.douyin.com/",
            "user-agent": "official-edge-agent",
            "uifid": "runtime-only",
            "cookie": "must-not-be-reused",
            "content-length": "must-not-be-reused",
        }


class FakeBootstrapPage:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.handlers: dict[str, list[Any]] = {}
        self.goto_calls: list[tuple[str, str, int]] = []
        self.evaluate_calls: list[Mapping[str, int]] = []

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))

    def evaluate(self, _script: str, argument: Mapping[str, int]) -> Mapping[str, Any]:
        self.evaluate_calls.append(argument)
        for handler in self.handlers.get("request", []):
            handler(FakeBootstrapRequest())
        return self.payload


def test_official_transport_uses_isolated_headed_edge_and_closes_everything() -> None:
    page = FakeBootstrapPage(_payload("101", cursor=10, has_more=True))
    runtime = FakeRuntime(page)  # type: ignore[arg-type]

    class DirectResponse:
        status = 200

        def __enter__(self) -> DirectResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(_payload("102", cursor=20, has_more=False)).encode()

    direct_requests: list[Any] = []

    def open_direct(request: Any, *, timeout: float) -> DirectResponse:
        direct_requests.append((request, timeout))
        return DirectResponse()

    transport = DouyinOfficialPageTransport(
        SecretStr("sessionid=secret; passport_csrf_token=csrf"),
        playwright_factory=lambda: runtime,
        http_opener=open_direct,
    )

    first = transport.list_video_favorites(cursor=0, count=20)
    second = transport.list_video_favorites(cursor=10, count=20)

    assert first["aweme_list"][0]["aweme_id"] == "101"
    assert second["aweme_list"][0]["aweme_id"] == "102"
    assert runtime.chromium.launches == [{"channel": "msedge", "headless": False}]
    assert runtime.browser.context_options == [{"locale": "zh-CN"}]
    assert page.goto_calls[0][2] == 120_000
    assert page.evaluate_calls == [{"cursor": 0, "count": 20, "timeoutMs": 120_000}]
    assert runtime.context.cookies_added == [
        {
            "name": "sessionid",
            "value": "secret",
            "url": "https://www.douyin.com/",
        },
        {
            "name": "passport_csrf_token",
            "value": "csrf",
            "url": "https://www.douyin.com/",
        },
    ]
    assert runtime.closed == ["context", "browser", "runtime"]
    request, timeout = direct_requests[0]
    assert request.full_url == (
        "https://www.douyin.com/aweme/v1/web/aweme/listcollection/"
        "?aid=6383&a_bogus=runtime-only&msToken=runtime-only"
    )
    assert request.data == b"count=20&cursor=10"
    assert request.get_header("Cookie") == (
        "sessionid=secret; passport_csrf_token=csrf"
    )
    assert request.get_header("User-agent") == "official-edge-agent"
    assert request.get_header("Uifid") == "runtime-only"
    assert request.get_header("Content-length") is None
    assert timeout == 20.0


def test_official_transport_restores_encrypted_browser_storage_state() -> None:
    page = FakeBootstrapPage(_payload("101", cursor=0, has_more=False))
    runtime = FakeRuntime(page)  # type: ignore[arg-type]
    storage_state = {
        "cookies": [
            {
                "name": "sessionid",
                "value": "secret",
                "domain": ".douyin.com",
                "path": "/",
            }
        ],
        "origins": [
            {
                "origin": "https://www.douyin.com",
                "localStorage": [{"name": "device-state", "value": "sentinel"}],
            }
        ],
    }
    transport = DouyinOfficialPageTransport(
        SecretStr("sessionid=secret"),
        storage_state=storage_state,
        playwright_factory=lambda: runtime,
    )

    result = transport.list_video_favorites(cursor=0, count=20)

    assert result["aweme_list"][0]["aweme_id"] == "101"
    assert runtime.browser.context_options == [
        {"locale": "zh-CN", "storage_state": storage_state}
    ]
    assert runtime.context.cookies_added == []


def test_official_transport_rejects_nonzero_cursor_before_runtime_bootstrap() -> None:
    transport = DouyinOfficialPageTransport(
        SecretStr("sessionid=secret"),
        playwright_factory=lambda: pytest.fail("browser must not start"),
    )

    with pytest.raises(DouyinAdapterError, match="initial cursor"):
        transport.list_video_favorites(cursor=10, count=20)


def test_official_transport_projects_bootstrap_http_rejection() -> None:
    page = FakeBootstrapPage({"__learnnest_http_error__": 403})
    runtime = FakeRuntime(page)  # type: ignore[arg-type]
    transport = DouyinOfficialPageTransport(
        SecretStr("sessionid=secret"),
        playwright_factory=lambda: runtime,
    )

    with pytest.raises(DouyinAuthenticationError) as error:
        transport.list_video_favorites(cursor=0, count=20)

    assert error.value.reason == "request_rejected"
    assert error.value.status_code == 403
