from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from learnnest.adapters.douyin import DouyinAdapterError
from learnnest.adapters.douyin_http import (
    DouyinAuthenticationError,
    DouyinHttpRequestError,
)
from learnnest.adapters.douyin_official_page import DouyinOfficialPageError
from learnnest.douyin_favorites import (
    DouyinFavoritesError,
    DouyinFavoritesStore,
)
from learnnest.provider_profiles import connect, set_role_binding
from learnnest.web_app import (
    AutomationAuthorizeRequest,
    AutomationConfigureRequest,
    WebService,
    create_web_app,
)
from fastapi.testclient import TestClient


def _item(item_id: str, title: str, *, cover: str | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {"aweme_id": item_id, "desc": title}
    if cover is not None:
        raw["video"] = {"cover": {"url_list": [cover]}}
    return raw


class FakeTransport:
    def __init__(self, pages: list[Mapping[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[tuple[int, int]] = []

    def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]:
        self.calls.append((cursor, count))
        return self.pages[len(self.calls) - 1]


class FakeFolderTransport(FakeTransport):
    def __init__(self, pages: list[Mapping[str, Any]]) -> None:
        super().__init__(pages)
        self.folder_calls: list[tuple[int, int]] = []
        self.folder_item_calls: list[tuple[str, int, int]] = []

    def list_folders(self, *, cursor: int, count: int) -> Mapping[str, Any]:
        self.folder_calls.append((cursor, count))
        return {
            "status_code": 0,
            "collects_list": [
                {"collects_id_str": "10", "collects_name": "编程", "total_number": 2},
                {"collects_id_str": "20", "collects_name": "设计", "total_number": 1},
            ],
            "cursor": 0,
            "has_more": False,
        }

    def list_folder_items(
        self,
        folder_id: str,
        *,
        cursor: int,
        count: int,
    ) -> Mapping[str, Any]:
        self.folder_item_calls.append((folder_id, cursor, count))
        items = {
            "10": [_item("1", "默认与编程"), _item("3", "只在编程")],
            "20": [_item("3", "同时在设计")],
        }
        return {
            "status_code": 0,
            "aweme_list": items[folder_id],
            "cursor": 0,
            "has_more": False,
        }


class FakeResponse:
    status = 200

    def __init__(self, data: bytes, content_type: str = "image/jpeg") -> None:
        self.data = data
        self.headers = {"Content-Type": content_type}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.data


class FakeThumbnailOpener:
    def __init__(self, responses: Mapping[str, bytes | Exception]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def __call__(self, request: Any, *, timeout: float) -> FakeResponse:
        assert timeout == 20.0
        self.urls.append(request.full_url)
        result = self.responses[request.full_url]
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


def _store(
    tmp_path: Path,
    transport: FakeTransport,
    opener: FakeThumbnailOpener | None = None,
) -> DouyinFavoritesStore:
    return DouyinFavoritesStore(
        tmp_path,
        transport_factory=lambda _cookie: transport,
        thumbnail_opener=opener,
        page_size=10,
    )


class FakeLogin:
    browser_state = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://www.douyin.com",
                "localStorage": [{"name": "device-state", "value": "sentinel"}],
            }
        ],
    }

    def cookie_for(self, _session_id: str) -> SecretStr:
        return SecretStr("fake-cookie")

    def browser_storage_state_for(self, _session_id: str) -> Mapping[str, Any]:
        return self.browser_state

    def invalidate(self, _session_id: str) -> None:
        return None

    def shutdown(self) -> None:
        return None


def _authorized_service(
    root: Path, store: DouyinFavoritesStore, *, auto: bool
) -> WebService:
    connect(root, name="note", preset="mimo", secret_value="fake-key")
    set_role_binding(root, role="note_writer", connection_name="note")
    set_role_binding(root, role="note_reviewer", connection_name="note")
    service = WebService(root, douyin_login=FakeLogin(), douyin_favorites=store)  # type: ignore[arg-type]
    service.configure_automation(
        AutomationConfigureRequest(
            default_output="complete_note",
            auto_organize_new_favorites=auto,
            check_interval_minutes=5,
            max_items_per_tick=1,
        )
    )
    service.authorize_automation(AutomationAuthorizeRequest(confirm_paid=True))
    return service


def test_first_favorite_sync_is_a_baseline_but_later_new_items_are_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(
        tmp_path,
        FakeTransport(
            [
                {
                    "status_code": 0,
                    "aweme_list": [_item("1", "历史")],
                    "has_more": False,
                },
                {
                    "status_code": 0,
                    "aweme_list": [_item("2", "新增"), _item("1", "历史")],
                    "has_more": False,
                },
            ]
        ),
    )
    service = _authorized_service(tmp_path, store, auto=True)
    queued: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        service,
        "submit_process",
        lambda source, *, source_kind=None: queued.append((source, source_kind)),
    )

    service.sync_douyin_favorites("session")
    service.sync_douyin_favorites("session")

    assert queued == [("https://www.douyin.com/video/2", "douyin_favorite")]


