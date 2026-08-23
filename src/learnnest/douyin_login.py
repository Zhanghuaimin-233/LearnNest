"""Official-page-assisted Douyin Passport login.

The browser is confined to one headed, non-persistent Edge context per
attempt. The official site owns every credential and verification interaction;
this module only observes the resulting CookieJar and runs one strict favorites
smoke request before declaring the session connected. An injected credential
store may persist that CookieJar outside project facts.
"""

from __future__ import annotations

import base64
import logging
import math
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from playwright.sync_api import sync_playwright
from pydantic import SecretStr

from learnnest.adapters.douyin import DouyinAdapterError
from learnnest.adapters.douyin_http import (
    DouyinAuthenticationError,
    douyin_authentication_message,
)
from learnnest.adapters.douyin_official_page import collect_official_favorites

_DOUYIN_HOME_URL = "https://www.douyin.com/"
_QR_PATH = "/passport/web/get_qrcode/"
_CHECK_QR_PATH = "/passport/web/check_qrconnect/"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_AUTH_COOKIE_NAMES = frozenset({"sessionid", "sessionid_ss", "sid_guard", "sid_tt"})
_AUTH_STATUS_CODES = frozenset({"401", "403", "-1", "1001", "1002"})
_BROWSER_LOGIN_TTL = 300.0
_POST_CONFIRM_TTL = 30.0
_VERIFICATION_TTL = 300.0
_SAFE_FAILURE = "登录失败，请重新连接抖音。"
_RECONNECT_FAILURE = "登录已失效，请重新连接抖音。"
_AUTHENTICATION_FAILURE = "已取得登录凭据，但抖音未接受本次登录。"
_COOKIE_SAVE_FAILURE = "登录凭据已通过校验，但无法安全保存到本机。"
_EXPIRED_FAILURE = "验证窗口已超时，系统没有取得可用登录凭据。"
_STATUS_MESSAGES = {
    "starting": "正在打开抖音官方验证窗口。",
    "browser_ready": "官方验证窗口已打开，等待完成手机号与扫码验证。",
    "qr_ready": "请使用抖音 App 扫描二维码。",
    "scanned": "已扫码，请在手机或官方窗口继续确认。",
    "confirmed": "已确认，正在等待抖音签发登录凭据。",
    "verification_required": "抖音要求继续验证，请在原官方窗口完成页面提示。",
    "validating": "已取得登录凭据，正在通过抖音官方页面验证收藏访问。",
    "connected": "登录凭据与收藏访问均已验证，可以同步收藏。",
    "expired": _EXPIRED_FAILURE,
    "failed": _SAFE_FAILURE,
    "cancelled": "已取消本次抖音连接。",
}
_LOGGER = logging.getLogger(__name__)


class DouyinLoginError(RuntimeError):
    """A safe login-session error suitable for a local API boundary."""


class BrowserFactory(Protocol):
    """Create a Playwright runtime inside the session worker thread."""

    def __call__(self) -> Any: ...


class FavoritesSmoke(Protocol):
    """Return the raw response from the internal favorites smoke request."""

    def __call__(self, cookie: SecretStr) -> Mapping[str, Any]: ...


class CookieStore(Protocol):
    """Persist an encrypted Cookie header outside project facts."""

    def load(self) -> SecretStr | None: ...

    def save(self, cookie: SecretStr) -> None: ...

    def clear(self) -> None: ...


@dataclass
class _LoginSession:
    session_id: str
    login_mode: str = field(default="qr", repr=False)
    status: str = "starting"
    expires_at: float | None = None
    qr_png: bytes | None = None
    cookie: SecretStr | None = field(default=None, repr=False)
    error: str | None = field(default=None, repr=False)
    failure_kind: str | None = field(default=None, repr=False)
    generation: int = field(default=0, repr=False)
    worker: threading.Thread | None = field(default=None, repr=False)
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    startup_deadline: float | None = field(default=None, repr=False)


