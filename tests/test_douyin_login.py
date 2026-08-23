from __future__ import annotations

import base64
import threading
import time
from collections.abc import Callable
from typing import Any

from pydantic import SecretStr
from pytest import MonkeyPatch

import learnnest.douyin_login as douyin_login_module
from learnnest.adapters.douyin_http import DouyinAuthenticationError
from learnnest.douyin_login import DouyinLoginError, DouyinLoginSessionManager

_QR_PNG = b"\x89PNG\r\n\x1a\nqr-test"
_QR_B64 = base64.b64encode(_QR_PNG).decode("ascii")


class FakeResponse:
    def __init__(self, url: str, payload: dict[str, Any]) -> None:
        self.url = url
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeLoginButton:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    def click(self, **_kwargs: Any) -> None:
        self.page.login_clicks += 1
        if not self.page.response_events:
            return
        if self.page.context is not None and self.page.context.popup_login:
            popup = self.page.context.create_popup()
            popup._emit_qr()
            return
        self.page._emit_qr()


class LateLoginButton:
    def __init__(self, page: "LateQrPage") -> None:
        self.page = page

    def click(self, **_kwargs: Any) -> None:
        time.sleep(0.04)
        self.page._emit_qr()


class FakePage:
    def __init__(
        self,
        *,
        auto_confirm: bool = True,
        response_events: bool = True,
        official_login: bool = True,
        primary: bool = True,
    ) -> None:
        self.response_handler: Callable[[FakeResponse], None] | None = None
        self.wait_calls = 0
        self.auto_confirm = auto_confirm
        self.response_events = response_events
        self.official_login = official_login
        self.primary = primary
        self.login_clicks = 0
        self.context: FakeContext | None = None

    def get_by_role(self, _role: str, **_kwargs: Any) -> FakeLoginButton:
        if not self.official_login:
            raise AttributeError("fake page has no official login button")
        return FakeLoginButton(self)

    def on(self, event: str, callback: Callable[[FakeResponse], None]) -> None:
        assert event == "response"
        self.response_handler = callback

    def goto(self, _url: str, **_kwargs: Any) -> None:
        return None

    def reload(self, **_kwargs: Any) -> None:
        self.wait_calls = 0
        self._emit_qr()

    def evaluate(self, _script: str) -> None:
        raise AssertionError("official login must not call page.evaluate")

    def wait_for_timeout(self, _milliseconds: int) -> None:
        time.sleep(0.005)
        if self.context is not None:
            self.context.tick()
        else:
            self._tick()

    def _tick(self) -> None:
        if not self.auto_confirm or not self.response_events:
            return
        self.wait_calls += 1
        if self.wait_calls == 1:
            time.sleep(0.1)
            self._emit_status("scanned")
        elif self.wait_calls == 8:
            self._emit_status("confirmed")

    def _emit_qr(self) -> None:
        assert self.response_handler is not None
        self.response_handler(
            FakeResponse(
                "https://login.douyin.com/passport/web/get_qrcode/",
                {"data": {"error_code": 0, "qrcode": _QR_B64}},
            )
        )

    def _emit_status(self, status: str) -> None:
        assert self.response_handler is not None
        self.response_handler(
            FakeResponse(
                "https://login.douyin.com/passport/web/check_qrconnect/",
                {"data": {"error_code": 0, "status": status}},
            )
        )


