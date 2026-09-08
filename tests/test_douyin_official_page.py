from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from learnnest.adapters.douyin import DouyinAdapterError
from learnnest.adapters.douyin_http import DouyinAuthenticationError
from learnnest.adapters.douyin_official_page import (
    _RUNTIME_COLLECTIONS_SCRIPT,
    _RUNTIME_FAVORITES_SCRIPT,
    DouyinOfficialPageError,
    DouyinOfficialPageTransport,
    bootstrap_official_favorites_request,
    collect_official_collections,
    collect_official_favorites,
)
from playwright.sync_api import sync_playwright


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


def test_official_page_collects_only_stable_folder_and_item_fields() -> None:
    class FolderPage:
        def evaluate(
            self, _script: str, argument: Mapping[str, int]
        ) -> Mapping[str, Any]:
            assert argument == {"timeoutMs": 120_000, "maxPages": 50}
            return {
                "status_code": 0,
                "folders": [
                    {
                        "folder_id": "10",
                        "name": "编程",
                        "item_count": 1,
                        "items": [
                            {
                                "aweme_id": "101",
                                "title": "作品 101",
                                "cover_url": "https://cdn.example/101.jpg?signature=transient",
                            }
                        ],
                    }
                ],
            }

    payload = collect_official_collections(FolderPage())

    assert payload["folders"][0]["folder_id"] == "10"
    assert payload["folders"][0]["items"][0].keys() == {
        "aweme_id",
        "title",
        "cover_url",
    }


def test_official_page_cold_collection_runtime_uses_action_results_before_react_state() -> (
    None
):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()
        page.set_content(
            '<div role="tab">收藏夹</div><div id="collection-context"></div>'
        )
        page.evaluate(
            """() => {
                const context = {
                    state: {
                        collects: [],
                        collectResRef: {current: {hasMore: false}},
                        videoResRef: {current: {}},
                    },
                    action: {
                        getCollects: async () => ({
                            statusCode: 0,
                            cursor: 1,
                            hasMore: false,
                            data: [{
                                collectionFolderId: "10",
                                collectionFolderName: "编程",
                                videoTotal: 1,
                            }],
                        }),
                        getVideoList: async (folderId) => ({
                            statusCode: 0,
                            cursor: 1,
                            hasMore: false,
                            data: [{
                                awemeId: "101",
                                desc: `作品 ${folderId}`,
                                video: {coverUrlList: ["https://cdn.example/101.jpg"]},
                            }],
                        }),
                    },
                };
                document.querySelector("#collection-context").__reactFiber$test = {
                    memoizedProps: {value: context},
                    return: null,
                };
            }"""
        )

        result = page.evaluate(
            _RUNTIME_COLLECTIONS_SCRIPT,
            {"timeoutMs": 1_000, "maxPages": 5},
        )
        browser.close()

    assert result == {
        "status_code": 0,
        "folders": [
            {
                "folder_id": "10",
                "name": "编程",
                "item_count": 1,
                "items": [
                    {
                        "aweme_id": "101",
                        "title": "作品 10",
                        "cover_url": "https://cdn.example/101.jpg",
                    }
                ],
            }
        ],
    }


def test_runtime_favorites_script_selects_params_module_by_exports() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()
        page.set_content("<main>offline runtime fixture</main>")
        page.evaluate(
            """() => {
                window.axiosInstance = () => {};
                const modules = {
                    decoy: {
                        COMMON_SEARCH_PARAMS: {not: "the params"},
                    },
                    valid: {
                        COMMON_SEARCH_PARAMS: {
                            device_platform: "webapp",
                            aid: "6383",
                            channel: "channel_pc_web",
                        },
                        DISABLE_SECRET_VIDEO_PARAMS: {
                            publish_video_strategy_type: "2",
                        },
                    },
                    request: {
                        v_: async () => ({status_code: 0, aweme_list: []}),
                    },
                };
                const webpackRequire = (id) => modules[id];
                webpackRequire.m = {
                    decoy: function decoyFactory() {
                        // COMMON_SEARCH_PARAMS DISABLE_SECRET_VIDEO_PARAMS CHANNEL_PC_WEB
                    },
                    valid: function validFactory() {
                        // COMMON_SEARCH_PARAMS DISABLE_SECRET_VIDEO_PARAMS CHANNEL_PC_WEB
                    },
                    request: function requestFactory() {
                        // ies.janus.proxy v_
                    },
                };
                const chunk = [];
                chunk.push = (entry) => entry[2](webpackRequire);
                window.webpackChunkLearnNestFixture = chunk;
            }"""
        )
        result = page.evaluate(
            _RUNTIME_FAVORITES_SCRIPT,
            {"cursor": 0, "count": 20, "timeoutMs": 1_000},
        )
        browser.close()

    assert result == {"status_code": 0, "aweme_list": []}


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