def test_web_sync_passes_complete_browser_state_to_favorites_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = FakeTransport(
        [
            {
                "status_code": 0,
                "aweme_list": [_item("1", "收藏")],
                "has_more": False,
            }
        ]
    )
    store = _store(tmp_path, transport)
    login = FakeLogin()
    service = WebService(
        tmp_path,
        douyin_login=login,  # type: ignore[arg-type]
        douyin_favorites=store,
    )
    observed_state: list[Mapping[str, Any] | None] = []
    original_sync = store.sync

    def capture_sync(
        cookie: SecretStr,
        *,
        browser_storage_state: Mapping[str, Any] | None = None,
        on_authentication_failure: Any = None,
    ) -> Any:
        observed_state.append(browser_storage_state)
        return original_sync(
            cookie,
            browser_storage_state=browser_storage_state,
            on_authentication_failure=on_authentication_failure,
        )

    monkeypatch.setattr(store, "sync", capture_sync)

    service.sync_douyin_favorites("session")

    assert observed_state == [login.browser_state]


def test_favorite_sync_with_auto_disabled_never_queues_new_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(
        tmp_path,
        FakeTransport(
            [
                {
                    "status_code": 0,
                    "aweme_list": [_item("1", "历史")],
                    "has_more": False,
                },
                {
                    "status_code": 0,
                    "aweme_list": [_item("2", "新增")],
                    "has_more": False,
                },
            ]
        ),
    )
    service = _authorized_service(tmp_path, store, auto=False)
    queued: list[object] = []
    monkeypatch.setattr(
        service, "submit_process", lambda *_args, **_kwargs: queued.append(True)
    )

    service.sync_douyin_favorites("session")
    service.sync_douyin_favorites("session")

    assert queued == []


def test_syncs_paginated_favorites_dedupes_and_persists_local_thumbnails(
    tmp_path: Path,
) -> None:
    cover_one = "https://cdn.example/1.jpg?x=signature-one"
    cover_two = "https://cdn.example/2.jpg?x=signature-two"
    cover_three = "https://cdn.example/3.jpg?x=signature-three"
    transport = FakeTransport(
        [
            {
                "status_code": 0,
                "aweme_list": [
                    _item("1", "第一条", cover=cover_one),
                    _item("2", "第二条", cover=cover_two),
                ],
                "cursor": 10,
                "has_more": True,
            },
            {
                "status_code": 0,
                "aweme_list": [
                    _item("2", "第二条重复", cover=cover_two),
                    _item("3", "第三条", cover=cover_three),
                ],
                "cursor": 20,
                "has_more": False,
            },
        ]
    )
    opener = FakeThumbnailOpener(
        {
            cover_one: b"jpeg-one",
            cover_two: b"jpeg-two",
            cover_three: b"jpeg-three",
        }
    )
    store = _store(tmp_path, transport, opener)

    snapshot = store.sync(SecretStr("session-cookie"))

    assert [item.aweme_id for item in snapshot.items] == ["1", "2", "3"]
    assert [item.title for item in snapshot.items] == ["第一条", "第二条", "第三条"]
    assert transport.calls == [(0, 10), (10, 10)]
    assert len(opener.urls) == 3
    facts = store.facts_path.read_text(encoding="utf-8")
    assert "signature-one" not in facts
    assert "https://cdn.example" not in facts
    assert "session-cookie" not in facts
    assert json.loads(facts)["items"][0].keys() == {
        "aweme_id",
        "folder_ids",
        "title",
        "url",
        "synced_at",
        "thumbnail_path",
    }
    expected_images = {"1": b"jpeg-one", "2": b"jpeg-two", "3": b"jpeg-three"}
    for item in snapshot.items:
        assert item.thumbnail_path is not None
        assert (
            store.thumbnail_file(item.thumbnail_path).read_bytes()
            == expected_images[item.aweme_id]
        )