class FakeContext:
    def __init__(
        self,
        closed: list[str],
        *,
        auto_confirm: bool = True,
        response_events: bool = True,
        official_login: bool = True,
        page_factory: Callable[[], FakePage] | None = None,
        popup_login: bool = False,
        authenticated_cookies: bool = True,
    ) -> None:
        self.closed = closed
        self.page_handlers: list[Callable[[FakePage], None]] = []
        self.pages: list[FakePage] = []
        self.popup_login = popup_login
        self.authenticated_cookies = authenticated_cookies
        self.page = (
            page_factory()
            if page_factory is not None
            else FakePage(
                auto_confirm=auto_confirm,
                response_events=response_events,
                official_login=official_login,
            )
        )
        self.page.context = self
        self.pages.append(self.page)

    def on(self, event: str, callback: Callable[[FakePage], None]) -> None:
        assert event == "page"
        self.page_handlers.append(callback)

    def new_page(self) -> FakePage:
        for callback in self.page_handlers:
            callback(self.page)
        return self.page

    def create_popup(self) -> FakePage:
        popup = FakePage(official_login=False, primary=False)
        popup.context = self
        self.pages.append(popup)
        for callback in self.page_handlers:
            callback(popup)
        return popup

    def tick(self) -> None:
        for page in tuple(self.pages):
            page._tick()

    def cookies(self) -> list[dict[str, str]]:
        cookies = [
            {"name": "passport_csrf_token", "value": "csrf-value", "httpOnly": True}
        ]
        if self.authenticated_cookies:
            cookies.insert(
                0,
                {
                    "name": "sessionid",
                    "value": "COOKIE" + "_SENTINEL",
                    "httpOnly": True,
                },
            )
        return cookies

    def close(self) -> None:
        self.closed.append("context")


class FakeBrowser:
    def __init__(
        self,
        closed: list[str],
        *,
        auto_confirm: bool = True,
        response_events: bool = True,
        official_login: bool = True,
        page_factory: Callable[[], FakePage] | None = None,
        popup_login: bool = False,
        authenticated_cookies: bool = True,
    ) -> None:
        self.closed = closed
        self.context = FakeContext(
            closed,
            auto_confirm=auto_confirm,
            response_events=response_events,
            official_login=official_login,
            page_factory=page_factory,
            popup_login=popup_login,
            authenticated_cookies=authenticated_cookies,
        )
        self.last_page = self.context.page

    def new_context(self, **_kwargs: Any) -> FakeContext:
        return self.context

    def close(self) -> None:
        self.closed.append("browser")


class FakePlaywright:
    def __init__(
        self,
        closed: list[str],
        launches: list[dict[str, Any]],
        *,
        launch_error: Exception | None = None,
        auto_confirm: bool = True,
        response_events: bool = True,
        official_login: bool = True,
        page_factory: Callable[[], FakePage] | None = None,
        popup_login: bool = False,
        authenticated_cookies: bool = True,
    ) -> None:
        self.closed = closed
        self.launches = launches
        self.launch_error = launch_error
        self.auto_confirm = auto_confirm
        self.response_events = response_events
        self.official_login = official_login
        self.page_factory = page_factory
        self.popup_login = popup_login
        self.authenticated_cookies = authenticated_cookies
        self.chromium = self
        self.last_page: FakePage | None = None

    def launch(self, **kwargs: Any) -> FakeBrowser:
        self.launches.append(kwargs)
        if self.launch_error is not None:
            raise self.launch_error
        browser = FakeBrowser(
            self.closed,
            auto_confirm=self.auto_confirm,
            response_events=self.response_events,
            official_login=self.official_login,
            page_factory=self.page_factory,
            popup_login=self.popup_login,
            authenticated_cookies=self.authenticated_cookies,
        )
        self.last_page = browser.last_page
        return browser

    def stop(self) -> None:
        self.closed.append("playwright")


class FakeBrowserFactory:
    def __init__(
        self,
        *,
        first_error: Exception | None = None,
        auto_confirm: bool = True,
        response_events: bool = True,
        official_login: bool = True,
        page_factory: Callable[[], FakePage] | None = None,
        popup_login: bool = False,
        authenticated_cookies: bool = True,
    ) -> None:
        self.closed: list[str] = []
        self.launches: list[dict[str, Any]] = []
        self.first_error = first_error
        self.calls = 0
        self.auto_confirm = auto_confirm
        self.response_events = response_events
        self.official_login = official_login
        self.page_factory = page_factory
        self.popup_login = popup_login
        self.authenticated_cookies = authenticated_cookies
        self.last_playwright: FakePlaywright | None = None

    def __call__(self) -> FakePlaywright:
        self.calls += 1
        error = self.first_error if self.calls == 1 else None
        playwright = FakePlaywright(
            self.closed,
            self.launches,
            launch_error=error,
            auto_confirm=self.auto_confirm,
            response_events=self.response_events,
            official_login=self.official_login,
            page_factory=self.page_factory,
            popup_login=self.popup_login,
            authenticated_cookies=self.authenticated_cookies,
        )
        self.last_playwright = playwright
        return playwright