class FakeBootstrapCandidateRequest(FakeBootstrapRequest):
    def __init__(self, *, signature: str | None) -> None:
        suffix = "" if signature is None else f"&a_bogus={signature}"
        self.url = (
            "https://www.douyin.com/aweme/v1/web/aweme/listcollection/"
            f"?aid=6383{suffix}"
        )


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


class FakeBootstrapCandidatePage(FakeBootstrapPage):
    def __init__(
        self,
        payload: Mapping[str, Any],
        requests: list[FakeBootstrapCandidateRequest],
    ) -> None:
        super().__init__(payload)
        self.requests = requests

    def evaluate(self, script: str, argument: Mapping[str, int]) -> Mapping[str, Any]:
        self.evaluate_calls.append(argument)
        for request in self.requests:
            for handler in self.handlers.get("request", []):
                handler(request)
        return self.payload


class FakeCollectionBootstrapPage(FakeBootstrapPage):
    def evaluate(self, script: str, argument: Mapping[str, int]) -> Mapping[str, Any]:
        if "cursor" in argument:
            return super().evaluate(script, argument)
        self.evaluate_calls.append(argument)
        return {
            "status_code": 0,
            "folders": [
                {
                    "folder_id": "10",
                    "name": "编程",
                    "item_count": 1,
                    "items": [
                        {
                            "aweme_id": "101",
                            "title": "作品 101",
                            "cover_url": None,
                        }
                    ],
                }
            ],
        }


def test_official_transport_uses_short_lived_headless_edge_and_closes_everything() -> (
    None
):
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
    assert runtime.chromium.launches == [{"channel": "msedge", "headless": True}]
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


def test_official_transport_collects_custom_folders_in_the_same_isolated_page() -> None:
    page = FakeCollectionBootstrapPage(_payload("101", cursor=0, has_more=False))
    runtime = FakeRuntime(page)  # type: ignore[arg-type]
    transport = DouyinOfficialPageTransport(
        SecretStr("sessionid=secret"),
        playwright_factory=lambda: runtime,
        include_custom_folders=True,
    )

    transport.list_video_favorites(cursor=0, count=20)
    folders = transport.list_folders(cursor=0, count=20)
    items = transport.list_folder_items("10", cursor=0, count=20)

    assert folders == {
        "status_code": 0,
        "collects_list": [
            {
                "collects_id_str": "10",
                "collects_name": "编程",
                "total_number": 1,
            }
        ],
        "cursor": 1,
        "has_more": False,
    }
    assert items["aweme_list"] == [
        {"aweme_id": "101", "desc": "作品 101", "cover_url": None}
    ]
    assert page.evaluate_calls == [
        {"cursor": 0, "count": 20, "timeoutMs": 120_000},
        {"timeoutMs": 120_000, "maxPages": 50},
    ]
    assert runtime.closed == ["context", "browser", "runtime"]


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


@pytest.mark.parametrize(
    ("signatures", "expected_signature"),
    [
        (["signed-first", None], "signed-first"),
        ([None, "signed-last"], "signed-last"),
        (["signed-first", None, "signed-last"], "signed-last"),
    ],
)
def test_bootstrap_selects_signed_candidate_regardless_of_event_order(
    signatures: list[str | None],
    expected_signature: str,
) -> None:
    page = FakeBootstrapCandidatePage(
        _payload("101", cursor=0, has_more=False),
        [FakeBootstrapCandidateRequest(signature=value) for value in signatures],
    )

    _payload_result, signer = bootstrap_official_favorites_request(
        page,
        cursor=0,
        count=20,
        timeout_ms=1_000,
    )

    assert signer.params["a_bogus"] == expected_signature


def test_bootstrap_reports_missing_pagination_template_after_successful_response() -> (
    None
):
    page = FakeBootstrapCandidatePage(
        _payload("101", cursor=0, has_more=False),
        [FakeBootstrapCandidateRequest(signature=None)],
    )

    with pytest.raises(DouyinOfficialPageError) as error:
        bootstrap_official_favorites_request(
            page,
            cursor=0,
            count=20,
            timeout_ms=1_000,
        )

    assert error.value.reason == "pagination_template_unavailable"
    assert "signed-secret" not in str(error.value)


def test_bootstrap_candidate_diagnostic_contains_only_safe_shape(caplog: Any) -> None:
    page = FakeBootstrapCandidatePage(
        _payload("101", cursor=0, has_more=False),
        [
            FakeBootstrapCandidateRequest(signature="signed-secret"),
            FakeBootstrapCandidateRequest(signature=None),
        ],
    )

    with caplog.at_level("INFO", logger="learnnest.adapters.douyin_official_page"):
        bootstrap_official_favorites_request(
            page,
            cursor=0,
            count=20,
            timeout_ms=1_000,
        )

    assert "candidate_count=2" in caplog.text
    assert "method=POST" in caplog.text
    assert "path=/aweme/v1/web/aweme/listcollection/" in caplog.text
    assert "signature_present=[True, False]" in caplog.text
    assert "signed-secret" not in caplog.text


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