class DouyinLoginSessionManager:
    """Own isolated official-page login attempts and expose safe state only."""

    def __init__(
        self,
        *,
        playwright_factory: BrowserFactory | None = None,
        favorites_smoke: FavoritesSmoke | None = None,
        cookie_store: CookieStore | None = None,
        qr_ttl: int = 60,
        startup_timeout: float = 15.0,
    ) -> None:
        if qr_ttl < 1:
            raise ValueError("Douyin QR TTL must be positive")
        if startup_timeout <= 0:
            raise ValueError("Douyin login startup timeout must be positive")
        self._playwright_factory = playwright_factory or _default_playwright_factory
        self._favorites_smoke = favorites_smoke
        self._cookie_store = cookie_store
        self._qr_ttl = qr_ttl
        self._startup_timeout = startup_timeout
        self._sessions: dict[str, _LoginSession] = {}
        self._current_session_id: str | None = None
        self._latest_session_id: str | None = None
        self._lock = threading.RLock()
        self._shutdown = False
        self._restore_persisted_session()

    def create_session(self) -> dict[str, Any]:
        """Create a random local session handle and start one headed attempt."""
        return self._create_session("qr")

    def create_browser_session(self) -> dict[str, Any]:
        """Open the official login window without accepting credentials."""
        return self._create_session("browser")

    def current_session(self) -> dict[str, Any]:
        """Return the latest safe session state without exposing credentials."""
        with self._lock:
            if self._current_session_id is not None:
                session = self._sessions.get(self._current_session_id)
                if session is not None and session.status == "connected":
                    return self._public_payload_locked(session)
            if self._latest_session_id is not None:
                session = self._sessions.get(self._latest_session_id)
                if session is not None:
                    self._expire_if_needed_locked(session)
                    return self._public_payload_locked(session)
            return {
                "session_id": None,
                "status": "disconnected",
                "expires_in": 0,
                "qr_available": False,
                "message": "尚未连接抖音。",
            }

    def _create_session(self, login_mode: str) -> dict[str, Any]:
        with self._lock:
            if self._shutdown:
                raise DouyinLoginError("登录服务已关闭。")
            session = _LoginSession(
                session_id=uuid.uuid4().hex,
                login_mode=login_mode,
            )
            self._sessions[session.session_id] = session
            self._latest_session_id = session.session_id
            session.generation = 1
            self._spawn_worker_locked(session, session.generation)
            return self._public_payload_locked(session)

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._require_session_locked(session_id)
            self._expire_if_needed_locked(session)
            return self._public_payload_locked(session)

    def refresh_session(self, session_id: str) -> dict[str, Any]:
        """Recycle the old attempt before creating a fresh isolated context."""
        with self._lock:
            session = self._require_session_locked(session_id)
            old_worker = session.worker
            old_stop = session.stop_event
            session.generation += 1
            generation = session.generation
            old_stop.set()
            session.cookie = None
            session.status = "starting"
            session.expires_at = None
            session.qr_png = None
            session.error = None
            session.failure_kind = None
            session.startup_deadline = None

        self._join_worker(old_worker)

        with self._lock:
            if (
                self._shutdown
                or session.generation != generation
                or session.status == "cancelled"
            ):
                return self._public_payload_locked(session)
            if old_worker is not None and old_worker.is_alive():
                session.status = "failed"
                session.error = _SAFE_FAILURE
                return self._public_payload_locked(session)
            self._spawn_worker_locked(session, generation)
            return self._public_payload_locked(session)

    def cancel_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._require_session_locked(session_id)
            worker = session.worker
            session.generation += 1
            session.stop_event.set()
            session.cookie = None
            session.qr_png = None
            session.expires_at = None
            session.startup_deadline = None
            session.error = None
            session.failure_kind = None
            session.status = "cancelled"
            if self._current_session_id == session_id:
                self._current_session_id = None
        self._join_worker(worker)
        with self._lock:
            return self._public_payload_locked(session)

    def qr_image(self, session_id: str) -> bytes:
        with self._lock:
            session = self._require_session_locked(session_id)
            self._expire_if_needed_locked(session)
            if session.qr_png is None:
                raise DouyinLoginError("二维码暂不可用。")
            return session.qr_png

    def cookie_for(self, session_id: str) -> SecretStr:
        """Return a connected CookieJar to an internal HTTP consumer only."""
        with self._lock:
            session = self._require_session_locked(session_id)
            if session.status != "connected" or session.cookie is None:
                raise DouyinLoginError("请先连接抖音。")
            return session.cookie

    def invalidate(self, session_id: str) -> None:
        """Forget a rejected CookieJar and require a fresh official login."""
        with self._lock:
            session = self._require_session_locked(session_id)
            worker = session.worker
            session.generation += 1
            session.stop_event.set()
            session.cookie = None
            session.qr_png = None
            session.expires_at = None
            session.startup_deadline = None
            session.status = "failed"
            session.error = _RECONNECT_FAILURE
            if self._current_session_id == session_id:
                self._current_session_id = None
        self._join_worker(worker)
        self._clear_persisted_cookie()

    def shutdown(self) -> None:
        """Stop every worker and clear every in-memory CookieJar."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._current_session_id = None
            sessions = list(self._sessions.values())
            workers: list[threading.Thread | None] = []
            for session in sessions:
                workers.append(session.worker)
                session.generation += 1
                session.stop_event.set()
                session.cookie = None
                session.startup_deadline = None
                if session.status not in {"connected", "failed"}:
                    session.status = "cancelled"
        for worker in workers:
            self._join_worker(worker)

    def _restore_persisted_session(self) -> None:
        if self._cookie_store is None:
            return
        try:
            cookie = self._cookie_store.load()
        except Exception as error:
            _LOGGER.warning(
                "douyin login outcome=persisted_cookie_unavailable exception_type=%s",
                type(error).__name__,
            )
            return
        if cookie is None:
            return
        if self._favorites_smoke is not None:
            try:
                smoke_payload = self._favorites_smoke(cookie)
                _validate_favorites_smoke(smoke_payload)
            except DouyinAuthenticationError as error:
                _LOGGER.warning(
                    "douyin login outcome=persisted_cookie_rejected "
                    "reason=%s status_code=%s",
                    error.reason,
                    error.status_code if error.status_code is not None else "unknown",
                )
                self._clear_persisted_cookie()
                return
            except Exception as error:
                _LOGGER.warning(
                    "douyin login outcome=persisted_cookie_unverified "
                    "exception_type=%s",
                    type(error).__name__,
                )
                return
        session = _LoginSession(
            session_id=uuid.uuid4().hex,
            login_mode="restored",
            status="connected",
            cookie=cookie,
        )
        self._sessions[session.session_id] = session
        self._current_session_id = session.session_id
        self._latest_session_id = session.session_id

    def _clear_persisted_cookie(self) -> None:
        if self._cookie_store is None:
            return
        try:
            self._cookie_store.clear()
        except Exception as error:
            _LOGGER.warning(
                "douyin login outcome=persisted_cookie_clear_failed exception_type=%s",
                type(error).__name__,
            )

    def _spawn_worker_locked(self, session: _LoginSession, generation: int) -> None:
        if session.worker is not None and session.worker.is_alive():
            raise DouyinLoginError("登录旧会话尚未回收。")
        stop_event = threading.Event()
        session.stop_event = stop_event
        session.startup_deadline = time.monotonic() + self._startup_timeout
        worker = threading.Thread(
            target=self._run_session,
            args=(session, generation, stop_event),
            name=f"douyin-login-{session.session_id}-{generation}",
            daemon=True,
        )
        session.worker = worker
        worker.start()

    def _run_session(
        self,
        session: _LoginSession,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        try:
            self._run_browser_attempt(session, generation, stop_event)
        except _LoginCancelled:
            return
        except _LoginExpired:
            self._mark_expired(session, generation)
        except _LoginStartupTimeout:
            _LOGGER.warning("douyin login outcome=startup_timeout")
            self._mark_failed(session, generation, _SAFE_FAILURE)
        except DouyinAuthenticationError as error:
            _LOGGER.warning(
                "douyin login outcome=favorites_authentication_rejected "
                "reason=%s status_code=%s",
                error.reason,
                error.status_code if error.status_code is not None else "unknown",
            )
            self._mark_failed(
                session,
                generation,
                douyin_authentication_message(error),
                failure_kind=error.reason,
            )
        except DouyinLoginError as error:
            _LOGGER.warning(
                "douyin login outcome=safe_failure exception_type=%s",
                type(error).__name__,
            )
            self._mark_failed(session, generation, str(error))
        except Exception as error:
            _LOGGER.warning(
                "douyin login outcome=exception exception_type=%s",
                type(error).__name__,
            )
            self._mark_failed(session, generation, _SAFE_FAILURE)
        finally:
            self._worker_finished(session, generation)

    def _run_browser_attempt(
        self,
        session: _LoginSession,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        runtime: Any | None = None
        browser: Any | None = None
        context: Any | None = None
        try:
            runtime = self._playwright_factory()
            browser = runtime.chromium.launch(channel="msedge", headless=False)
            context = browser.new_context(locale="zh-CN")
            attached_pages: set[int] = set()

            def attach_page(page: Any) -> None:
                marker = id(page)
                if marker in attached_pages:
                    return
                attached_pages.add(marker)
                page.on(
                    "response",
                    lambda response: self._handle_response(
                        session, generation, response
                    ),
                )

            context.on("page", attach_page)
            page = context.new_page()
            attach_page(page)
            self._ensure_current(session, generation, stop_event)
            try:
                page.goto(
                    _DOUYIN_HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=15_000,
                )
            except Exception as error:
                # Douyin can keep the top-level navigation pending after the
                # login controls are already usable. The button lookup below
                # remains the real readiness gate.
                _LOGGER.warning(
                    "douyin login outcome=navigation_incomplete exception_type=%s",
                    type(error).__name__,
                )
            try:
                page.get_by_role("button", name="登录", exact=True).click(
                    timeout=15_000
                )
            except Exception:
                raise DouyinLoginError("官方登录入口不可用。") from None
            if session.login_mode == "browser":
                with self._lock:
                    if not self._is_current_locked(session, generation):
                        raise _LoginCancelled
                    session.status = "browser_ready"
                    session.expires_at = time.monotonic() + _BROWSER_LOGIN_TTL
                    session.qr_png = None
                    session.startup_deadline = None
                    session.error = None
                    session.failure_kind = None

            while True:
                self._ensure_current(session, generation, stop_event)
                with self._lock:
                    self._expire_if_needed_locked(session)
                    if session.status == "failed":
                        raise DouyinLoginError("官方登录未完成。")
                    if session.status == "expired":
                        raise _LoginExpired
                    login_ready = session.status in {"browser_ready", "qr_ready"}
                    deadline = session.startup_deadline
                if login_ready:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    raise _LoginStartupTimeout
                page.wait_for_timeout(100)

            while True:
                self._ensure_current(session, generation, stop_event)
                with self._lock:
                    self._expire_if_needed_locked(session)
                    if session.status == "failed":
                        raise DouyinLoginError("官方登录未完成。")
                    if session.status == "expired":
                        raise _LoginExpired
                    auth_candidate = session.status in {
                        "browser_ready",
                        "scanned",
                        "confirmed",
                        "verification_required",
                    }
                    deadline = session.startup_deadline
                    qr_ready = session.qr_png is not None
                if not qr_ready and not auth_candidate:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise _LoginStartupTimeout
                if auth_candidate:
                    cookies = context.cookies()
                    if not _has_authenticated_cookie(cookies):
                        page.wait_for_timeout(100)
                        continue
                    cookie = _cookie_header(cookies)
                    cookie_secret = SecretStr(cookie)
                    with self._lock:
                        if not self._is_current_locked(session, generation):
                            raise _LoginCancelled
                        session.status = "validating"
                        session.expires_at = None
                        session.error = None
                        session.failure_kind = None
                    if self._favorites_smoke is not None:
                        smoke_payload = self._favorites_smoke(cookie_secret)
                    else:
                        try:
                            smoke_payload = collect_official_favorites(
                                page,
                                first_page_only=True,
                            )
                        except DouyinAuthenticationError:
                            raise
                        except DouyinAdapterError:
                            raise DouyinLoginError(
                                "已取得登录凭据，但抖音官方页面没有返回可验证的收藏结果。"
                            ) from None
                    _validate_favorites_smoke(smoke_payload)
                    if self._cookie_store is not None:
                        try:
                            self._cookie_store.save(cookie_secret)
                        except Exception as error:
                            _LOGGER.warning(
                                "douyin login outcome=cookie_save_failed exception_type=%s",
                                type(error).__name__,
                            )
                            raise DouyinLoginError(_COOKIE_SAVE_FAILURE) from error
                    with self._lock:
                        if (
                            self._shutdown
                            or session.generation != generation
                            or stop_event.is_set()
                        ):
                            raise _LoginCancelled
                        session.cookie = cookie_secret
                        session.status = "connected"
                        session.expires_at = None
                        session.qr_png = None
                        session.startup_deadline = None
                        session.error = None
                        self._current_session_id = session.session_id
                    return
                page.wait_for_timeout(100)
        finally:
            _close_runtime(context, browser, runtime)

    def _handle_response(
        self, session: _LoginSession, generation: int, response: Any
    ) -> None:
        try:
            url = str(response.url)
            payload = response.json()
        except Exception:
            return
        if not isinstance(payload, Mapping):
            return
        if _QR_PATH in url:
            _log_official_response(response, _QR_PATH, payload)
            self._handle_qr_payload(session, generation, payload)
        elif _CHECK_QR_PATH in url:
            _log_official_response(response, _CHECK_QR_PATH, payload)
            self._handle_check_payload(session, generation, payload)

    def _handle_qr_payload(
        self,
        session: _LoginSession,
        generation: int,
        payload: Mapping[str, Any],
    ) -> None:
        with self._lock:
            if not self._is_current_locked(session, generation):
                return
            if session.login_mode == "browser":
                # The official page may use this QR beside SMS verification.
                # A QR generation failure is not terminal for the combined
                # browser flow; only the final CookieJar is authoritative.
                return
        data = _payload_data(payload)
        error_code = data.get("error_code", payload.get("error_code", 0))
        if str(error_code) not in {"0", "None"}:
            _LOGGER.warning(
                "douyin login path=%s outcome=qr_business_error error_code=%s",
                _QR_PATH,
                _safe_error_code(error_code),
            )
            self._mark_failed(session, generation, _SAFE_FAILURE)
            return
        try:
            qr_png = _decode_qr_png(data.get("qrcode") or data.get("qr_code"))
        except (TypeError, ValueError):
            _LOGGER.warning("douyin login path=%s outcome=invalid_qr_payload", _QR_PATH)
            self._mark_failed(session, generation, _SAFE_FAILURE)
            return
        expires_at = _qr_expiry(data, self._qr_ttl)
        with self._lock:
            if not self._is_current_locked(session, generation):
                return
            session.qr_png = qr_png
            session.status = "qr_ready"
            session.expires_at = expires_at
            session.startup_deadline = None
            session.error = None

    def _handle_check_payload(
        self,
        session: _LoginSession,
        generation: int,
        payload: Mapping[str, Any],
    ) -> None:
        with self._lock:
            if not self._is_current_locked(session, generation):
                return
            browser_mode = session.login_mode == "browser"
        data = _payload_data(payload)
        error_code = data.get("error_code", payload.get("error_code", 0))
        account_flow = (
            str(data.get("account_flow", payload.get("account_flow", "")))
            .strip()
            .lower()
        )
        if str(error_code) == "2046" or account_flow == "verify":
            _LOGGER.warning(
                "douyin login path=%s outcome=verification_required error_code=%s",
                _CHECK_QR_PATH,
                _safe_error_code(error_code),
            )
            with self._lock:
                if not self._is_current_locked(session, generation):
                    return
                if session.status != "verification_required":
                    session.expires_at = time.monotonic() + _VERIFICATION_TTL
                session.status = "verification_required"
                session.qr_png = None
                session.error = None
            return
        if str(error_code) not in {"0", "None"}:
            if browser_mode:
                # A QR substep may fail or expire while the same official
                # window continues through SMS. Keep the browser attempt alive.
                return
            _LOGGER.warning(
                "douyin login path=%s outcome=check_business_error error_code=%s",
                _CHECK_QR_PATH,
                _safe_error_code(error_code),
            )
            self._mark_failed(session, generation, _SAFE_FAILURE)
            return
        raw_status = data.get("status", payload.get("status"))
        status = str(raw_status).strip().lower()
        with self._lock:
            if not self._is_current_locked(session, generation):
                return
            if status in {"", "new", "waiting", "1"}:
                return
            if status in {"scanned", "2"}:
                session.status = "scanned"
            elif status in {"confirmed", "3"}:
                session.status = "confirmed"
                session.expires_at = time.monotonic() + _POST_CONFIRM_TTL
                session.qr_png = None
            elif status in {"expired", "4", "refused", "5"}:
                if not browser_mode:
                    session.status = "expired"
                    session.expires_at = None
                    session.qr_png = None
            elif status:
                _LOGGER.warning(
                    "douyin login path=%s outcome=unknown_check_status",
                    _CHECK_QR_PATH,
                )

    def _ensure_current(
        self,
        session: _LoginSession,
        generation: int,
        stop_event: threading.Event,
    ) -> None:
        if stop_event.is_set():
            raise _LoginCancelled
        with self._lock:
            if self._shutdown or not self._is_current_locked(session, generation):
                raise _LoginCancelled

    def _expire_if_needed_locked(self, session: _LoginSession) -> None:
        if (
            session.expires_at is not None
            and time.monotonic() >= session.expires_at
            and session.status
            in {
                "browser_ready",
                "qr_ready",
                "scanned",
                "confirmed",
                "verification_required",
            }
        ):
            session.status = "expired"
            session.expires_at = None
            session.qr_png = None
            session.error = _EXPIRED_FAILURE

    def _is_current_locked(self, session: _LoginSession, generation: int) -> bool:
        return not self._shutdown and session.generation == generation

    def _worker_finished(self, session: _LoginSession, generation: int) -> None:
        with self._lock:
            if (
                session.generation == generation
                and session.worker is threading.current_thread()
            ):
                session.worker = None
                session.startup_deadline = None

    def _mark_expired(self, session: _LoginSession, generation: int) -> None:
        with self._lock:
            if not self._is_current_locked(session, generation):
                return
            session.status = "expired"
            session.expires_at = None
            session.qr_png = None
            session.cookie = None
            session.error = session.error or _EXPIRED_FAILURE
            session.failure_kind = None

    def _mark_failed(
        self,
        session: _LoginSession,
        generation: int,
        message: str,
        *,
        failure_kind: str | None = None,
    ) -> None:
        with self._lock:
            if not self._is_current_locked(session, generation):
                return
            session.status = "failed"
            session.expires_at = None
            session.qr_png = None
            session.cookie = None
            session.startup_deadline = None
            session.error = message
            session.failure_kind = failure_kind

    def _public_payload_locked(self, session: _LoginSession) -> dict[str, Any]:
        remaining = 0
        if session.expires_at is not None:
            remaining = max(0, math.ceil(session.expires_at - time.monotonic()))
        payload = {
            "session_id": session.session_id,
            "status": session.status,
            "expires_in": remaining,
            "qr_available": session.qr_png is not None,
            "message": session.error
            or _STATUS_MESSAGES.get(session.status, _SAFE_FAILURE),
        }
        if session.failure_kind is not None:
            payload["failure_kind"] = session.failure_kind
        return payload

    def _require_session_locked(self, session_id: str) -> _LoginSession:
        if not isinstance(session_id, str) or not session_id:
            raise DouyinLoginError("登录会话不存在。")
        session = self._sessions.get(session_id)
        if session is None:
            raise DouyinLoginError("登录会话不存在。")
        return session

    @staticmethod
    def _join_worker(worker: threading.Thread | None) -> None:
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=5.0)


class _LoginCancelled(Exception):
    pass


class _LoginExpired(Exception):
    pass


class _LoginStartupTimeout(Exception):
    pass


def _default_playwright_factory() -> Any:
    return sync_playwright().start()


def _validate_favorites_smoke(payload: object) -> None:
    if not isinstance(payload, Mapping):
        raise DouyinLoginError("已取得登录凭据，但收藏接口返回了无法识别的结果。")
    status = payload.get("status_code")
    if str(status) in _AUTH_STATUS_CODES:
        raise DouyinAuthenticationError(
            "Douyin authentication failed",
            reason="business_rejected",
            status_code=str(status),
        )
    if status != 0:
        raise DouyinLoginError("已取得登录凭据，但收藏接口校验未通过。")
    items = payload.get("aweme_list")
    if not isinstance(items, list) or not any(
        isinstance(item, Mapping) and _numeric_aweme_id(item) is not None
        for item in items
    ):
        raise DouyinLoginError("已取得登录凭据，但收藏接口没有返回可验证的作品。")


def _payload_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def _log_official_response(
    response: Any, path: str, payload: Mapping[str, Any]
) -> None:
    raw_status = getattr(response, "status", None)
    status = str(raw_status) if isinstance(raw_status, int) else "unknown"
    data = _payload_data(payload)
    error_code = data.get("error_code", payload.get("error_code"))
    raw_headers = getattr(response, "headers", {})
    protected_headers: dict[str, int] = {}
    if isinstance(raw_headers, Mapping):
        for raw_name, raw_value in raw_headers.items():
            name = str(raw_name).lower()
            if not (
                name.startswith(("x-", "sec-"))
                or name in {"set-cookie", "cookie", "referer", "user-agent"}
            ):
                continue
            if len(name) > 100 or any(
                char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in name
            ):
                continue
            protected_headers[name] = (
                len(raw_value) if isinstance(raw_value, str) else 0
            )
    _LOGGER.warning(
        "douyin official response path=%s status=%s error_code=%s protected_headers=%s",
        path,
        status,
        _safe_error_code(error_code),
        protected_headers,
    )


def _safe_error_code(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        candidate = value.strip()
        if len(candidate) <= 16 and candidate.lstrip("-").isdigit():
            return candidate
    return "non_numeric"


def _qr_expiry(data: Mapping[str, Any], fallback_ttl: int) -> float:
    raw = data.get("expire_time")
    try:
        expiry = float(raw)
        if expiry > 10_000_000_000:
            expiry /= 1000
        remaining = expiry - time.time()
        if remaining > 0:
            return time.monotonic() + remaining
    except (TypeError, ValueError):
        pass
    return time.monotonic() + fallback_ttl


def _numeric_aweme_id(item: Mapping[str, Any]) -> str | None:
    value = item.get("aweme_id") or item.get("aweme_id_str")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value.isdigit() else None


def _decode_qr_png(value: object) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("QR data is missing")
    raw = value.strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("QR data is invalid") from error
    if not decoded.startswith(_PNG_SIGNATURE):
        raise ValueError("QR data is not a PNG")
    if len(decoded) > 2 * 1024 * 1024:
        raise ValueError("QR data is too large")
    return decoded


def _cookie_header(cookies: object) -> str:
    if not isinstance(cookies, list):
        return ""
    pairs: list[str] = []
    for cookie in cookies:
        if not isinstance(cookie, Mapping):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if not name or any(char in name + value for char in "\r\n"):
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _has_authenticated_cookie(cookies: object) -> bool:
    if not isinstance(cookies, list):
        return False
    return any(
        isinstance(cookie, Mapping)
        and cookie.get("name") in _AUTH_COOKIE_NAMES
        and isinstance(cookie.get("value"), str)
        and bool(cookie["value"])
        for cookie in cookies
    )


def _close_runtime(
    context: Any | None, browser: Any | None, runtime: Any | None
) -> None:
    for owner in (context, browser, runtime):
        if owner is None:
            continue
        close = getattr(owner, "close", None)
        stop = getattr(owner, "stop", None)
        action = close if callable(close) else stop if callable(stop) else None
        if action is not None:
            try:
                action()
            except Exception:
                continue