class LateQrPage(FakePage):
    def __init__(self) -> None:
        super().__init__(official_login=True)

    def get_by_role(self, _role: str, **_kwargs: Any) -> LateLoginButton:
        return LateLoginButton(self)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        time.sleep(0.005)
        self.wait_calls += 1
        if self.wait_calls == 3:
            self._emit_status("confirmed")


class ExpiringQrPage(FakePage):
    def __init__(self) -> None:
        super().__init__(official_login=True, auto_confirm=False)

    def _emit_qr(self) -> None:
        assert self.response_handler is not None
        self.response_handler(
            FakeResponse(
                "https://login.douyin.com/passport/web/get_qrcode/",
                {
                    "data": {
                        "error_code": 0,
                        "qrcode": _QR_B64,
                        "expire_time": time.time() + 0.03,
                    }
                },
            )
        )


class VerificationPage(FakePage):
    def __init__(self) -> None:
        super().__init__(official_login=True, auto_confirm=False)

    def _tick(self) -> None:
        self.wait_calls += 1
        if self.wait_calls == 1:
            self._emit_status("new")
        elif self.wait_calls == 2:
            assert self.response_handler is not None
            self.response_handler(
                FakeResponse(
                    "https://login.douyin.com/passport/web/check_qrconnect/",
                    {
                        "data": {
                            "error_code": 2046,
                            "account_flow": "verify",
                        }
                    },
                )
            )
        elif self.wait_calls == 20:
            assert self.context is not None
            self.context.authenticated_cookies = True


class RepeatingVerificationPage(VerificationPage):
    def _tick(self) -> None:
        self.wait_calls += 1
        if self.wait_calls == 1:
            self._emit_status("new")
            return
        assert self.response_handler is not None
        self.response_handler(
            FakeResponse(
                "https://login.douyin.com/passport/web/check_qrconnect/",
                {
                    "data": {
                        "error_code": 2046,
                        "account_flow": "verify",
                    }
                },
            )
        )


class BrowserLoginPage(FakePage):
    def __init__(self) -> None:
        super().__init__(official_login=True, auto_confirm=False)

    def _emit_qr(self) -> None:
        assert self.response_handler is not None
        self.response_handler(
            FakeResponse(
                "https://login.douyin.com/passport/web/get_qrcode/",
                {"data": {"error_code": 22}},
            )
        )

    def _tick(self) -> None:
        self.wait_calls += 1
        if self.wait_calls == 1:
            self._emit_status("expired")
        elif self.wait_calls == 20:
            assert self.context is not None
            self.context.authenticated_cookies = True


class BrowserLoginAfterNavigationTimeoutPage(BrowserLoginPage):
    def goto(self, *_args: Any, **_kwargs: Any) -> None:
        raise TimeoutError


class ConfirmedWithoutCookiePage(BrowserLoginPage):
    def _tick(self) -> None:
        self.wait_calls += 1
        if self.wait_calls == 1:
            self._emit_status("scanned")
        elif self.wait_calls == 2:
            self._emit_status("confirmed")


class FakeCookieStore:
    def __init__(self, loaded: SecretStr | None = None) -> None:
        self.loaded = loaded
        self.saved: list[SecretStr] = []
        self.clear_calls = 0

    def load(self) -> SecretStr | None:
        return self.loaded

    def save(self, cookie: SecretStr) -> None:
        self.saved.append(cookie)
        self.loaded = cookie

    def clear(self) -> None:
        self.clear_calls += 1
        self.loaded = None


def _valid_smoke_payload() -> dict[str, Any]:
    return {"status_code": 0, "aweme_list": [{"aweme_id": "123"}]}


