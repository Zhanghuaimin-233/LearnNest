from pathlib import Path

import pytest
from pydantic import SecretStr

from learnnest.douyin_cookie_store import (
    DouyinCookieStore,
    DouyinCookieStoreError,
)


def _protect(value: bytes) -> bytes:
    return b"protected:" + value[::-1]


def _unprotect(value: bytes) -> bytes:
    if not value.startswith(b"protected:"):
        raise ValueError("invalid protected payload")
    return value.removeprefix(b"protected:")[::-1]


def test_cookie_store_persists_only_protected_bytes_and_clears(tmp_path: Path) -> None:
    sentinel = "sessionid=COOKIE" + "_PERSISTENCE_SENTINEL"
    store = DouyinCookieStore(
        tmp_path,
        protect=_protect,
        unprotect=_unprotect,
    )

    store.save(SecretStr(sentinel))

    raw = store.path.read_bytes()
    assert sentinel.encode() not in raw
    assert store.path == tmp_path / ".learnnest" / "douyin" / "session.dpapi"
    assert not list(store.path.parent.glob("*.tmp"))
    assert store.load() == SecretStr(sentinel)

    store.clear()
    assert store.load() is None


def test_cookie_store_encrypts_cookie_and_browser_storage_state_together(
    tmp_path: Path,
) -> None:
    cookie = SecretStr("sessionid=SESSION" + "_COOKIE_SENTINEL")
    browser_state = SecretStr(
        '{"cookies":[{"name":"sessionid","value":"STATE_SENTINEL"}],'
        '"origins":[{"origin":"https://www.douyin.com","localStorage":[]}]}'
    )
    store = DouyinCookieStore(
        tmp_path,
        protect=_protect,
        unprotect=_unprotect,
    )

    store.save_session(cookie, browser_state)

    raw = store.path.read_bytes()
    assert b"COOKIE_SENTINEL" not in raw
    assert b"STATE_SENTINEL" not in raw
    loaded = store.load_session()
    assert loaded is not None
    assert loaded.cookie == cookie
    assert loaded.browser_state == browser_state
    assert store.load() == cookie


def test_cookie_store_rejects_corrupt_or_unsafe_plaintext(tmp_path: Path) -> None:
    store = DouyinCookieStore(
        tmp_path,
        protect=_protect,
        unprotect=_unprotect,
    )
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b"not-a-supported-store")

    with pytest.raises(DouyinCookieStoreError, match="格式无效"):
        store.load()
    with pytest.raises(DouyinCookieStoreError, match="格式无效"):
        store.save(SecretStr("sessionid=value\r\nInjected=true"))


def test_cookie_store_hides_credential_from_encryption_failure(tmp_path: Path) -> None:
    sentinel = "sessionid=FAILURE" + "_SENTINEL"

    def fail_with_plaintext(value: bytes) -> bytes:
        raise RuntimeError(value.decode("utf-8"))

    store = DouyinCookieStore(tmp_path, protect=fail_with_plaintext)

    with pytest.raises(DouyinCookieStoreError) as error:
        store.save(SecretStr(sentinel))

    assert sentinel not in str(error.value)


def test_cookie_store_round_trips_with_windows_current_user_dpapi(
    tmp_path: Path,
) -> None:
    sentinel = "sessionid=WINDOWS" + "_DPAPI_SENTINEL"
    store = DouyinCookieStore(tmp_path)

    store.save(SecretStr(sentinel))

    assert sentinel.encode() not in store.path.read_bytes()
    assert store.load() == SecretStr(sentinel)
    store.clear()