def test_sync_projects_default_and_custom_folders_without_duplicate_cards(
    tmp_path: Path,
) -> None:
    transport = FakeFolderTransport(
        [
            {
                "status_code": 0,
                "aweme_list": [_item("1", "默认与编程"), _item("2", "只在默认")],
                "cursor": 0,
                "has_more": False,
            }
        ]
    )
    store = _store(tmp_path, transport)

    snapshot = store.sync(SecretStr("session-cookie"))

    assert [
        (folder.folder_id, folder.name, folder.item_count)
        for folder in snapshot.folders
    ] == [
        ("default", "默认收藏夹", 2),
        ("10", "编程", 2),
        ("20", "设计", 1),
    ]
    assert [(item.aweme_id, item.folder_ids) for item in snapshot.items] == [
        ("1", ("default", "10")),
        ("2", ("default",)),
        ("3", ("10", "20")),
    ]
    assert transport.folder_calls == [(0, 10)]
    assert transport.folder_item_calls == [("10", 0, 10), ("20", 0, 10)]
    facts = json.loads(store.facts_path.read_text(encoding="utf-8"))
    assert [folder["folder_id"] for folder in facts["folders"]] == [
        "default",
        "10",
        "20",
    ]
    assert "session-cookie" not in store.facts_path.read_text(encoding="utf-8")


def test_thumbnail_failure_keeps_the_favorite_with_placeholder_path(
    tmp_path: Path,
) -> None:
    failed_cover = "https://cdn.example/fail.jpg?sig=hidden"
    transport = FakeTransport(
        [
            {
                "status_code": 0,
                "aweme_list": [
                    _item("1", "能显示", cover="https://cdn.example/ok.jpg"),
                    _item("2", "封面失败", cover=failed_cover),
                    _item("3", "没有封面"),
                ],
                "has_more": False,
            }
        ]
    )
    opener = FakeThumbnailOpener(
        {
            "https://cdn.example/ok.jpg": b"ok",
            failed_cover: OSError("remote signature should not escape"),
        }
    )

    snapshot = _store(tmp_path, transport, opener).sync(SecretStr("cookie"))

    assert len(snapshot.items) == 3
    assert snapshot.items[0].thumbnail_path is not None
    assert snapshot.items[1].thumbnail_path is None
    assert snapshot.items[2].thumbnail_path is None
    assert "sig=hidden" not in (
        tmp_path / ".learnnest" / "douyin" / "favorites.json"
    ).read_text(encoding="utf-8")


def test_non_json_transport_failure_is_safe() -> None:
    class NonJsonTransport:
        def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]:
            del cursor, count
            raise DouyinHttpRequestError(
                "Douyin API returned a non-JSON response",
                reason="invalid_response",
            )

    store = DouyinFavoritesStore(
        Path("artifacts/local/douyin-test"),
        transport_factory=lambda _cookie: NonJsonTransport(),
    )
    with pytest.raises(DouyinFavoritesError, match="非 JSON 或非对象响应"):
        store.sync(SecretStr("COOKIE" + "_SENTINEL"))


def test_official_runtime_change_is_specific() -> None:
    class ChangedRuntimeTransport:
        def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]:
            del cursor, count
            raise DouyinOfficialPageError(
                "runtime detail must not escape",
                reason="runtime_unavailable",
            )

    store = DouyinFavoritesStore(
        Path("artifacts/local/douyin-test"),
        transport_factory=lambda _cookie: ChangedRuntimeTransport(),
    )

    with pytest.raises(DouyinFavoritesError, match="请求组件已变化"):
        store.sync(SecretStr("cookie"))