def _wait_for_status(
    manager: DouyinLoginSessionManager,
    session_id: str,
    expected: set[str],
    *,
    timeout: float = 1.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.get_session(session_id)
        if state["status"] in expected:
            return state
        time.sleep(0.005)
    raise AssertionError(manager.get_session(session_id))


def test_login_manager_captures_qr_and_closes_browser_after_http_smoke() -> None:
    factory = FakeBrowserFactory()
    smoke_cookies: list[SecretStr] = []

    def smoke(cookie: SecretStr) -> dict[str, Any]:
        smoke_cookies.append(cookie)
        return _valid_smoke_payload()

    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=smoke,
    )

    created = manager.create_session()
    qr_state = _wait_for_status(manager, created["session_id"], {"qr_ready"})
    assert qr_state["expires_in"] > 0
    assert manager.qr_image(created["session_id"]) == _QR_PNG
    connected = _wait_for_status(
        manager, created["session_id"], {"connected"}, timeout=1.0
    )

    assert connected == {
        "session_id": created["session_id"],
        "status": "connected",
        "expires_in": 0,
        "qr_available": False,
        "message": "登录凭据与收藏访问均已验证，可以同步收藏。",
    }
    assert smoke_cookies[0].get_secret_value() == (
        "sessionid=COOKIE" + "_SENTINEL; passport_csrf_token=csrf-value"
    )
    assert factory.launches == [{"channel": "msedge", "headless": False}]
    assert factory.closed.count("context") == 1
    assert factory.closed.count("browser") == 1
    assert factory.closed.count("playwright") == 1

    manager.shutdown()


def test_login_manager_uses_response_events_without_page_evaluation() -> None:
    factory = FakeBrowserFactory()
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_session()
    _wait_for_status(manager, session["session_id"], {"connected"})
    manager.shutdown()


def test_login_manager_prefers_official_site_login_button_and_events() -> None:
    factory = FakeBrowserFactory(official_login=True)
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_session()
    _wait_for_status(manager, session["session_id"], {"connected"}, timeout=2.0)

    assert factory.last_playwright is not None
    assert factory.last_playwright.last_page is not None
    assert factory.last_playwright.last_page.login_clicks == 1
    manager.shutdown()


def test_login_manager_attaches_popup_page_from_official_login() -> None:
    factory = FakeBrowserFactory(popup_login=True)
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_session()
    _wait_for_status(manager, session["session_id"], {"connected"})

    assert factory.last_playwright is not None
    assert factory.last_playwright.last_page is not None
    assert len(factory.last_playwright.last_page.context.pages) == 2
    manager.shutdown()


def test_login_manager_keeps_official_browser_open_for_secondary_verification() -> None:
    factory = FakeBrowserFactory(
        page_factory=VerificationPage,
        authenticated_cookies=False,
    )
    smoke_cookies: list[SecretStr] = []

    def smoke(cookie: SecretStr) -> dict[str, Any]:
        smoke_cookies.append(cookie)
        return _valid_smoke_payload()

    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=smoke,
    )

    session = manager.create_session()
    verification = _wait_for_status(
        manager,
        session["session_id"],
        {"verification_required"},
    )
    assert verification["qr_available"] is False
    assert verification["expires_in"] > 0
    assert factory.closed == []

    connected = _wait_for_status(
        manager,
        session["session_id"],
        {"connected"},
        timeout=2.0,
    )
    assert connected["status"] == "connected"
    assert smoke_cookies[0].get_secret_value().startswith("sessionid=")
    assert factory.closed.count("context") == 1
    assert factory.closed.count("browser") == 1
    assert factory.closed.count("playwright") == 1
    manager.shutdown()


def test_secondary_verification_has_a_fixed_cleanup_deadline(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(douyin_login_module, "_VERIFICATION_TTL", 0.03)
    factory = FakeBrowserFactory(
        page_factory=RepeatingVerificationPage,
        authenticated_cookies=False,
    )
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_session()
    expired = _wait_for_status(manager, session["session_id"], {"expired"})
    cleanup_deadline = time.monotonic() + 1.0
    while factory.closed.count("context") == 0 and time.monotonic() < cleanup_deadline:
        time.sleep(0.005)

    assert expired["qr_available"] is False
    assert factory.closed.count("context") == 1
    assert factory.closed.count("browser") == 1
    assert factory.closed.count("playwright") == 1
    manager.shutdown()


def test_browser_login_waits_for_official_cookie_and_persists_it() -> None:
    store = FakeCookieStore()
    factory = FakeBrowserFactory(
        page_factory=BrowserLoginPage,
        authenticated_cookies=False,
    )
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
        cookie_store=store,
    )

    session = manager.create_browser_session()
    ready = _wait_for_status(manager, session["session_id"], {"browser_ready"})

    assert ready["qr_available"] is False
    assert factory.closed == []

    connected = _wait_for_status(
        manager,
        session["session_id"],
        {"connected"},
        timeout=2.0,
    )
    assert manager.current_session() == connected
    assert store.saved[0].get_secret_value().startswith("sessionid=")
    assert factory.closed.count("context") == 1
    manager.shutdown()
    assert store.loaded is not None


