from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from learnnest.adapters.douyin_http import DouyinAuthenticationError
from learnnest.adapters.douyin_official_page import (
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
            "https://www.douyin.com/user/self?showTab=favorite_collection",
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


def test_official_page_projects_http_403_without_response_body() -> None:
    class BodyMustNotBeRead(FakeResponse):
        def json(self) -> Mapping[str, Any]:
            raise AssertionError("403 body must not be read")

    page = FakePage([[BodyMustNotBeRead({}, status=403)]])

    with pytest.raises(DouyinAuthenticationError) as error:
        collect_official_favorites(page, timeout_ms=1_000, max_scrolls=1)

    assert error.value.reason == "request_rejected"
    assert error.value.status_code == 403


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

    def new_context(self) -> FakeContext:
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


def test_official_transport_uses_isolated_headed_edge_and_closes_everything() -> None:
    runtime = FakeRuntime(
        FakePage([[FakeResponse(_payload("101", cursor=0, has_more=False))]])
    )
    transport = DouyinOfficialPageTransport(
        SecretStr("sessionid=secret; passport_csrf_token=csrf"),
        playwright_factory=lambda: runtime,
    )

    result = transport.list_video_favorites(cursor=0, count=20)

    assert result["aweme_list"][0]["aweme_id"] == "101"
    assert runtime.chromium.launches == [{"channel": "msedge", "headless": False}]
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