def test_official_page_pagination_stall_is_specific_and_keeps_snapshot(
    tmp_path: Path,
) -> None:
    facts = tmp_path / ".learnnest" / "douyin" / "favorites.json"
    facts.parent.mkdir(parents=True)
    facts.write_text(
        json.dumps(
            {
                "synced_at": "2026-08-11T00:00:00Z",
                "items": [
                    {
                        "aweme_id": "1",
                        "title": "旧收藏",
                        "url": "https://www.douyin.com/video/1",
                        "synced_at": "2026-08-11T00:00:00Z",
                        "thumbnail_path": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class StalledTransport:
        def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]:
            del cursor, count
            raise DouyinOfficialPageError(
                "runtime detail must not escape",
                reason="pagination_stalled",
            )

    store = DouyinFavoritesStore(
        tmp_path,
        transport_factory=lambda _cookie: StalledTransport(),
    )

    with pytest.raises(DouyinFavoritesError, match="已返回首批收藏"):
        store.sync(SecretStr("cookie"))

    snapshot = store.read_snapshot()
    assert snapshot.items[0].title == "旧收藏"
    assert snapshot.items[0].folder_ids == ("default",)
    assert [(folder.folder_id, folder.item_count) for folder in snapshot.folders] == [
        ("default", 1)
    ]


def test_direct_pagination_rejects_a_cursor_that_does_not_advance(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        [
            {
                "status_code": 0,
                "aweme_list": [_item("1", "第一页")],
                "cursor": 0,
                "has_more": True,
            }
        ]
    )

    with pytest.raises(DouyinFavoritesError, match="游标没有前进"):
        _store(tmp_path, transport).sync(SecretStr("cookie"))

    assert not (tmp_path / ".learnnest" / "douyin" / "favorites.json").exists()


def test_direct_pagination_reports_the_safety_page_limit(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            {
                "status_code": 0,
                "aweme_list": [_item("1", "第一页")],
                "cursor": 10,
                "has_more": True,
            }
        ]
    )
    store = DouyinFavoritesStore(
        tmp_path,
        transport_factory=lambda _cookie: transport,
        thumbnail_opener=FakeThumbnailOpener({}),
        max_pages=1,
    )

    with pytest.raises(DouyinFavoritesError, match="安全分页上限（1 页）"):
        store.sync(SecretStr("cookie"))


def test_favorites_require_success_status_and_numeric_aweme_id(tmp_path: Path) -> None:
    invalid_payloads = [
        {"status_code": 999, "aweme_list": [_item("1", "业务失败")]},
        {"status_code": 0, "aweme_list": []},
        {"status_code": 0, "aweme_list": [{"aweme_id": "not-a-number"}]},
    ]

    for index, payload in enumerate(invalid_payloads):
        store = _store(tmp_path / str(index), FakeTransport([payload]))
        with pytest.raises(DouyinFavoritesError):
            store.sync(SecretStr("cookie"))


def test_authentication_failure_invokes_reconnect_callback(tmp_path: Path) -> None:
    class AuthTransport:
        def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]:
            del cursor, count
            raise DouyinAuthenticationError("authentication failed")

    reconnected = []
    store = DouyinFavoritesStore(
        tmp_path,
        transport_factory=lambda _cookie: AuthTransport(),
    )

    with pytest.raises(DouyinAuthenticationError):
        store.sync(
            SecretStr("COOKIE" + "_SENTINEL"),
            on_authentication_failure=lambda: reconnected.append(True),
        )

    assert reconnected == [True]


def test_thumbnail_endpoint_boundary_rejects_path_traversal_and_unlisted_files(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        [
            {
                "status_code": 0,
                "aweme_list": [_item("1", "一", cover="https://cdn.example/1.jpg")],
            }
        ]
    )
    store = _store(
        tmp_path,
        transport,
        FakeThumbnailOpener({"https://cdn.example/1.jpg": b"image"}),
    )
    snapshot = store.sync(SecretStr("cookie"))
    assert snapshot.items[0].thumbnail_path == "thumbnails/1.jpg"

    with pytest.raises(FileNotFoundError):
        store.thumbnail_file("thumbnails/../favorites.json")
    with pytest.raises(FileNotFoundError):
        store.thumbnail_file("thumbnails/not-declared.jpg")

    outside = store.directory / "secret.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"outside")
    store.facts_path.write_text(
        json.dumps(
            {
                "synced_at": "now",
                "items": [
                    {
                        "aweme_id": "9",
                        "title": "恶意",
                        "url": "https://www.douyin.com/video/9",
                        "synced_at": "now",
                        "thumbnail_path": "thumbnails/../secret.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError):
        store.thumbnail_file("thumbnails/../secret.png")


def test_new_store_reads_only_last_safe_snapshot(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            {
                "status_code": 0,
                "aweme_list": [
                    _item("1", "持久收藏", cover="https://cdn.example/1.jpg")
                ],
            }
        ]
    )
    first = _store(
        tmp_path,
        transport,
        FakeThumbnailOpener({"https://cdn.example/1.jpg": b"image"}),
    )
    first.sync(SecretStr("cookie"))

    restarted = DouyinFavoritesStore(tmp_path)
    snapshot = restarted.read_snapshot()

    assert [item.title for item in snapshot.items] == ["持久收藏"]
    assert snapshot.items[0].url == "https://www.douyin.com/video/1"
    assert restarted.thumbnail_file("thumbnails/1.jpg").read_bytes() == b"image"


class FakeLoginBoundary:
    def __init__(self, cookie: str = "session-cookie") -> None:
        self.cookie = SecretStr(cookie)
        self.status = "connected"
        self.shutdown_called = False
        self.invalidated = False

    def create_session(self) -> dict[str, Any]:
        self.status = "qr_ready"
        return self.get_session("session1234567890")

    def create_browser_session(self) -> dict[str, Any]:
        self.status = "browser_ready"
        return self.get_session("session1234567890")

    def current_session(self) -> dict[str, Any]:
        if self.status != "connected":
            return {
                "session_id": None,
                "status": "disconnected",
                "expires_in": 0,
                "qr_available": False,
            }
        return {
            "session_id": "session1234567890",
            "status": "connected",
            "expires_in": 0,
            "qr_available": False,
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        assert session_id == "session1234567890"
        return {
            "session_id": session_id,
            "status": self.status,
            "expires_in": 0 if self.status == "connected" else 60,
            "qr_available": self.status == "qr_ready",
        }

    def refresh_session(self, session_id: str) -> dict[str, Any]:
        return self.get_session(session_id)

    def cancel_session(self, session_id: str) -> dict[str, Any]:
        self.status = "cancelled"
        return self.get_session(session_id)

    def qr_image(self, session_id: str) -> bytes:
        assert session_id == "session1234567890"
        return _PNG

    def cookie_for(self, session_id: str) -> SecretStr:
        assert session_id == "session1234567890"
        if self.status != "connected":
            raise RuntimeError("not connected")
        return self.cookie

    def invalidate(self, session_id: str) -> None:
        assert session_id == "session1234567890"
        self.status = "failed"
        self.invalidated = True

    def shutdown(self) -> None:
        self.shutdown_called = True


_PNG = b"\x89PNG\r\n\x1a\nlocal"


def test_douyin_web_apis_return_safe_login_state_and_shutdown(tmp_path: Path) -> None:
    login = FakeLoginBoundary()
    app = create_web_app(tmp_path, douyin_login=login)

    with TestClient(app) as client:
        current = client.get("/api/douyin/login/current")
        browser = client.post("/api/douyin/login/browser")
        browser_state = client.get("/api/douyin/login/session1234567890")
        browser_cancelled = client.delete("/api/douyin/login/session1234567890")
        created = client.post("/api/douyin/login/qr")
        state = client.get("/api/douyin/login/qr/session1234567890")
        image = client.get("/api/douyin/login/qr/session1234567890/image")
        cancelled = client.delete("/api/douyin/login/qr/session1234567890")

    assert current.json()["status"] == "connected"
    assert browser.status_code == 201
    assert browser_state.json()["status"] == "browser_ready"
    assert browser_state.json()["qr_available"] is False
    assert browser_cancelled.json()["status"] == "cancelled"
    assert created.status_code == 201
    assert state.json().keys() == {
        "session_id",
        "status",
        "expires_in",
        "qr_available",
    }
    assert all(
        secret not in state.text.lower() for secret in ("cookie", "token", "passport")
    )
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")
    assert image.content == _PNG
    assert cancelled.json()["status"] == "cancelled"
    assert login.shutdown_called is True


def test_douyin_favorites_web_sync_thumbnail_and_restart_snapshot(
    tmp_path: Path,
) -> None:
    cover = "https://cdn.example/1.jpg?signature=never-persist"
    transport = FakeTransport(
        [{"status_code": 0, "aweme_list": [_item("1", "收藏标题", cover=cover)]}]
    )
    opener = FakeThumbnailOpener({cover: b"local-image"})
    store = _store(tmp_path, transport, opener)
    login = FakeLoginBoundary()
    app = create_web_app(tmp_path, douyin_login=login, douyin_favorites=store)

    with TestClient(app) as client:
        synced = client.post(
            "/api/douyin/favorites",
            json={"session_id": "session1234567890"},
        )
        listing = client.get("/api/douyin/favorites")
        thumbnail = client.get("/api/douyin/favorites/thumbnails/thumbnails/1.jpg")
        traversal = client.get(
            "/api/douyin/favorites/thumbnails/thumbnails/../favorites.json"
        )

    assert synced.status_code == 200
    assert synced.json()["items"][0]["title"] == "收藏标题"
    assert "signature=never-persist" not in synced.text
    assert listing.json() == synced.json()
    assert thumbnail.status_code == 200
    assert thumbnail.content == b"local-image"
    assert traversal.status_code == 404

    restarted = create_web_app(tmp_path, douyin_login=FakeLoginBoundary())
    with TestClient(restarted) as client:
        readonly = client.get("/api/douyin/favorites")
    assert readonly.json()["items"][0]["url"] == "https://www.douyin.com/video/1"


def test_douyin_favorites_web_auth_failure_requires_reconnect(tmp_path: Path) -> None:
    class AuthTransport:
        def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]:
            del cursor, count
            raise DouyinAuthenticationError("authentication failed")

    login = FakeLoginBoundary()
    store = DouyinFavoritesStore(
        tmp_path,
        transport_factory=lambda _cookie: AuthTransport(),
    )
    app = create_web_app(tmp_path, douyin_login=login, douyin_favorites=store)

    with TestClient(app) as client:
        response = client.post(
            "/api/douyin/favorites",
            json={"session_id": "session1234567890"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "登录已失效，请重新连接抖音。"
    assert login.invalidated is True


def test_douyin_favorites_web_request_rejection_keeps_login_and_explains_drift(
    tmp_path: Path,
) -> None:
    class RejectedTransport:
        def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]:
            del cursor, count
            raise DouyinAuthenticationError(
                "Douyin API request returned HTTP 403",
                reason="request_rejected",
                status_code=403,
            )

    login = FakeLoginBoundary()
    store = DouyinFavoritesStore(
        tmp_path,
        transport_factory=lambda _cookie: RejectedTransport(),
    )
    app = create_web_app(tmp_path, douyin_login=login, douyin_favorites=store)

    with TestClient(app) as client:
        response = client.post(
            "/api/douyin/favorites",
            json={"session_id": "session1234567890"},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "登录凭据已取得，但收藏请求被抖音拦截（HTTP 403）；"
        "当前网页接口校验已变化，不是扫码或验证码失败。"
    )
    assert login.invalidated is False


def test_douyin_favorites_web_explains_missing_official_page_response(
    tmp_path: Path,
) -> None:
    class MissingResponseTransport:
        def list_video_favorites(self, *, cursor: int, count: int) -> Mapping[str, Any]:
            del cursor, count
            raise DouyinAdapterError("runtime detail must not escape")

    store = DouyinFavoritesStore(
        tmp_path,
        transport_factory=lambda _cookie: MissingResponseTransport(),
    )
    app = create_web_app(
        tmp_path,
        douyin_login=FakeLoginBoundary(),
        douyin_favorites=store,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/douyin/favorites",
            json={"session_id": "session1234567890"},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "抖音官方页面没有返回可验证的收藏结果；收藏未更新，请稍后重试。"
    )
    assert "runtime detail" not in response.text