def test_browser_login_projects_secondary_verification_progress() -> None:
    factory = FakeBrowserFactory(
        page_factory=VerificationPage,
        authenticated_cookies=False,
    )
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_browser_session()
    verification = _wait_for_status(
        manager,
        session["session_id"],
        {"verification_required"},
    )

    assert verification["message"] == ("抖音要求继续验证，请在原官方窗口完成页面提示。")
    _wait_for_status(manager, session["session_id"], {"connected"}, timeout=2.0)
    manager.shutdown()


def test_browser_login_projects_favorites_validation_before_success() -> None:
    entered = threading.Event()
    release = threading.Event()

    def smoke(_cookie: SecretStr) -> dict[str, Any]:
        entered.set()
        release.wait(timeout=1.0)
        return _valid_smoke_payload()

    manager = DouyinLoginSessionManager(
        playwright_factory=FakeBrowserFactory(page_factory=BrowserLoginPage),
        favorites_smoke=smoke,
    )

    session = manager.create_browser_session()
    assert entered.wait(timeout=1.0)
    try:
        validating = manager.get_session(session["session_id"])
        assert validating["status"] == "validating"
        assert validating["message"] == "已取得登录凭据，正在验证收藏访问。"
    finally:
        release.set()
    _wait_for_status(manager, session["session_id"], {"connected"})
    manager.shutdown()


def test_failed_browser_login_keeps_the_specific_safe_result_for_page_restore() -> None:
    def unavailable(_cookie: SecretStr) -> dict[str, Any]:
        raise DouyinLoginError("已取得登录凭据，但收藏接口暂不可用。")

    manager = DouyinLoginSessionManager(
        playwright_factory=FakeBrowserFactory(page_factory=BrowserLoginPage),
        favorites_smoke=unavailable,
    )

    session = manager.create_browser_session()
    failed = _wait_for_status(manager, session["session_id"], {"failed"})

    assert failed["message"] == "已取得登录凭据，但收藏接口暂不可用。"
    assert manager.current_session() == failed
    manager.shutdown()


def test_browser_login_reports_http_request_rejection_as_protocol_drift() -> None:
    def rejected(_cookie: SecretStr) -> dict[str, Any]:
        raise DouyinAuthenticationError(
            "Douyin API request returned HTTP 403",
            reason="request_rejected",
            status_code=403,
        )

    manager = DouyinLoginSessionManager(
        playwright_factory=FakeBrowserFactory(page_factory=BrowserLoginPage),
        favorites_smoke=rejected,
    )

    session = manager.create_browser_session()
    failed = _wait_for_status(manager, session["session_id"], {"failed"})

    assert failed["message"] == (
        "登录凭据已取得，但收藏请求被抖音拦截（HTTP 403）；"
        "当前网页接口校验已变化，不是扫码或验证码失败。"
    )
    assert failed["failure_kind"] == "request_rejected"
    assert manager.current_session() == failed
    manager.shutdown()


def test_browser_login_reports_business_authentication_code() -> None:
    manager = DouyinLoginSessionManager(
        playwright_factory=FakeBrowserFactory(page_factory=BrowserLoginPage),
        favorites_smoke=lambda _cookie: {
            "status_code": 1001,
            "aweme_list": [],
        },
    )

    session = manager.create_browser_session()
    failed = _wait_for_status(manager, session["session_id"], {"failed"})

    assert failed["message"] == (
        "登录凭据已取得，但收藏接口返回登录失效（状态码 1001）。"
    )
    manager.shutdown()


