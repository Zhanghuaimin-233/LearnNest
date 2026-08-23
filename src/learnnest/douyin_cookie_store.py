"""Windows user-scoped encrypted storage for the Douyin WebUI session."""

from __future__ import annotations

import ctypes
import json
import os
import uuid
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

_FILE_MAGIC = b"LEARNNEST_DOUYIN_DPAPI_V1\0"
_PLAINTEXT_MAGIC = b"LEARNNEST_DOUYIN_COOKIE_V1\0"
_SESSION_PLAINTEXT_MAGIC = b"LEARNNEST_DOUYIN_SESSION_V2\0"
_DPAPI_ENTROPY = b"LearnNest-Douyin-Cookie-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_MAX_BROWSER_STATE_BYTES = 8 * 1024 * 1024


class DouyinCookieStoreError(RuntimeError):
    """A safe local credential-store failure."""


@dataclass(frozen=True)
class DouyinStoredSession:
    """One encrypted login identity and its isolated browser storage state."""

    cookie: SecretStr
    browser_state: SecretStr | None = None


class DouyinCookieStore:
    """Persist one official-page session encrypted for the Windows user."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self.path = (
            Path(output_root).resolve() / ".learnnest" / "douyin" / "session.dpapi"
        )
        self._protect = protect or _protect_windows
        self._unprotect = unprotect or _unprotect_windows

    def load(self) -> SecretStr | None:
        session = self.load_session()
        return session.cookie if session is not None else None

    def load_session(self) -> DouyinStoredSession | None:
        try:
            payload = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise DouyinCookieStoreError("本地抖音登录状态无法读取。") from None
        if not payload.startswith(_FILE_MAGIC):
            raise DouyinCookieStoreError("本地抖音登录状态格式无效。")
        try:
            plaintext = self._unprotect(payload[len(_FILE_MAGIC) :])
        except Exception:
            raise DouyinCookieStoreError("本地抖音登录状态无法解密。") from None
        if plaintext.startswith(_PLAINTEXT_MAGIC):
            try:
                cookie = plaintext[len(_PLAINTEXT_MAGIC) :].decode("utf-8")
            except UnicodeDecodeError:
                raise DouyinCookieStoreError("本地抖音登录状态格式无效。") from None
            _validate_cookie(cookie)
            return DouyinStoredSession(cookie=SecretStr(cookie))
        if not plaintext.startswith(_SESSION_PLAINTEXT_MAGIC):
            raise DouyinCookieStoreError("本地抖音登录状态格式无效。")
        try:
            decoded = plaintext[len(_SESSION_PLAINTEXT_MAGIC) :].decode("utf-8")
            session = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DouyinCookieStoreError("本地抖音登录状态格式无效。") from None
        if not isinstance(session, dict):
            raise DouyinCookieStoreError("本地抖音登录状态格式无效。")
        cookie = session.get("cookie")
        browser_state = session.get("browser_state")
        if not isinstance(cookie, str) or not isinstance(browser_state, str):
            raise DouyinCookieStoreError("本地抖音登录状态格式无效。")
        _validate_cookie(cookie)
        _validate_browser_state(browser_state)
        return DouyinStoredSession(
            cookie=SecretStr(cookie),
            browser_state=SecretStr(browser_state),
        )

    def save(self, cookie: SecretStr) -> None:
        value = cookie.get_secret_value()
        _validate_cookie(value)
        self._save_plaintext(_PLAINTEXT_MAGIC + value.encode("utf-8"))

    def save_session(self, cookie: SecretStr, browser_state: SecretStr) -> None:
        cookie_value = cookie.get_secret_value()
        state_value = browser_state.get_secret_value()
        _validate_cookie(cookie_value)
        _validate_browser_state(state_value)
        encoded = json.dumps(
            {"cookie": cookie_value, "browser_state": state_value},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._save_plaintext(_SESSION_PLAINTEXT_MAGIC + encoded)

    def _save_plaintext(self, plaintext: bytes) -> None:
        try:
            protected = self._protect(plaintext)
        except Exception:
            raise DouyinCookieStoreError("本地抖音登录状态无法加密。") from None
        if not protected:
            raise DouyinCookieStoreError("本地抖音登录状态无法加密。")
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(_FILE_MAGIC + protected)
            os.replace(temporary, self.path)
        except OSError:
            raise DouyinCookieStoreError("本地抖音登录状态无法保存。") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise DouyinCookieStoreError("本地抖音登录状态无法清除。") from None


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value)
    return (
        _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        ),
        buffer,
    )


def _windows_libraries() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    if os.name != "nt":
        raise DouyinCookieStoreError("Windows 用户加密在当前系统不可用。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _protect_windows(plaintext: bytes) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    plaintext_blob, plaintext_buffer = _blob(plaintext)
    entropy_blob, entropy_buffer = _blob(_DPAPI_ENTROPY)
    protected_blob = _DataBlob()
    success = crypt32.CryptProtectData(
        ctypes.byref(plaintext_blob),
        "LearnNest Douyin Cookie",
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(protected_blob),
    )
    del plaintext_buffer, entropy_buffer
    if not success:
        raise DouyinCookieStoreError("Windows 用户加密失败。")
    try:
        return ctypes.string_at(protected_blob.pbData, protected_blob.cbData)
    finally:
        kernel32.LocalFree(protected_blob.pbData)


def _unprotect_windows(protected: bytes) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    protected_blob, protected_buffer = _blob(protected)
    entropy_blob, entropy_buffer = _blob(_DPAPI_ENTROPY)
    plaintext_blob = _DataBlob()
    success = crypt32.CryptUnprotectData(
        ctypes.byref(protected_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(plaintext_blob),
    )
    del protected_buffer, entropy_buffer
    if not success:
        raise DouyinCookieStoreError("Windows 用户解密失败。")
    try:
        return ctypes.string_at(plaintext_blob.pbData, plaintext_blob.cbData)
    finally:
        kernel32.LocalFree(plaintext_blob.pbData)


def _validate_cookie(value: str) -> None:
    if not value or any(character in value for character in "\r\n"):
        raise DouyinCookieStoreError("本地抖音登录状态格式无效。")


def _validate_browser_state(value: str) -> None:
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > _MAX_BROWSER_STATE_BYTES:
        raise DouyinCookieStoreError("本地抖音登录状态格式无效。")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        raise DouyinCookieStoreError("本地抖音登录状态格式无效。") from None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("cookies"), list)
        or not isinstance(payload.get("origins"), list)
    ):
        raise DouyinCookieStoreError("本地抖音登录状态格式无效。")