def test_confirmed_browser_login_reports_when_no_usable_cookie_arrives(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(douyin_login_module, "_POST_CONFIRM_TTL", 0.03)
    manager = DouyinLoginSessionManager(
        playwright_factory=FakeBrowserFactory(
            page_factory=ConfirmedWithoutCookiePage,
            authenticated_cookies=False,
        ),
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_browser_session()
    expired = _wait_for_status(manager, session["session_id"], {"expired"})

    assert expired["message"] == "验证窗口已超时，系统没有取得可用登录凭据。"
    assert manager.current_session() == expired
    manager.shutdown()


def test_browser_login_reports_local_encrypted_save_failure_without_details() -> None:
    class FailingCookieStore(FakeCookieStore):
        def save(self, cookie: SecretStr) -> None:
            del cookie
            raise OSError("PRIVATE_PATH COOKIE_SENTINEL")

    manager = DouyinLoginSessionManager(
        playwright_factory=FakeBrowserFactory(page_factory=BrowserLoginPage),
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
        cookie_store=FailingCookieStore(),
    )

    session = manager.create_browser_session()
    failed = _wait_for_status(manager, session["session_id"], {"failed"})

    assert failed["message"] == "登录凭据已通过校验，但无法安全保存到本机。"
    assert "PRIVATE_PATH" not in str(failed)
    assert "COOKIE_SENTINEL" not in str(failed)
    manager.shutdown()


def test_browser_login_uses_official_controls_after_navigation_timeout() -> None:
    store = FakeCookieStore()
    factory = FakeBrowserFactory(
        page_factory=BrowserLoginAfterNavigationTimeoutPage,
        authenticated_cookies=False,
    )
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
        cookie_store=store,
    )

    session = manager.create_browser_session()
    connected = _wait_for_status(
        manager,
        session["session_id"],
        {"connected"},
        timeout=2.0,
    )

    assert connected["status"] == "connected"
    assert factory.last_playwright is not None
    assert factory.last_playwright.last_page is not None
    assert factory.last_playwright.last_page.login_clicks == 1
    assert len(store.saved) == 1
    manager.shutdown()


def test_manager_restores_valid_persisted_cookie_without_browser() -> None:
    cookie = SecretStr("sessionid=PERSISTED" + "_COOKIE_SENTINEL")
    store = FakeCookieStore(cookie)
    factory = FakeBrowserFactory()
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda received: (
            _valid_smoke_payload() if received == cookie else {}
        ),
        cookie_store=store,
    )

    current = manager.current_session()

    assert current["status"] == "connected"
    assert current["session_id"]
    assert manager.cookie_for(current["session_id"]) == cookie
    assert factory.calls == 0
    manager.shutdown()
    assert store.clear_calls == 0


def test_manager_clears_rejected_persisted_cookie() -> None:
    store = FakeCookieStore(SecretStr("sessionid=REJECTED" + "_COOKIE_SENTINEL"))

    def reject(_cookie: SecretStr) -> dict[str, Any]:
        raise DouyinAuthenticationError("authentication failed")

    manager = DouyinLoginSessionManager(
        playwright_factory=FakeBrowserFactory(),
        favorites_smoke=reject,
        cookie_store=store,
    )

    assert manager.current_session()["status"] == "disconnected"
    assert store.clear_calls == 1
    manager.shutdown()


def test_manager_clears_explicit_business_auth_rejection() -> None:
    store = FakeCookieStore(SecretStr("sessionid=REJECTED" + "_COOKIE_SENTINEL"))
    manager = DouyinLoginSessionManager(
        playwright_factory=FakeBrowserFactory(),
        favorites_smoke=lambda _cookie: {
            "status_code": 401,
            "aweme_list": [],
        },
        cookie_store=store,
    )

    assert manager.current_session()["status"] == "disconnected"
    assert store.clear_calls == 1
    manager.shutdown()


def test_manager_preserves_persisted_cookie_on_ambiguous_upstream_failure() -> None:
    cookie = SecretStr("sessionid=RETRY_LATER" + "_COOKIE_SENTINEL")
    store = FakeCookieStore(cookie)

    def unavailable(_cookie: SecretStr) -> dict[str, Any]:
        raise DouyinLoginError("收藏接口暂不可用，登录未完成。")

    manager = DouyinLoginSessionManager(
        playwright_factory=FakeBrowserFactory(),
        favorites_smoke=unavailable,
        cookie_store=store,
    )

    assert manager.current_session()["status"] == "disconnected"
    assert store.loaded == cookie
    assert store.clear_calls == 0
    manager.shutdown()


def test_login_manager_rejects_business_failure_from_favorites_smoke() -> None:
    factory = FakeBrowserFactory()
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: {
            "status_code": 999,
            "aweme_list": [{"aweme_id": "123"}],
        },
    )

    session = manager.create_session()
    failed = _wait_for_status(manager, session["session_id"], {"failed"})

    assert failed["status"] == "failed"
    assert factory.closed.count("context") == 1
    manager.shutdown()


def test_refresh_recycles_old_context_before_starting_new_generation() -> None:
    factory = FakeBrowserFactory(auto_confirm=False)
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_session()
    _wait_for_status(manager, session["session_id"], {"qr_ready"})
    refreshed = manager.refresh_session(session["session_id"])
    assert refreshed["status"] == "starting"
    _wait_for_status(manager, session["session_id"], {"qr_ready"})

    assert len(factory.launches) == 2
    assert factory.closed.count("context") == 1
    manager.cancel_session(session["session_id"])
    manager.shutdown()


def test_qr_ready_near_startup_deadline_is_not_killed_after_qr() -> None:
    factory = FakeBrowserFactory(page_factory=LateQrPage)
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
        startup_timeout=0.05,
    )

    session = manager.create_session()
    connected = _wait_for_status(
        manager, session["session_id"], {"connected"}, timeout=1.0
    )

    assert connected["status"] == "connected"
    manager.shutdown()


def test_login_manager_redacts_browser_exception_before_public_state() -> None:
    cookie_sentinel = "COOKIE" + "_SENTINEL"
    token_sentinel = "TOKEN" + "_SENTINEL"
    factory = FakeBrowserFactory(
        first_error=RuntimeError(f"Cookie={cookie_sentinel} Token={token_sentinel}")
    )
    manager = DouyinLoginSessionManager(playwright_factory=factory)

    session = manager.create_session()
    state = _wait_for_status(manager, session["session_id"], {"failed"})

    assert cookie_sentinel not in str(state)
    assert token_sentinel not in str(state)
    assert (
        manager._sessions[session["session_id"]].error == "登录失败，请重新连接抖音。"
    )
    assert state["status"] == "failed"
    assert factory.closed == ["playwright"]
    manager.shutdown()


def test_login_manager_uses_only_headed_isolated_edge() -> None:
    factory = FakeBrowserFactory()
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_session()
    _wait_for_status(manager, session["session_id"], {"connected"})

    assert factory.launches == [{"channel": "msedge", "headless": False}]
    manager.shutdown()


def test_login_manager_cancel_closes_isolated_context() -> None:
    factory = FakeBrowserFactory(auto_confirm=False)
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_session()
    _wait_for_status(manager, session["session_id"], {"qr_ready"})
    cancelled = manager.cancel_session(session["session_id"])
    _wait_for_status(manager, session["session_id"], {"cancelled"})
    deadline = time.monotonic() + 1
    while "context" not in factory.closed and time.monotonic() < deadline:
        time.sleep(0.005)

    assert cancelled["status"] == "cancelled"
    assert factory.closed.count("context") == 1
    assert factory.closed.count("browser") == 1
    manager.shutdown()


def test_login_manager_qr_expiry_closes_isolated_context() -> None:
    factory = FakeBrowserFactory(page_factory=ExpiringQrPage)
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_session()
    expired = _wait_for_status(manager, session["session_id"], {"expired"})

    assert expired["status"] == "expired"
    deadline = time.monotonic() + 1
    while "context" not in factory.closed and time.monotonic() < deadline:
        time.sleep(0.005)
    assert factory.closed.count("context") == 1
    assert factory.closed.count("browser") == 1
    manager.shutdown()


def test_login_manager_shutdown_closes_active_browser() -> None:
    factory = FakeBrowserFactory(auto_confirm=False)
    manager = DouyinLoginSessionManager(
        playwright_factory=factory,
        favorites_smoke=lambda _cookie: _valid_smoke_payload(),
    )

    session = manager.create_session()
    _wait_for_status(manager, session["session_id"], {"qr_ready"})
    manager.shutdown()

    assert manager.get_session(session["session_id"])["status"] == "cancelled"
    assert factory.closed.count("context") == 1
    assert factory.closed.count("browser") == 1
