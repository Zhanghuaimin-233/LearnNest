"""Real loopback Edge browser coverage for the WebUI product loop."""

from __future__ import annotations

import json
import socket
import threading
import time
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterator

import pytest
import uvicorn
from playwright.sync_api import Browser, Page, expect, sync_playwright
from pydantic import SecretStr

from learnnest.automation_coordinator import AutomationCoordinator
from learnnest.automation_models import AutomationIntake
from learnnest.automation_runner import AutomationProviders
from learnnest.automation_runner import run_automation_tasks
from learnnest.automation_store import (
    create_intake,
    load_intake,
    load_status,
    load_task_state,
)
from learnnest.douyin_favorites import (
    DouyinFavorite,
    DouyinFavoritesError,
    DouyinFavoritesFolder,
    DouyinFavoritesSnapshot,
)
from learnnest.execution_models import FailureInfo, TaskAttempt
from learnnest.models import ContentPack, Evidence, StageStatus
from learnnest.pipeline import PipelineError
from learnnest.provider_model_catalog import ProviderModelCatalogError
from learnnest.provider_profiles import load_settings
from learnnest.task_store import (
    complete_task_goal,
    create_task,
    find_task_by_id,
    load_task,
    write_task_atomic,
)
from learnnest.task_control import load_task_control
from learnnest.web_app import create_web_app


class _OfflineWriter:
    name = "xiaomi-mimo"
    model = "mimo-v2.5"
    endpoint_identity = "https://api.xiaomimimo.com/v1"

    def write_markdown(self, _dossier: str) -> str:
        return "# 离线浏览器笔记\n\n这是可安全打开的完整笔记。"

    def review_markdown(self, dossier: str, candidate: str) -> str:
        source_fingerprint = json.loads(dossier)["task"]["source_fingerprint"]
        task_id = f"20260813-goal4{source_fingerprint.rsplit('-', 1)[1]}"
        return candidate.replace(
            "# 离线浏览器笔记",
            f"<!-- learnnest-task-id: {task_id} -->\n# 离线浏览器笔记",
        )


class _UnusedAudio:
    name = "unused"
    model = "unused"
    voice = "unused"


class _OfflinePodcast:
    name = "deepseek"
    model = "deepseek-v4-pro"
    endpoint_identity = "https://api.deepseek.com/v1"

    def generate(self, context: str, _images: tuple[object, ...]) -> str:
        payload = json.loads(context)
        pack = payload["content_pack"]
        evidence_id = pack["evidence"][0]["id"]
        return json.dumps(
            {
                "schema_version": "1.0",
                "task_id": pack["task_id"],
                "source_fingerprint": pack["source_fingerprint"],
                "note_content_sha256": payload["note_content_sha256"],
                "title": "离线浏览器播客",
                "segments": [
                    {
                        "order": 1,
                        "kind": "intro",
                        "text": "欢迎收听离线浏览器播客",
                        "evidence_ids": [evidence_id],
                    },
                    {
                        "order": 2,
                        "kind": "body",
                        "text": "这里是已经验证的浏览器材料",
                        "evidence_ids": [evidence_id],
                    },
                    {
                        "order": 3,
                        "kind": "outro",
                        "text": "感谢收听",
                        "evidence_ids": [evidence_id],
                    },
                ],
                "ai_supplements": [],
            },
            ensure_ascii=False,
        )


class _OfflineTts:
    name = "windows-tts"
    model = "system-speech"
    voice = "Test"
    billing = "local"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def synthesize(self, _speech_text: str, _style_instruction: str) -> bytes:
        if self.fail:
            raise RuntimeError("offline audio failure")
        stream = BytesIO()
        with wave.open(stream, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\0\0" * 1_600)
        return stream.getvalue()


def _offline_providers(
    _root: Path,
    _task_id: str,
    *,
    with_audio: bool = False,
    audio_failure: bool = False,
) -> AutomationProviders:
    writer = _OfflineWriter()
    return AutomationProviders(
        writer=writer,
        reviewer=writer,
        podcast=_OfflinePodcast() if with_audio else _UnusedAudio(),
        tts=_OfflineTts(fail=audio_failure) if with_audio else _UnusedAudio(),
    )


def _run_offline_tasks(
    root: Path,
    task_ids: tuple[str, ...],
    *,
    default_outputs: dict[str, str],
    now: datetime,
):
    selected_audio = any(
        output == "complete_note_with_audio" for output in default_outputs.values()
    )
    return run_automation_tasks(
        root,
        task_ids,
        _offline_providers(root, "", with_audio=selected_audio),
        default_outputs=default_outputs,
        now=now,
    )  # type: ignore[arg-type]


class _HistoricalFavorites:
    def read_snapshot(self) -> DouyinFavoritesSnapshot:
        return DouyinFavoritesSnapshot(
            synced_at="2026-08-13T00:00:00+00:00",
            items=(
                DouyinFavorite(
                    aweme_id="1234567890123456789",
                    title="历史收藏浏览器来源",
                    url="https://www.douyin.com/video/1234567890123456789",
                    synced_at="2026-08-13T00:00:00+00:00",
                    thumbnail_path=None,
                ),
            ),
        )

    def thumbnail_file(self, _relative_path: str) -> Path:
        raise FileNotFoundError


class _FolderFavorites(_HistoricalFavorites):
    def read_snapshot(self) -> DouyinFavoritesSnapshot:
        synced_at = "2026-08-25T00:00:00+00:00"
        return DouyinFavoritesSnapshot(
            synced_at=synced_at,
            folders=(
                DouyinFavoritesFolder("default", "默认收藏夹", 1),
                DouyinFavoritesFolder("10", "编程", 2),
                DouyinFavoritesFolder("20", "设计", 1),
            ),
            items=(
                DouyinFavorite(
                    aweme_id="1234567890123456781",
                    title="默认与编程",
                    url="https://www.douyin.com/video/1234567890123456781",
                    synced_at=synced_at,
                    thumbnail_path=None,
                    folder_ids=("default", "10"),
                ),
                DouyinFavorite(
                    aweme_id="1234567890123456782",
                    title="只在编程",
                    url="https://www.douyin.com/video/1234567890123456782",
                    synced_at=synced_at,
                    thumbnail_path=None,
                    folder_ids=("10",),
                ),
                DouyinFavorite(
                    aweme_id="1234567890123456783",
                    title="只在设计",
                    url="https://www.douyin.com/video/1234567890123456783",
                    synced_at=synced_at,
                    thumbnail_path=None,
                    folder_ids=("20",),
                ),
            ),
        )


class _ObservableFailedLogin:
    def __init__(self) -> None:
        self.session_id = "offline-douyin-session"
        self.status = "disconnected"
        self.polls = 0

    def _payload(self, message: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": self.session_id,
            "status": self.status,
            "expires_in": 60 if self.status not in {"failed", "cancelled"} else 0,
            "qr_available": False,
            "message": message,
        }
        if self.status == "failed":
            payload["failure_kind"] = "request_rejected"
        return payload

    def create_browser_session(self) -> dict[str, object]:
        self.status = "browser_ready"
        self.polls = 0
        return self._payload("官方验证窗口已打开，等待完成手机号与扫码验证。")

    def current_session(self) -> dict[str, object]:
        if self.status == "disconnected":
            return {
                "session_id": None,
                "status": "disconnected",
                "expires_in": 0,
                "qr_available": False,
                "message": "尚未连接抖音。",
            }
        return self._payload(
            "登录凭据已取得，但收藏请求被抖音拦截（HTTP 403）；"
            "当前网页接口校验已变化，不是扫码或验证码失败。"
            if self.status == "failed"
            else "正在完成抖音验证。"
        )

    def get_session(self, session_id: str) -> dict[str, object]:
        assert session_id == self.session_id
        self.polls += 1
        if self.polls == 1:
            self.status = "verification_required"
            return self._payload("抖音要求继续验证，请在原官方窗口完成页面提示。")
        if self.polls == 2:
            self.status = "validating"
            return self._payload("已取得登录凭据，正在验证收藏访问。")
        self.status = "failed"
        return self._payload(
            "登录凭据已取得，但收藏请求被抖音拦截（HTTP 403）；"
            "当前网页接口校验已变化，不是扫码或验证码失败。"
        )

    def cancel_session(self, session_id: str) -> dict[str, object]:
        assert session_id == self.session_id
        self.status = "cancelled"
        return self._payload("已取消本次抖音连接。")

    def shutdown(self) -> None:
        return None


class _ObservableConnectedLogin(_ObservableFailedLogin):
    def get_session(self, session_id: str) -> dict[str, object]:
        assert session_id == self.session_id
        self.polls += 1
        if self.polls == 1:
            self.status = "validating"
            return self._payload("已取得登录凭据，正在验证收藏访问。")
        self.status = "connected"
        return self._payload("登录凭据与收藏访问均已验证。")

    def cookie_for(self, session_id: str) -> SecretStr:
        assert session_id == self.session_id
        return SecretStr("offline-cookie")


class _StalledFavorites(_HistoricalFavorites):
    def __init__(self) -> None:
        self.sync_calls = 0

    def sync(self, _cookie: SecretStr, **_kwargs: object) -> DouyinFavoritesSnapshot:
        self.sync_calls += 1
        time.sleep(2.5)
        raise DouyinFavoritesError(
            "抖音官方页面已返回首批收藏，但继续加载下一批时没有响应；"
            "本次收藏未更新，请重试。"
        )


@dataclass
class _LoopbackApp:
    url: str
    root: Path
    provider_runs: list[tuple[str, ...]]
    source_failures: list[int]
    writer_failures: list[str]
    audio_failures: list[bool]
    clock: list[datetime]
    server: uvicorn.Server
    thread: threading.Thread
    run_tasks: Callable[..., object]


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_loopback_server(
    root: Path,
    run_tasks: Callable[..., object],
    clock: Callable[[], datetime] | None = None,
    douyin_login: object | None = None,
    douyin_favorites: object | None = None,
    douyin_short_resolver: object | None = None,
) -> tuple[str, uvicorn.Server, threading.Thread]:
    coordinator = AutomationCoordinator(  # type: ignore[arg-type]
        root, run_tasks=run_tasks, clock=clock or (lambda: datetime.now(UTC))
    )
    port = _port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_web_app(
                root,
                coordinator=coordinator,
                douyin_login=douyin_login,  # type: ignore[arg-type]
                douyin_favorites=douyin_favorites or _HistoricalFavorites(),  # type: ignore[arg-type]
                douyin_short_resolver=douyin_short_resolver,  # type: ignore[arg-type]
            ),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    return f"http://127.0.0.1:{port}", server, thread


def _stop_loopback_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)


def _task(root: Path, source: str, index: int):
    task_id = f"20260813-goal4{index:04d}"
    task_dir = root / "视频学习素材" / task_id
    task = create_task(
        task_id=task_id,
        source_path=source,
        source_fingerprint=f"goal4-{index:04d}",
        title=f"浏览器来源 {index}",
    )
    task = task.model_copy(
        update={
            "stages": {"content_pack": StageStatus.COMPLETED},
            "artifacts": {"content_pack": ["content_pack.json"]},
        }
    )
    task_dir.mkdir(parents=True)
    write_task_atomic(task_dir, task)
    (task_dir / "content_pack.json").write_text(
        ContentPack(
            task_id=task_id,
            source_fingerprint=task.source_fingerprint,
            evidence=[
                Evidence(
                    id="tr_0001",
                    kind="transcript",
                    start_ms=0,
                    end_ms=1000,
                    text="offline fixture",
                    artifact_path="content_pack.json",
                )
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )
    return task


def _open_view(page: Page, view: str) -> None:
    navigation = (
        ".mobile-nav"
        if page.viewport_size is not None and page.viewport_size["width"] <= 760
        else ".primary-nav"
    )
    page.locator(f'{navigation} [data-view="{view}"]').click()
    expect(page.locator(f'[data-view-panel="{view}"]')).to_be_visible()


_TASK_BUCKET_BY_LEGACY_LIST = {
    "processing-list": "processing",
    "inbox-list": "inbox",
    "library-list": "library",
}


def _first_task_row(page: Page, list_id: str):
    expected_bucket = _TASK_BUCKET_BY_LEGACY_LIST[list_id]
    return page.locator(
        f'#task-list article[data-item-ref][data-bucket="{expected_bucket}"]'
    ).first


def _open_settings_panel(page: Page, panel: str) -> None:
    _open_view(page, "settings")
    page.locator(f'[data-settings-tab="{panel}"]').click()
    expect(page.locator(f'[data-settings-panel="{panel}"]')).to_be_visible()


def _open_provider_roles(page: Page) -> None:
    _open_settings_panel(page, "roles")
    expect(page.locator("#provider-role-list")).to_be_visible()


def test_douyin_folder_rail_filters_compact_cards_and_keeps_cross_folder_selection(
    tmp_path: Path,
) -> None:
    url, server, thread = _start_loopback_server(
        tmp_path,
        lambda *_args, **_kwargs: [],
        douyin_favorites=_FolderFavorites(),
    )
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    console_issues: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(edge), headless=True
            )
            page = browser.new_page(viewport={"width": 2492, "height": 1415})
            page.on(
                "console",
                lambda message: (
                    console_issues.append(message.text)
                    if message.type in {"error", "warning"}
                    else None
                ),
            )
            page.goto(url)
            _open_view(page, "sources")
            expect(page.locator("#favorite-folders button")).to_have_count(4)
            expect(page.locator("#douyin-favorites-list article")).to_have_count(3)
            card_width = page.locator("#douyin-favorites-list article").first.evaluate(
                "element => element.getBoundingClientRect().width"
            )
            assert card_width < 300

            page.locator("button[data-favorite-folder='10']").click()
            expect(page.locator("#favorite-folder-title")).to_have_text("编程")
            expect(page.locator("#douyin-favorites-list article")).to_have_count(2)
            page.locator("#douyin-favorites-list input[type=checkbox]").nth(1).check()

            page.locator("button[data-favorite-folder='20']").click()
            expect(page.locator("#douyin-favorites-list article")).to_have_count(1)
            page.locator("#douyin-favorites-list input[type=checkbox]").check()
            expect(page.locator("#favorite-selection")).to_contain_text("已选 2 项")

            page.locator("button[data-favorite-folder='all']").click()
            expect(
                page.locator("#douyin-favorites-list input[type=checkbox]:checked")
            ).to_have_count(2)

            page.set_viewport_size({"width": 390, "height": 844})
            mobile = page.locator("#douyin-favorites-list article").evaluate_all(
                "elements => elements.slice(0, 2).map((element) => element.getBoundingClientRect().x)"
            )
            assert len(mobile) == 2 and mobile[0] != mobile[1]
            assert page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            assert console_issues == []
            browser.close()
    finally:
        _stop_loopback_server(server, thread)


def test_douyin_login_failure_reason_survives_refresh_in_real_edge(
    tmp_path: Path,
) -> None:
    login = _ObservableFailedLogin()
    url, server, thread = _start_loopback_server(
        tmp_path,
        lambda *_args, **_kwargs: [],
        douyin_login=login,
    )
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(edge), headless=True
            )
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            console_issues: list[str] = []
            page.on(
                "console",
                lambda message: (
                    console_issues.append(message.text)
                    if message.type in {"error", "warning"}
                    else None
                ),
            )
            page.goto(url)
            _open_view(page, "sources")
            expect(page.locator(".source-douyin-panel")).to_contain_text(
                "同步默认与自建收藏夹"
            )
            expect(page.locator(".source-connection-guidance")).to_contain_text(
                "同步会短暂恢复隔离官方页面"
            )
            page.locator("#connect-douyin").click()
            expect(page.locator("#douyin-login-message")).to_have_text(
                "登录凭据已取得，但收藏请求被抖音拦截（HTTP 403）；"
                "当前网页接口校验已变化，不是扫码或验证码失败。",
                timeout=6_000,
            )
            expect(page.locator("#douyin-connection-state")).to_have_text("接口变化")
            expect(page.locator("#connect-douyin")).to_be_disabled()
            expect(page.locator("#connect-douyin")).to_have_text("暂不需要重新扫码")

            page.reload()
            _open_view(page, "sources")
            expect(page.locator("#douyin-connection-state")).to_have_text("接口变化")
            expect(page.locator("#douyin-login-message")).to_have_text(
                "登录凭据已取得，但收藏请求被抖音拦截（HTTP 403）；"
                "当前网页接口校验已变化，不是扫码或验证码失败。"
            )
            assert console_issues == []
            browser.close()
    finally:
        _stop_loopback_server(server, thread)


def test_douyin_login_auto_sync_keeps_the_specific_failure_visible_in_real_edge(
    tmp_path: Path,
) -> None:
    login = _ObservableConnectedLogin()
    favorites = _StalledFavorites()
    url, server, thread = _start_loopback_server(
        tmp_path,
        lambda *_args, **_kwargs: [],
        douyin_login=login,
        douyin_favorites=favorites,
    )
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(edge), headless=True
            )
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            console_issues: list[str] = []
            page.on(
                "console",
                lambda message: (
                    console_issues.append(message.text)
                    if message.type in {"error", "warning"}
                    else None
                ),
            )
            page.goto(url)
            _open_view(page, "sources")
            page.locator("#connect-douyin").click()

            expect(page.locator("#favorites-status")).to_have_text(
                "正在获取抖音当前请求校验，并由后端同步全部收藏…",
                timeout=6_000,
            )
            expected = (
                "抖音官方页面已返回首批收藏，但继续加载下一批时没有响应；"
                "本次收藏未更新，请重试。"
            )
            expect(page.locator("#favorites-status")).to_have_text(
                expected,
                timeout=6_000,
            )
            page.wait_for_timeout(2_500)
            expect(page.locator("#favorites-status")).to_have_text(expected)
            expect(page.locator("#douyin-login-message")).to_have_text(
                f"登录有效，但收藏同步未完成：{expected}"
            )
            assert favorites.sync_calls == 1
            assert console_issues
            assert all("502" in issue for issue in console_issues)
            browser.close()
    finally:
        _stop_loopback_server(server, thread)


def _add_connection(page: Page, *, name: str, preset: str, key: str) -> None:
    _open_settings_panel(page, "connections")
    capability = {
        "local-asr": "asr",
        "local-ocr": "ocr",
        "mimo": "llm",
        "deepseek": "llm",
        "mimo-tts": "tts",
        "windows-tts": "tts",
    }[preset]
    page.locator(f'button[data-add-capability="{capability}"]').click()
    expect(page.locator("#connection-dialog")).to_be_visible()
    page.locator("#provider-connection-form [name=name]").fill(name)
    page.locator("#provider-connection-form [name=preset]").select_option(preset)
    if page.locator("#provider-key-field").is_visible():
        page.locator("#provider-connection-form [name=api_key]").fill(key)
    if capability == "llm":
        page.locator("#fetch-new-provider-models").click()
        first_model = page.locator(
            "#new-provider-model-list button[data-new-provider-model]"
        ).first
        expect(first_model).to_be_visible()
        first_model.click()
    page.locator("#provider-connection-form button[type=submit]").click()
    expect(page.locator("#connection-dialog")).not_to_be_visible()
    expect(page.get_by_text(name, exact=True)).to_be_visible()


def _add_and_bind_local_material_model(page: Page, *, preset: str, role: str) -> None:
    _add_connection(page, name=preset, preset=preset, key="")
    _open_provider_roles(page)
    role_select = page.locator(f'select[data-setup-role-select="{role}"]')
    expect(role_select).to_be_visible()
    role_select.select_option(preset)
    expect(page.locator("#provider-feedback")).to_contain_text(role)


def _enable_note_automation(page: Page, *, key: str) -> None:
    _add_connection(page, name="offline-note", preset="mimo", key=key)
    expect(page.get_by_text("offline-note", exact=True)).to_be_visible()
    _open_provider_roles(page)
    for role in ("\u7b14\u8bb0 Writer", "\u7b14\u8bb0 Reviewer"):
        page.locator(f'select[data-setup-role-select="{role}"]').select_option(
            "offline-note"
        )
        expect(page.locator("#provider-feedback")).to_contain_text(role)
    _open_settings_panel(page, "output")
    page.locator("#automation-form [name=default_output]").select_option(
        "complete_note"
    )
    expect(
        page.locator("#automation-form [name=check_interval_minutes]")
    ).to_have_value("30")
    with page.expect_response(
        lambda response: response.url.endswith("/api/automation/configure")
    ) as configured:
        page.locator("#automation-form button[type=submit]").click()
    assert configured.value.json()["check_interval_minutes"] == 30
    expect(page.locator("#automation-state")).to_have_text("等待付费许可")
    page.locator("#authorize-automation").click()
    expect(page.locator("#automation-authorization-dialog")).to_be_visible()
    page.locator("#confirm-automation-authorization").click()
    expect(page.locator("#automation-state")).to_have_text("付费许可已开启")
    expect(page.locator("#automation-access-title")).to_have_text("付费整理许可有效")
    expect(page.locator("#authorize-automation")).to_be_hidden()


def _enable_audio_automation(page: Page) -> None:
    _enable_note_automation(page, key="audio-note-key")
    for name, preset, key in (
        ("offline-podcast", "deepseek", "audio-podcast-key"),
        ("offline-tts", "windows-tts", "audio-tts-key"),
    ):
        _add_connection(page, name=name, preset=preset, key=key)
    _open_settings_panel(page, "output")
    page.locator("#automation-form [name=default_output]").select_option(
        "complete_note_with_audio"
    )
    page.locator("#automation-form button[type=submit]").click()
    expect(page.locator("#automation-state")).to_have_text("等待付费许可")
    _open_provider_roles(page)
    page.locator('select[data-setup-role-select="播客"]').select_option(
        "offline-podcast"
    )
    expect(page.locator("#provider-feedback")).to_contain_text("播客")
    page.locator('select[data-setup-role-select="TTS"]').select_option("offline-tts")
    _open_settings_panel(page, "output")
    page.locator("#authorize-automation").click()
    expect(page.locator("#automation-authorization-dialog")).to_be_visible()
    with page.expect_response(
        lambda response: response.url.endswith("/api/automation/authorize")
    ) as authorized:
        page.locator("#confirm-automation-authorization").click()
    assert authorized.value.status == 200
    expect(page.locator("#automation-state")).to_have_text("付费许可已开启")


@pytest.fixture
def loopback_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_LoopbackApp]:
    created = 0
    source_failures = [0]
    writer_failures: list[str] = []
    audio_failures: list[bool] = []
    clock = [datetime.now(UTC)]

    def process(_source: object, root: Path, _profile: str):
        nonlocal created
        if source_failures[0]:
            source_failures[0] -= 1
            raise PipelineError("offline retryable source failure")
        created += 1
        return _task(root, "safe-input", created)

    import learnnest.web_app as web_app
    from learnnest.provider_model_catalog import ModelCatalogPreview
    from learnnest.provider_profiles import connection_presets

    monkeypatch.setattr(
        web_app,
        "process_video",
        lambda source, root, profile: process(source, root, profile),
    )
    monkeypatch.setattr(
        web_app,
        "process_source",
        lambda source, root, profile, **_kwargs: process(source, root, profile),
    )

    def fake_preview(
        preset: str, _api_key: str, **_kwargs: object
    ) -> ModelCatalogPreview:
        adapter = connection_presets()[preset]
        return ModelCatalogPreview(
            source=adapter.catalog_mode or "live",
            models=[{"id": model_id} for model_id in sorted(adapter.allowed_models)],
            note="离线测试目录。",
            adapter_revision=adapter.adapter_revision,
        )

    monkeypatch.setattr(web_app, "preview_provider_model_catalog", fake_preview)
    provider_runs: list[tuple[str, ...]] = []

    def run_tasks(
        root: Path,
        task_ids: tuple[str, ...],
        *,
        default_outputs: dict[str, str],
        now: datetime,
    ):
        provider_runs.append(task_ids)
        if writer_failures:
            failure = writer_failures.pop(0)
            providers = _offline_providers(root, "")
            if failure == "timeout":
                providers.writer.write_markdown = (  # type: ignore[method-assign]
                    lambda _dossier: (_ for _ in ()).throw(
                        TimeoutError("offline timeout")
                    )
                )
            elif failure == "permanent":
                providers.writer.write_markdown = (  # type: ignore[method-assign]
                    lambda _dossier: (_ for _ in ()).throw(
                        RuntimeError("HTTP 400 permanent")
                    )
                )
            elif failure == "retryable":
                providers.writer.write_markdown = (  # type: ignore[method-assign]
                    lambda _dossier: (_ for _ in ()).throw(
                        RuntimeError("HTTP 503 temporary")
                    )
                )
            return run_automation_tasks(
                root, task_ids, providers, default_outputs=default_outputs, now=now
            )
        if audio_failures:
            return run_automation_tasks(
                root,
                task_ids,
                _offline_providers(
                    root, "", with_audio=True, audio_failure=audio_failures.pop(0)
                ),
                default_outputs=default_outputs,
                now=now,
            )
        return _run_offline_tasks(
            root, task_ids, default_outputs=default_outputs, now=now
        )

    douyin_login = _ObservableConnectedLogin()
    douyin_login.status = "connected"
    url, server, thread = _start_loopback_server(
        tmp_path,
        run_tasks,
        lambda: clock[0],
        douyin_login=douyin_login,
    )
    app = _LoopbackApp(
        url,
        tmp_path,
        provider_runs,
        source_failures,
        writer_failures,
        audio_failures,
        clock,
        server,
        thread,
        run_tasks,
    )
    try:
        yield app
    finally:
        _stop_loopback_server(app.server, app.thread)


def test_goal4_three_sources_reach_a_safe_note_in_real_edge(
    loopback_app: _LoopbackApp, tmp_path: Path
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    assert edge.is_file()
    requests: list[str] = []
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch(
            executable_path=str(edge), headless=True
        )
        page: Page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.on("request", lambda request: requests.append(request.url))
        page.on(
            "console",
            lambda message: (
                errors.append(message.text) if message.type == "error" else None
            ),
        )
        page.goto(loopback_app.url)
        _enable_note_automation(page, key="not-a-real-provider-key")
        _open_view(page, "sources")
        _open_settings_panel(page, "output")
        expect(page.locator("#automation-access-title")).to_have_text(
            "付费整理许可有效"
        )
        expect(page.locator("#authorize-automation")).to_be_hidden()
        expect(page.locator("#automation-access input[type=checkbox]")).to_have_count(0)
        status = page.evaluate(
            "async () => await (await fetch('/api/automation/status')).json()"
        )
        assert status["paid_authorized"] is True
        assert status["auto_new_favorites_enabled"] is False
        assert status["auto_new_favorites_active"] is False
        video = tmp_path / "lesson.mp4"
        video.write_bytes(b"offline video")
        _open_view(page, "sources")
        page.locator("[data-open-single-video]").first.click()
        page.locator("#local-video").set_input_files(str(video))
        with page.expect_response(
            lambda response: "/api/learning/uploads" in response.url
        ) as upload:
            page.locator("#upload-form button[type=submit]").click()
        assert upload.value.status == 202
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        with page.expect_response(
            lambda response: response.url.endswith("/api/learning/submit")
        ) as public:
            page.locator('button[form="url-form"]').click()
        assert public.value.status == 202
        expect(page.locator(".source-jobs .source-job")).to_have_count(2)
        _open_view(page, "sources")
        expect(
            page.locator("#douyin-favorites-list input[type=checkbox]")
        ).to_have_count(1)
        page.locator("#douyin-favorites-list input[type=checkbox]").check()
        with page.expect_response(
            lambda response: response.url.endswith("/api/douyin/favorites/select")
        ) as favorite:
            page.locator("#add-selected-favorites").click()
        assert favorite.value.status == 202
        expect(page.locator(".source-jobs .source-job")).to_have_count(3)
        deadline = time.monotonic() + 30
        while (
            page.locator('#task-list article[data-bucket="library"]').count() != 3
            and time.monotonic() < deadline
        ):
            page.wait_for_timeout(100)
        snapshot = page.evaluate(
            "async () => await (await fetch('/api/learning/snapshot')).json()"
        )
        assert page.locator('#task-list article[data-bucket="library"]').count() == 3, (
            json.dumps(snapshot, ensure_ascii=False)
        )
        row = _first_task_row(page, "library-list")
        expect(row.locator(".progress-ring strong")).to_have_text("100")
        with page.expect_popup() as note:
            row.locator("button[data-action='open_note']").click()
        expect(note.value.locator("article.note-content")).to_contain_text(
            "离线浏览器笔记"
        )
        body = page.locator("body").inner_text()
        assert "not-a-real-provider-key" not in body
        assert str(video) not in body
        assert "https://www.bilibili.com/video/BV1xx411c7mD" not in body
        assert "https://www.douyin.com/video/1234567890123456789" not in body
        assert all(url.startswith(loopback_app.url) for url in requests)
        assert errors == []
        assert loopback_app.provider_runs
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        browser.close()


def test_goal4_waiting_setup_survives_refresh_without_constructing_a_provider(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(loopback_app.url)
        _open_settings_panel(page, "output")
        expect(page.locator("#authorize-automation")).to_be_disabled()
        expect(page.locator("#automation-access-title")).to_have_text(
            "\u5148\u5b8c\u6210\u8bbe\u7f6e"
        )
        assert loopback_app.provider_runs == []
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        with page.expect_response(
            lambda response: response.url.endswith("/api/learning/submit")
        ) as submitted:
            page.locator('button[form="url-form"]').click()
        assert submitted.value.status == 202
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        expect(
            page.locator('#task-list article[data-bucket="processing"]')
        ).to_contain_text("请先完成整理设置")
        page.reload()
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        expect(
            page.locator('#task-list article[data-bucket="processing"]')
        ).to_contain_text("请先完成整理设置")
        assert loopback_app.provider_runs == []
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        browser.close()


def test_local_asr_and_ocr_are_visible_bindable_and_persist_in_real_edge(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        console_issues: list[str] = []
        page.on(
            "console",
            lambda message: (
                console_issues.append(message.text)
                if message.type in {"error", "warning"}
                else None
            ),
        )
        page.goto(loopback_app.url)
        _open_settings_panel(page, "connections")

        expect(page.locator('[data-capability="asr"]')).to_contain_text(
            "faster-whisper large-v3"
        )
        expect(page.locator('[data-capability="ocr"]')).to_contain_text("PaddleOCR")
        _open_provider_roles(page)
        expect(page.locator('[data-capability="asr"]')).to_contain_text(
            "faster-whisper large-v3"
        )
        expect(page.locator('[data-provider-role="语音识别（ASR）"]')).to_contain_text(
            "设备可用"
        )
        _add_and_bind_local_material_model(
            page, preset="local-asr", role="语音识别（ASR）"
        )
        _add_and_bind_local_material_model(
            page, preset="local-ocr", role="画面文字（OCR）"
        )

        page.reload()
        _open_settings_panel(page, "connections")
        expect(
            page.locator('button[data-delete-connection="local-asr"]')
        ).to_be_disabled()
        expect(
            page.locator('button[data-delete-connection="local-ocr"]')
        ).to_be_disabled()
        settings = load_settings(loopback_app.root)
        assert settings.role_bindings["asr"].connection_id == "local-asr"
        assert settings.role_bindings["ocr"].connection_id == "local-ocr"
        assert loopback_app.provider_runs == []
        assert console_issues == []
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        browser.close()


def test_provider_card_protects_bound_connection_and_deletes_idle_connection(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        console_issues: list[str] = []
        page.on(
            "console",
            lambda message: (
                console_issues.append(message.text)
                if message.type in {"error", "warning"}
                else None
            ),
        )
        page.goto(loopback_app.url)
        _add_connection(page, name="mimo-note", preset="mimo", key="note-key")
        _open_provider_roles(page)
        writer_select = page.locator('select[data-setup-role-select="笔记 Writer"]')
        writer_select.select_option("mimo-note")
        expect(page.locator("#provider-feedback")).to_contain_text("笔记 Writer")
        _add_connection(
            page,
            name="deepseek-spare",
            preset="deepseek",
            key="spare-key",
        )

        bound_delete = page.locator('button[data-delete-connection="mimo-note"]')
        idle_delete = page.locator('button[data-delete-connection="deepseek-spare"]')
        expect(bound_delete).to_be_disabled()
        expect(bound_delete).to_have_css("color", "rgb(167, 177, 191)")
        expect(bound_delete).to_have_css("background-color", "rgb(242, 244, 247)")
        expect(idle_delete).to_be_enabled()

        idle_delete.click()
        expect(page.locator("#delete-connection-dialog")).to_be_visible()
        expect(page.locator("#delete-connection-name")).to_have_text("deepseek-spare")
        with page.expect_response(
            lambda response: (
                response.url.endswith("/api/providers/connections/deepseek-spare")
                and response.request.method == "DELETE"
            )
        ) as deleted:
            page.locator("#confirm-delete-connection").click()
        assert deleted.value.status == 200
        expect(page.locator("#delete-connection-dialog")).not_to_be_visible()
        expect(page.locator('[data-connection="deepseek-spare"]')).to_have_count(0)
        expect(page.locator('[data-connection="mimo-note"]')).to_have_count(1)

        page.reload()
        _open_settings_panel(page, "connections")
        expect(page.locator('[data-connection="deepseek-spare"]')).to_have_count(0)
        settings = load_settings(loopback_app.root)
        assert "deepseek-spare" not in settings.connections
        assert settings.role_bindings["note_writer"].connection_id == "mimo-note"
        assert loopback_app.provider_runs == []
        assert console_issues == []
        browser.close()


def test_goal4_podcast_role_only_offers_an_isolated_llm_connection(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _add_connection(page, name="mimo-note", preset="mimo", key="same-key")
        _open_provider_roles(page)
        for role in ("笔记 Writer", "笔记 Reviewer"):
            page.locator(f'select[data-setup-role-select="{role}"]').select_option(
                "mimo-note"
            )
            expect(page.locator("#provider-feedback")).to_contain_text(role)

        podcast_select = page.locator('select[data-setup-role-select="播客"]')
        expect(podcast_select).to_be_visible()
        expect(podcast_select.locator('option[value="mimo-note"]')).to_have_count(0)
        expect(podcast_select.locator("option")).to_have_count(1)
        expect(page.locator('[data-provider-role="播客"]')).to_contain_text(
            "需要单独的播客连接"
        )

        _add_connection(page, name="mimo-podcast", preset="mimo", key="same-key")
        _open_provider_roles(page)
        expect(podcast_select).to_be_visible()
        expect(podcast_select.locator('option[value="mimo-note"]')).to_have_count(0)
        expect(podcast_select.locator('option[value="mimo-podcast"]')).to_have_count(1)
        browser.close()


def test_goal4_late_provider_poll_does_not_replace_a_new_role_selection(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _add_connection(page, name="offline-note", preset="mimo", key="test-key")
        _open_provider_roles(page)
        select = page.locator('select[data-setup-role-select="笔记 Writer"]')
        expect(select).to_be_visible()
        page.evaluate(
            """
            () => {
              const originalFetch = window.fetch;
              let release;
              const gate = new Promise((resolve) => { release = resolve; });
              window.__releaseProviderPoll = release;
              window.fetch = (...args) => originalFetch(...args).then(async (response) => {
                if (String(args[0]).startsWith('/api/providers/settings')) await gate;
                return response;
              });
              window.__providerPoll = refreshProviderSettingsWhenIdle().finally(() => {
                window.fetch = originalFetch;
              });
            }
            """
        )
        select.select_option("offline-note")
        expect(page.locator("#provider-feedback")).to_contain_text("笔记 Writer")
        page.evaluate("window.__releaseProviderPoll()")
        page.evaluate("window.__providerPoll")

        expect(
            page.locator('select[data-setup-role-select="笔记 Writer"]')
        ).to_have_value("offline-note")
        browser.close()


def test_provider_model_picker_fetches_searches_saves_and_persists_on_desktop_and_narrow_edge(
    loopback_app: _LoopbackApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    import learnnest.web_app as web_app

    fetches: list[tuple[Path, str]] = []

    def fake_fetch(root: str | Path, name: str) -> list[dict[str, str]]:
        fetches.append((Path(root), name))
        return [
            {"id": "mimo-v2.5"},
            {"id": "mimo-v2.5-pro", "owned_by": "xiaomi"},
        ]

    monkeypatch.setattr(web_app, "fetch_provider_model_catalog", fake_fetch)
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(loopback_app.url)
        _add_connection(page, name="mimo-model", preset="mimo", key="catalog-key")
        _open_provider_roles(page)
        for role in ("笔记 Writer", "笔记 Reviewer"):
            page.locator(f'select[data-setup-role-select="{role}"]').select_option(
                "mimo-model"
            )
        assert fetches == []

        _open_settings_panel(page, "connections")
        row = page.locator('[data-connection="mimo-model"]')
        assert (
            row.evaluate("element => getComputedStyle(element).borderTopLeftRadius")
            == "10px"
        )
        row.locator("button[data-select-model]").click()
        expect(page.locator("#model-selection-dialog")).to_be_visible()
        expect(page.locator("#model-selection-current")).to_have_text("mimo-v2.5")
        expect(page.locator("#model-selection-connection")).to_have_count(0)
        expect(page.locator("#provider-model-empty")).to_be_visible()

        page.locator("#fetch-provider-models").click()
        expect(
            page.locator("#provider-model-list button[data-provider-model]")
        ).to_have_count(2)
        assert fetches == [(loopback_app.root.resolve(), "mimo-model")]
        page.locator("#provider-model-search").fill("pro")
        expect(
            page.locator("#provider-model-list button[data-provider-model]")
        ).to_have_count(1)
        page.locator('button[data-provider-model="mimo-v2.5-pro"]').click()
        expect(page.locator("#save-provider-model")).to_be_enabled()
        page.locator("#save-provider-model").click()
        expect(page.locator("#model-selection-dialog")).not_to_be_visible()
        expect(page.locator("#provider-feedback")).to_contain_text("受影响职责")
        expect(page.locator("#provider-feedback")).to_contain_text("付费许可需重新确认")

        page.reload()
        _open_settings_panel(page, "connections")
        expect(row.locator(".provider-model")).to_contain_text("mimo-v2.5-pro")
        assert load_settings(loopback_app.root).connections["mimo-model"].model == (
            "mimo-v2.5-pro"
        )
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        browser.close()


def test_provider_model_picker_covers_loading_empty_error_and_ignores_late_response(
    loopback_app: _LoopbackApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    import learnnest.web_app as web_app

    mode = ["empty"]

    def fake_fetch(_root: str | Path, _name: str) -> list[dict[str, str]]:
        if mode[0] == "error":
            raise ProviderModelCatalogError(
                "service_error", "官方模型目录服务暂时不可用，请稍后重试。"
            )
        return []

    monkeypatch.setattr(web_app, "fetch_provider_model_catalog", fake_fetch)
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(loopback_app.url)
        _add_connection(page, name="mimo-empty", preset="mimo", key="catalog-key")
        page.locator('[data-connection="mimo-empty"] button[data-select-model]').click()
        page.locator("#fetch-provider-models").click()
        expect(page.locator("#provider-model-empty")).to_contain_text(
            "没有可选择的兼容模型"
        )

        mode[0] = "error"
        page.locator("#fetch-provider-models").click()
        expect(page.locator("#provider-model-feedback")).to_have_text(
            "官方模型目录服务暂时不可用，请稍后重试。"
        )

        mode[0] = "empty"
        page.evaluate(
            """
            () => {
              const originalFetch = window.fetch;
              let release;
              const gate = new Promise((resolve) => { release = resolve; });
              window.__releaseModelCatalog = release;
              window.fetch = (...args) => originalFetch(...args).then(async (response) => {
                if (String(args[0]).includes('/api/providers/connections/mimo-empty/models')) await gate;
                return response;
              });
              window.__lateModelCatalog = fetchProviderModels().finally(() => {
                window.fetch = originalFetch;
              });
            }
            """
        )
        expect(page.locator("#fetch-provider-models")).to_have_text("获取中…")
        page.locator(
            "#model-selection-dialog [data-close-model-selection]"
        ).first.click()
        page.evaluate("window.__releaseModelCatalog()")
        page.evaluate("window.__lateModelCatalog")
        page.locator('[data-connection="mimo-empty"] button[data-select-model]').click()
        expect(page.locator("#provider-model-list")).to_be_empty()
        browser.close()


def test_w32_provider_new_connection_live_curated_atomic_save_on_desktop_and_narrow_edge(
    loopback_app: _LoopbackApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    import learnnest.web_app as web_app
    from learnnest.provider_model_catalog import ModelCatalogPreview

    previews: list[tuple[str, str]] = []

    def fake_preview(
        preset: str, api_key: str, **_kwargs: object
    ) -> ModelCatalogPreview:
        previews.append((preset, api_key))
        if preset == "kimi":
            return ModelCatalogPreview(
                source="live",
                models=[
                    {"id": "kimi-k2.6"},
                    {"id": "kimi-k3", "owned_by": "moonshot"},
                ],
                note="实时目录来自该 Provider 当前可见模型与语栖适配允许列表的交集。",
                adapter_revision="1",
            )
        return ModelCatalogPreview(
            source="curated",
            models=[{"id": "glm-4.6"}, {"id": "glm-5.3"}],
            note="内置支持列表由语栖维护，不代表你的账号已开通该模型。",
            adapter_revision="1",
        )

    monkeypatch.setattr(web_app, "preview_provider_model_catalog", fake_preview)

    def fake_fetch(_root: str | Path, name: str) -> list[dict[str, str]]:
        assert name == "w32-kimi"
        return [{"id": "kimi-k2.6"}, {"id": "kimi-k3", "owned_by": "moonshot"}]

    monkeypatch.setattr(web_app, "fetch_provider_model_catalog", fake_fetch)
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(loopback_app.url)
        _open_settings_panel(page, "connections")

        page.locator('button[data-add-capability="llm"]').click()
        expect(page.locator("#connection-dialog")).to_be_visible()
        expect(
            page.locator('#provider-connection-form option[value="openrouter"]')
        ).to_have_text("OpenRouter（多模型平台）")
        page.locator("#provider-connection-form [name=name]").fill("w32-kimi")
        page.locator("#provider-connection-form [name=preset]").select_option("kimi")
        expect(page.locator("#new-provider-model-section")).to_be_visible()
        expect(page.locator("#provider-key-entry")).to_be_visible()
        expect(page.locator("#provider-key-entry-link")).to_have_attribute(
            "href", "https://platform.kimi.com/console/api-keys"
        )
        expect(
            page.locator("#provider-connection-form button[type=submit]")
        ).to_be_disabled()
        page.locator("#provider-connection-form [name=api_key]").fill("kimi-e2e-key")
        page.locator("#fetch-new-provider-models").click()
        expect(
            page.locator("#new-provider-model-list button[data-new-provider-model]")
        ).to_have_count(2)
        expect(page.locator("#new-provider-model-feedback")).to_contain_text("实时目录")
        expect(
            page.locator("#provider-connection-form button[type=submit]")
        ).to_be_disabled()
        page.locator('button[data-new-provider-model="kimi-k3"]').click()
        expect(
            page.locator("#provider-connection-form button[type=submit]")
        ).to_be_enabled()
        page.locator("#provider-connection-form button[type=submit]").click()
        expect(page.locator("#connection-dialog")).not_to_be_visible()
        expect(page.locator("#provider-feedback")).to_contain_text("kimi-k3")

        settings = load_settings(loopback_app.root)
        connection = settings.connections["w32-kimi"]
        assert connection.model == "kimi-k3"
        assert connection.api_family == "openai_chat"
        assert connection.preset == "kimi"
        assert connection.secret_id is not None
        assert (
            loopback_app.root / ".learnnest" / "providers" / "settings.json"
        ).is_file()
        from learnnest.provider_secrets import ProviderSecretStore

        assert (
            ProviderSecretStore(loopback_app.root)
            .path_for(connection.secret_id)
            .is_file()
        )
        assert "kimi-e2e-key" not in page.locator("body").inner_text()
        assert previews == [("kimi", "kimi-e2e-key")]

        _open_provider_roles(page)
        page.locator('select[data-setup-role-select="笔记 Writer"]').select_option(
            "w32-kimi"
        )
        expect(page.locator("#provider-feedback")).to_contain_text("笔记 Writer")

        _open_settings_panel(page, "connections")
        row = page.locator('[data-connection="w32-kimi"]')
        row.locator("button[data-select-model]").click()
        expect(page.locator("#model-selection-dialog")).to_be_visible()
        expect(page.locator("#model-selection-current")).to_have_text("kimi-k3")
        page.locator("#fetch-provider-models").click()
        expect(
            page.locator("#provider-model-list button[data-provider-model]")
        ).to_have_count(2)
        page.locator('button[data-provider-model="kimi-k2.6"]').click()
        page.locator("#save-provider-model").click()
        expect(page.locator("#model-selection-dialog")).not_to_be_visible()
        expect(page.locator("#provider-feedback")).to_contain_text("受影响职责")
        expect(page.locator("#provider-feedback")).to_contain_text("付费许可需重新确认")
        assert (
            load_settings(loopback_app.root).connections["w32-kimi"].model
            == "kimi-k2.6"
        )

        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('button[data-add-capability="llm"]').click()
        expect(page.locator("#connection-dialog")).to_be_visible()
        page.locator("#provider-connection-form [name=name]").fill("w32-glm")
        page.locator("#provider-connection-form [name=preset]").select_option("glm")
        expect(page.locator("#new-provider-model-section")).to_be_visible()
        page.locator("#provider-connection-form [name=api_key]").fill("glm-e2e-key")
        page.locator("#fetch-new-provider-models").click()
        expect(
            page.locator("#new-provider-model-list button[data-new-provider-model]")
        ).to_have_count(2)
        expect(page.locator("#new-provider-model-feedback")).to_contain_text(
            "内置支持列表"
        )
        page.locator('button[data-new-provider-model="glm-5.3"]').click()
        page.locator("#provider-connection-form button[type=submit]").click()
        expect(page.locator("#connection-dialog")).not_to_be_visible()
        assert (
            load_settings(loopback_app.root).connections["w32-glm"].model == "glm-5.3"
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        assert "glm-e2e-key" not in page.locator("body").inner_text()
        browser.close()


def test_w32_provider_refuses_without_model_and_cancel_or_failure_leave_zero_residue(
    loopback_app: _LoopbackApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    import learnnest.web_app as web_app
    from learnnest.provider_model_catalog import ModelCatalogPreview

    mode = ["ok"]

    def fake_preview(
        _preset: str, _api_key: str, **_kwargs: object
    ) -> ModelCatalogPreview:
        if mode[0] == "error":
            raise ProviderModelCatalogError(
                "authentication", "官方目录认证失败，请检查已保存 API Key。"
            )
        return ModelCatalogPreview(
            source="live",
            models=[{"id": "kimi-k2.6"}, {"id": "kimi-k3"}],
            note="实时目录来自交集。",
            adapter_revision="1",
        )

    monkeypatch.setattr(web_app, "preview_provider_model_catalog", fake_preview)
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(loopback_app.url)
        _open_settings_panel(page, "connections")

        def provider_files() -> list[str]:
            return sorted(
                path.name
                for path in (loopback_app.root / ".learnnest" / "providers").glob("*")
            )

        baseline = provider_files()

        page.locator('button[data-add-capability="llm"]').click()
        expect(page.locator("#connection-dialog")).to_be_visible()
        page.locator("#provider-connection-form [name=name]").fill("w32-residue")
        page.locator("#provider-connection-form [name=preset]").select_option("kimi")
        page.locator("#provider-connection-form [name=api_key]").fill("residue-key")
        page.locator("#fetch-new-provider-models").click()
        expect(
            page.locator("#new-provider-model-list button[data-new-provider-model]")
        ).to_have_count(2)
        expect(
            page.locator("#provider-connection-form button[type=submit]")
        ).to_be_disabled()
        page.locator('button[data-new-provider-model="kimi-k3"]').click()
        page.locator("#connection-dialog [data-close-connection]").first.click()
        expect(page.locator("#connection-dialog")).not_to_be_visible()
        assert provider_files() == baseline
        assert load_settings(loopback_app.root).connections == {}

        mode[0] = "error"
        page.locator('button[data-add-capability="llm"]').click()
        expect(page.locator("#connection-dialog")).to_be_visible()
        page.locator("#provider-connection-form [name=name]").fill("w32-residue")
        page.locator("#provider-connection-form [name=preset]").select_option("kimi")
        page.locator("#provider-connection-form [name=api_key]").fill("residue-key")
        page.locator("#fetch-new-provider-models").click()
        expect(page.locator("#new-provider-model-feedback")).to_have_text(
            "官方目录认证失败，请检查已保存 API Key。"
        )
        expect(
            page.locator("#new-provider-model-list button[data-new-provider-model]")
        ).to_have_count(0)
        page.locator("#connection-dialog [data-close-connection]").first.click()
        expect(page.locator("#connection-dialog")).not_to_be_visible()
        assert provider_files() == baseline
        assert load_settings(loopback_app.root).connections == {}

        mode[0] = "ok"
        page.locator('button[data-add-capability="llm"]').click()
        expect(page.locator("#connection-dialog")).to_be_visible()
        page.locator("#provider-connection-form [name=name]").fill("w32-late")
        page.locator("#provider-connection-form [name=preset]").select_option("kimi")
        page.locator("#provider-connection-form [name=api_key]").fill("late-key")
        page.evaluate(
            """
            () => {
              const originalFetch = window.fetch;
              let release;
              const gate = new Promise((resolve) => { release = resolve; });
              window.__releasePreviewCatalog = release;
              window.fetch = (...args) => originalFetch(...args).then(async (response) => {
                if (String(args[0]).includes('/api/providers/presets/kimi/models')) await gate;
                return response;
              });
              window.__latePreview = fetchNewProviderModels().finally(() => {
                window.fetch = originalFetch;
              });
            }
            """
        )
        expect(page.locator("#fetch-new-provider-models")).to_have_text("获取中…")
        page.locator("#connection-dialog [data-close-connection]").first.click()
        page.evaluate("window.__releasePreviewCatalog()")
        page.evaluate("window.__latePreview")
        page.locator('button[data-add-capability="llm"]').click()
        expect(page.locator("#connection-dialog")).to_be_visible()
        page.locator("#provider-connection-form [name=preset]").select_option("kimi")
        expect(
            page.locator("#new-provider-model-list button[data-new-provider-model]")
        ).to_have_count(0)
        expect(
            page.locator("#provider-connection-form button[type=submit]")
        ).to_be_disabled()
        browser.close()


def test_goal4_substantive_setting_change_revokes_browser_authorization(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _enable_note_automation(page, key="first-test-key")
        _add_connection(
            page, name="replacement-note", preset="mimo", key="second-test-key"
        )
        _open_provider_roles(page)
        page.locator('select[data-setup-role-select="笔记 Writer"]').select_option(
            "replacement-note"
        )
        expect(page.locator("#automation-state")).to_have_text("需要重新确认付费许可")
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        page.locator('button[form="url-form"]').click()
        expect(
            page.locator('#task-list article[data-bucket="processing"]')
        ).to_contain_text("请开启付费整理许可")
        assert loopback_app.provider_runs == []
        assert "first-test-key" not in page.locator("body").inner_text()
        assert "second-test-key" not in page.locator("body").inner_text()
        browser.close()


def test_goal4_restarting_webui_keeps_source_job_and_intake_visible(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        page.locator('button[form="url-form"]').click()
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        expect(
            page.locator('#task-list article[data-bucket="processing"]')
        ).to_contain_text("请先完成整理设置")
        browser.close()
        _stop_loopback_server(loopback_app.server, loopback_app.thread)
        (
            loopback_app.url,
            loopback_app.server,
            loopback_app.thread,
        ) = _start_loopback_server(
            loopback_app.root, loopback_app.run_tasks, lambda: loopback_app.clock[0]
        )
        restarted = playwright.chromium.launch(executable_path=str(edge), headless=True)
        restored = restarted.new_page(viewport={"width": 1280, "height": 720})
        restored.goto(loopback_app.url)
        expect(restored.locator(".source-jobs .source-job")).to_have_count(1)
        expect(
            restored.locator('#task-list article[data-bucket="processing"]')
        ).to_contain_text("请先完成整理设置")
        assert (
            len(
                list(
                    (loopback_app.root / ".learnnest" / "automation" / "intake").glob(
                        "*.json"
                    )
                )
            )
            == 1
        )
        assert loopback_app.provider_runs == []
        restarted.close()


def test_w2_manual_pause_survives_refresh_and_restart_before_resuming_scheduler(
    loopback_app: _LoopbackApp,
) -> None:
    task = _task(loopback_app.root, "safe-paused-input", 99)
    create_intake(
        loopback_app.root,
        AutomationIntake(
            task_id=task.task_id,
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime.now(UTC),
        ),
    )
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    console_issues: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 2492, "height": 1415})
        page.on(
            "console",
            lambda message: (
                console_issues.append(message.text)
                if message.type in {"error", "warning"}
                else None
            ),
        )
        page.goto(loopback_app.url)
        wide_layout = page.evaluate(
            """() => {
              const consoleRect = document.querySelector('#task-console').getBoundingClientRect();
              const spineRect = document.querySelector('#task-spine').getBoundingClientRect();
              const canvasRect = document.querySelector('.task-canvas').getBoundingClientRect();
              return {
                consoleWidth: consoleRect.width,
                leftMargin: consoleRect.left,
                rightMargin: window.innerWidth - consoleRect.right,
                spineWidth: spineRect.width,
                canvasWidth: canvasRect.width,
                hasInsightRail: Boolean(document.querySelector('#task-insight-rail')),
              };
            }"""
        )
        assert 1_170 <= wide_layout["consoleWidth"] <= 1_190
        assert abs((wide_layout["leftMargin"] - 220) - wide_layout["rightMargin"]) <= 5
        assert wide_layout["spineWidth"] == wide_layout["consoleWidth"]
        assert wide_layout["canvasWidth"] == wide_layout["consoleWidth"]
        assert wide_layout["hasInsightRail"] is False
        for view, selector, minimum, maximum in (
            ("sources", ".source-workspace", 1_170, 1_190),
            ("settings", ".settings-workspace", 1_170, 1_190),
        ):
            _open_view(page, view)
            page_layout = page.locator(selector).evaluate(
                """element => {
                  const rect = element.getBoundingClientRect();
                  return {
                    width: rect.width,
                    leftMargin: rect.left,
                    rightMargin: window.innerWidth - rect.right,
                  };
                }"""
            )
            assert minimum <= page_layout["width"] <= maximum
            assert (
                abs((page_layout["leftMargin"] - 220) - page_layout["rightMargin"]) <= 5
            )
        _open_view(page, "tasks")
        expect(page.locator("#task-list article[data-item-ref]")).to_have_count(1)
        page.locator("#task-search").fill("does-not-exist")
        expect(page.locator("#task-list article[data-item-ref]")).to_be_hidden()
        expect(page.locator("#task-filter-empty")).to_be_visible()
        page.locator("#task-search").fill("safe-paused-input")
        expect(page.locator("#task-list article[data-item-ref]")).to_be_visible()
        expect(page.locator("#task-filter-empty")).to_be_hidden()
        expect(page.locator("#task-focus")).to_have_count(0)
        row = _first_task_row(page, "processing-list")
        pause_button = row.locator("button[data-pause-item-ref]")
        expect(pause_button).to_be_visible()
        pause_border = pause_button.evaluate(
            """element => {
              const style = getComputedStyle(element);
              return {
                borderTopStyle: style.borderTopStyle,
                borderTopWidth: style.borderTopWidth,
                borderColor: style.borderColor,
              };
            }"""
        )
        assert pause_border["borderTopStyle"] == "solid"
        assert pause_border["borderTopWidth"] == "1px"
        assert pause_border["borderColor"] != "rgba(0, 0, 0, 0)"

        with page.expect_response(
            lambda response: response.url.endswith("/pause")
        ) as paused:
            row.locator("button[data-pause-item-ref]").click()
        assert paused.value.status == 200
        row = _first_task_row(page, "processing-list")
        expect(row.locator(".learning-state")).to_have_text("已暂停")
        expect(row.locator("button[data-action='resume_task']")).to_be_visible()
        expect(row.locator("button[data-delete-item-ref]")).to_be_enabled()
        found = find_task_by_id(loopback_app.root, task.task_id)
        assert found is not None
        assert load_task_control(found[0], found[1].task_id).manually_paused is True
        assert load_intake(loopback_app.root, task.task_id).status == "pending"
        assert loopback_app.provider_runs == []

        page.locator("button[data-filter='paused']").click()
        expect(page.locator('[data-filter-count="paused"]')).to_have_text("1")
        expect(page.locator('[data-filter-count="processing"]')).to_have_text("0")
        expect(page.locator("#task-list article[data-item-ref]")).to_be_visible()
        page.reload()
        expect(
            _first_task_row(page, "processing-list").locator(".learning-state")
        ).to_have_text("已暂停")
        browser.close()

        _stop_loopback_server(loopback_app.server, loopback_app.thread)
        (
            loopback_app.url,
            loopback_app.server,
            loopback_app.thread,
        ) = _start_loopback_server(
            loopback_app.root, loopback_app.run_tasks, lambda: loopback_app.clock[0]
        )
        restarted = playwright.chromium.launch(executable_path=str(edge), headless=True)
        restored = restarted.new_page(viewport={"width": 390, "height": 844})
        restored.on(
            "console",
            lambda message: (
                console_issues.append(message.text)
                if message.type in {"error", "warning"}
                else None
            ),
        )
        restored.goto(loopback_app.url)
        expect(restored.locator('[data-filter-count="paused"]')).to_have_text("1")
        expect(
            _first_task_row(restored, "processing-list").locator(".learning-state")
        ).to_have_text("已暂停")

        _enable_note_automation(restored, key="offline-pause-key")
        assert loopback_app.provider_runs == []
        assert load_intake(loopback_app.root, task.task_id).status == "pending"
        restored.reload()
        expect(
            restored.locator('#task-list article[data-bucket="inbox"]')
        ).to_have_count(1)
        _open_view(restored, "tasks")
        row = _first_task_row(restored, "inbox-list")
        with restored.expect_response(
            lambda response: response.url.endswith("/resume")
        ) as resumed:
            row.locator("button[data-action='resume_task']").click()
        assert resumed.value.status == 200
        found_after_resume = find_task_by_id(loopback_app.root, task.task_id)
        assert found_after_resume is not None
        deadline = time.monotonic() + 15
        while (
            load_intake(loopback_app.root, task.task_id).status
            in {"pending", "claimed"}
            and time.monotonic() < deadline
        ):
            restored.wait_for_timeout(100)
        assert load_intake(loopback_app.root, task.task_id).status == "completed", {
            "provider_runs": loopback_app.provider_runs,
            "manually_paused": load_task_control(
                found_after_resume[0], task.task_id
            ).manually_paused,
        }
        restored.reload()
        expect(
            restored.locator('#task-list article[data-bucket="library"]')
        ).to_have_count(1)
        found = find_task_by_id(loopback_app.root, task.task_id)
        assert found is not None
        assert load_task_control(found[0], task.task_id).manually_paused is False
        assert load_intake(loopback_app.root, task.task_id).status == "completed"
        assert loopback_app.provider_runs == [(task.task_id,)]
        assert console_issues == []
        assert restored.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        restarted.close()


def test_goal4_corrupting_an_intake_fact_turns_the_browser_gate_red_then_green(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        with page.expect_response(
            lambda response: response.url.endswith("/api/learning/submit")
        ) as submitted:
            page.locator('button[form="url-form"]').click()
        assert submitted.value.status == 202
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        expect(
            page.locator('#task-list article[data-bucket="processing"]')
        ).to_contain_text("请先完成整理设置")
        intake = next(
            (loopback_app.root / ".learnnest" / "automation" / "intake").glob("*.json")
        )
        original = intake.read_bytes()
        intake.write_text("{broken", encoding="utf-8")
        page.reload()
        expect(
            page.locator('#task-list article[data-bucket="processing"]')
        ).to_contain_text("需要你处理")
        intake.write_bytes(original)
        page.reload()
        expect(
            page.locator('#task-list article[data-bucket="processing"]')
        ).to_contain_text("请先完成整理设置")
        assert loopback_app.provider_runs == []
        browser.close()


def test_goal4_user_can_move_one_stopped_task_to_trash(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    console_issues: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.on(
            "console",
            lambda message: (
                console_issues.append(message.text)
                if message.type in {"error", "warning"}
                else None
            ),
        )
        page.goto(loopback_app.url)
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        page.locator('button[form="url-form"]').click()
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        _open_view(page, "tasks")
        expect(page.locator("#task-list article")).to_have_count(1)
        page.locator("#task-list article button[data-delete-item-ref]").click()
        expect(page.locator("#delete-task-dialog")).to_be_visible()
        with page.expect_response(
            lambda response: (
                response.request.method == "DELETE"
                and "/api/learning/items/" in response.url
            )
        ) as deleted:
            page.locator("#confirm-delete-task").click()
        assert deleted.value.status == 200
        expect(page.locator("#task-list article[data-item-ref]")).to_have_count(0)
        expect(page.locator("#notice")).to_contain_text("已移入回收区")
        assert loopback_app.provider_runs == []
        assert list(
            (loopback_app.root / ".learnnest" / "trash" / "tasks").glob(
                "*/task/task.json"
            )
        )
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("button[data-filter='trash']").click()
        expect(page.locator("#trash-list article")).to_have_count(1)
        expect(page.locator("#trash-list article")).to_contain_text("恢复任务")
        with page.expect_response(
            lambda response: response.url.endswith("/restore")
        ) as restored:
            page.locator("#trash-list button[data-restore-bundle]").click()
        assert restored.value.status == 200
        expect(page.locator("#trash-list article")).to_have_count(0)
        page.locator("button[data-filter='all']").click()
        expect(page.locator("#task-list article[data-item-ref]")).to_have_count(1)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        assert console_issues == []
        browser.close()


def test_task_workspace_can_batch_move_and_permanently_delete_tasks(
    loopback_app: _LoopbackApp,
) -> None:
    first = _task(
        loopback_app.root,
        "https://www.bilibili.com/video/BV1batch001",
        8901,
    )
    second = _task(
        loopback_app.root,
        "https://www.bilibili.com/video/BV1batch002",
        8902,
    )
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    console_issues: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.on(
            "console",
            lambda message: (
                console_issues.append(message.text)
                if message.type in {"error", "warning"}
                else None
            ),
        )
        page.goto(loopback_app.url)
        expect(page.locator("#task-list article[data-item-ref]")).to_have_count(2)
        page.locator("#task-select-visible").check()
        expect(page.locator("#task-selection-count")).to_have_text("已选 2 项")
        expect(page.locator('[data-batch-task-action="trash"]')).to_be_visible()
        page.locator('[data-batch-task-action="trash"]').click()
        expect(page.locator("#batch-task-dialog")).to_be_visible()
        expect(page.locator("#batch-task-title")).to_have_text("移入 2 项任务？")
        page.locator("#confirm-batch-task").click()
        expect(page.locator("#task-list article[data-item-ref]")).to_have_count(0)
        expect(page.locator("#notice")).to_contain_text("2 项任务已移入回收区")

        page.locator("button[data-filter='trash']").click()
        expect(page.locator("#trash-list article")).to_have_count(2)
        expect(page.locator("#trash-list button[data-purge-bundle]")).to_have_count(2)
        page.locator("#task-select-visible").check()
        expect(page.locator("#task-selection-count")).to_have_text("已选 2 项")
        batch_actions = page.locator(
            "#task-selection-bar .task-selection-actions button:visible"
        )
        expect(batch_actions).to_have_count(2)
        expect(batch_actions.nth(0)).to_have_attribute(
            "data-batch-task-action", "purge"
        )
        expect(batch_actions.nth(1)).to_have_attribute(
            "data-batch-task-action", "restore"
        )
        page.locator('[data-batch-task-action="purge"]').click()
        expect(page.locator("#batch-task-dialog")).to_be_visible()
        expect(page.locator("#batch-task-title")).to_have_text("永久删除 2 项任务？")
        expect(page.locator("#batch-task-copy")).to_contain_text("无法恢复")
        page.locator("#confirm-batch-task").click()
        expect(page.locator("#trash-list article")).to_have_count(0)
        expect(page.locator("#notice")).to_contain_text("已永久删除 2 项任务")
        assert not list(
            (loopback_app.root / ".learnnest" / "trash" / "tasks").glob("*")
        )
        assert first.task_id != second.task_id
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        assert console_issues == []
        browser.close()


def test_task_workspace_can_batch_pause_and_resume_tasks(
    loopback_app: _LoopbackApp,
) -> None:
    first = _task(
        loopback_app.root,
        "https://www.bilibili.com/video/BV1batchpause1",
        8911,
    )
    second = _task(
        loopback_app.root,
        "https://www.bilibili.com/video/BV1batchpause2",
        8912,
    )
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        page.locator("#task-select-visible").check()
        expect(page.locator('[data-batch-task-action="pause"]')).to_be_visible()
        page.locator('[data-batch-task-action="pause"]').click()
        expect(page.locator("#notice")).to_contain_text("已暂停 2 项任务")
        expect(page.locator('[data-filter-count="paused"]')).to_have_text("2")

        page.locator("button[data-filter='paused']").click()
        page.locator("#task-select-visible").check()
        expect(page.locator('[data-batch-task-action="resume"]')).to_be_visible()
        page.locator('[data-batch-task-action="resume"]').click()
        expect(page.locator("#notice")).to_contain_text("已恢复 2 项任务")
        expect(page.locator('[data-filter-count="paused"]')).to_have_text("0")
        for task in (first, second):
            found = find_task_by_id(loopback_app.root, task.task_id)
            assert found is not None
            assert (
                load_task_control(found[0], found[1].task_id).manually_paused is False
            )
        browser.close()


def test_w2_failed_source_shows_stage_reason_and_exact_next_step(
    loopback_app: _LoopbackApp,
) -> None:
    task = _task(
        loopback_app.root,
        "https://www.bilibili.com/video/BV1xx411c7mD",
        8800,
    )
    failed_at = datetime.now(UTC)
    failure_summary = "URL 下载失败；可手动下载视频后按本地文件处理 (DownloadError)"
    failed = task.model_copy(
        update={
            "stages": {"source": StageStatus.FAILED},
            "artifacts": {},
            "error_summary": failure_summary,
            "attempts": [
                TaskAttempt(
                    attempt_id="failed-source-1",
                    ordinal=1,
                    reason="initial",
                    from_stage="source",
                    status="failed",
                    started_at=failed_at,
                    finished_at=failed_at,
                    failed_stage="source",
                    executed_stages=["source"],
                    failure=FailureInfo(
                        code="manual_error",
                        category="pipeline",
                        disposition="manual",
                        safe_summary=failure_summary,
                    ),
                )
            ],
        }
    )
    write_task_atomic(
        loopback_app.root / "视频学习素材" / task.task_id,
        failed,
    )
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    console_issues: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.on(
            "console",
            lambda message: (
                console_issues.append(message.text)
                if message.type in {"error", "warning"}
                else None
            ),
        )
        page.goto(loopback_app.url)
        expect(
            page.locator('#task-list article[data-bucket="processing"] .learning-state')
        ).to_have_text("处理已停止")
        row = _first_task_row(page, "processing-list")
        expect(row.locator(".row-problem")).to_contain_text("失败阶段")
        expect(row.locator(".row-problem")).to_contain_text("获取内容")
        expect(row.locator(".row-problem")).to_contain_text("来源链接的视频下载失败")
        expect(row.locator(".progress-ring strong")).to_have_text("0")
        expect(row.locator("button[data-action='open_single_video']")).to_have_text(
            "选择本地视频"
        )
        row.locator("button[data-action='open_single_video']").click()
        expect(page.locator("#single-video-dialog")).to_be_visible()
        assert console_issues == []
        browser.close()


def test_goal4_audio_success_is_playable_from_the_real_note_page(
    loopback_app: _LoopbackApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    import learnnest.automation_delivery as delivery
    import learnnest.learning_workspace as workspace_module

    monkeypatch.setattr(
        delivery,
        "probe_audio",
        lambda _path: {"streams": [{"codec_type": "audio", "codec_name": "mp3"}]},
    )
    monkeypatch.setattr(
        delivery,
        "convert_wav_to_mp3",
        lambda _source, destination: destination.write_bytes(b"offline-mp3"),
    )
    monkeypatch.setattr(
        workspace_module,
        "probe_audio",
        lambda _path: {
            "streams": [{"codec_type": "audio", "codec_name": "mp3"}],
            "format": {"duration": "1.0"},
        },
    )
    from learnnest.learning_workspace import LearningWorkspace

    original_audio = LearningWorkspace.audio
    audio_errors: list[str] = []

    def recording_audio(self: LearningWorkspace, item_ref: str) -> Path:
        try:
            return original_audio(self, item_ref)
        except Exception as error:
            audio_errors.append(f"{type(error).__name__}: {error}")
            raise

    monkeypatch.setattr(LearningWorkspace, "audio", recording_audio)
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _enable_audio_automation(page)
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        page.locator('button[form="url-form"]').click()
        expect(page.locator('#task-list article[data-bucket="library"]')).to_have_count(
            1
        )
        expect(
            page.locator('#task-list article[data-bucket="library"]')
        ).to_contain_text("\u53ef\u64ad\u653e")
        found = find_task_by_id(loopback_app.root, "20260813-goal40001")
        assert found is not None
        task_dir, task = found
        audio_json = task_dir / next(
            path for path in task.artifacts["tts"] if Path(path).name == "audio.json"
        )
        audio_metadata = json.loads(audio_json.read_text(encoding="utf-8"))
        published = loopback_app.root / audio_metadata["published_path"]
        marker = published.with_suffix(".learnnest.json")
        assert published.is_file()
        assert marker.is_file()
        snapshot = page.evaluate(
            "async () => await (await fetch('/api/learning/snapshot')).json()"
        )
        row = _first_task_row(page, "library-list")
        assert row.locator("button[data-action='open_note']").is_enabled(), json.dumps(
            {"snapshot": snapshot, "audio_errors": audio_errors}, ensure_ascii=False
        )
        with page.expect_popup() as note:
            row.locator("button[data-action='open_note']").click()
        expect(
            note.value.locator("audio[aria-label='播放本篇笔记的音频']")
        ).to_have_count(1)
        assert loopback_app.provider_runs == [("20260813-goal40001",)]
        browser.close()


def test_goal4_audio_failure_keeps_the_note_readable_and_distinct(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    loopback_app.audio_failures.append(True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _enable_audio_automation(page)
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        page.locator('button[form="url-form"]').click()
        expect(page.locator('#task-list article[data-bucket="library"]')).to_have_count(
            1
        )
        expect(
            page.locator('#task-list article[data-bucket="library"]')
        ).to_contain_text("音频仍在处理中")
        row = _first_task_row(page, "library-list")
        with page.expect_popup() as note:
            row.locator("button[data-action='open_note']").click()
        expect(note.value.locator("article.note-content")).to_contain_text(
            "离线浏览器笔记"
        )
        assert note.value.locator("audio").count() == 0
        browser.close()


def test_goal4_retryable_writer_failure_retries_the_same_task_record(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    loopback_app.writer_failures.append("retryable")
    loopback_app.clock[0] = datetime.now(UTC) - timedelta(minutes=2)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _enable_note_automation(page, key="retry-key")
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        page.locator('button[form="url-form"]').click()
        expect(
            page.locator('#task-list article[data-bucket="processing"]')
        ).to_contain_text("处理已停止")
        row = _first_task_row(page, "processing-list")
        expect(row.locator("button[data-action='retry_automation']")).to_have_count(1)
        task_id = next(
            (loopback_app.root / ".learnnest" / "automation" / "intake").glob("*.json")
        ).stem
        loopback_app.clock[0] = datetime.now(UTC)
        row.locator("button[data-action='retry_automation']").click()
        expect(page.locator('#task-list article[data-bucket="library"]')).to_have_count(
            1
        )
        status = load_status(loopback_app.root)
        assert status is not None
        state = load_task_state(loopback_app.root, task_id, status.policy_sha256)
        assert state is not None
        assert [
            (attempt.stage, attempt.attempt, attempt.status)
            for attempt in state.attempts
        ] == [
            ("writer", 1, "failed"),
            ("writer", 2, "completed"),
            ("reviewer", 1, "completed"),
        ]
        assert len(loopback_app.provider_runs) == 2
        browser.close()


def test_goal4_retryable_source_failure_reuses_the_same_durable_browser_job(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    loopback_app.source_failures[0] = 1
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        page.locator('button[form="url-form"]').click()
        expect(page.locator(".source-jobs button[data-retry-job]")).to_have_count(1)
        _open_view(page, "sources")
        retry = page.locator(".source-jobs button[data-retry-job]")
        job_id = retry.get_attribute("data-retry-job")
        assert job_id is not None
        retry.click()
        expect(page.locator(".source-jobs")).to_contain_text("材料已加入收件箱")
        assert page.locator(".source-jobs button[data-retry-job]").count() == 0
        assert (
            loopback_app.root / ".learnnest" / "web" / "jobs" / f"{job_id}.json"
        ).is_file()
        assert loopback_app.provider_runs == []
        browser.close()


@pytest.mark.parametrize("failure", ["timeout", "permanent"])
def test_goal4_unknown_and_permanent_automation_failures_hide_retry_and_stop_factory(
    loopback_app: _LoopbackApp, failure: str
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    loopback_app.writer_failures.append(failure)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _enable_note_automation(page, key="test-key")
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        page.locator('button[form="url-form"]').click()
        expect(
            page.locator('#task-list article[data-bucket="processing"]')
        ).to_contain_text("处理已停止")
        row = _first_task_row(page, "processing-list")
        reason = row.locator(".row-problem")
        expect(reason).to_be_visible()
        expect(reason).to_contain_text("失败阶段")
        expect(reason).to_contain_text("生成笔记初稿")
        expect(reason).to_contain_text(
            "返回结果无法确认" if failure == "timeout" else "模型服务返回 HTTP 400"
        )
        assert row.locator("button[data-action='retry_automation']").count() == 0
        assert len(loopback_app.provider_runs) == 1
        status = load_status(loopback_app.root)
        assert status is not None
        intake = next(
            (loopback_app.root / ".learnnest" / "automation" / "intake").glob("*.json")
        )
        task_id = intake.stem
        state = load_task_state(loopback_app.root, task_id, status.policy_sha256)
        assert state is not None
        assert state.blocked_reason == (
            "unknown_result" if failure == "timeout" else "non_retryable_failure"
        )
        assert load_intake(loopback_app.root, task_id).status == "needs_attention"
        browser.close()


def test_douyin_url_admission_reaches_tasks_or_actionable_error_in_real_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import learnnest.web_app as web_app

    created = [0]

    def fake_process(
        source_item: object, root: Path, profile: str, **_: object
    ) -> object:
        del profile
        created[0] += 1
        return _task(root, "douyin-input", created[0])

    monkeypatch.setattr(web_app, "process_source", fake_process)

    def fail_everything_short_resolver(_short: str) -> str:
        raise RuntimeError("ACCT_SENTINEL unreachable")

    def dispatch_short_resolver(short: str) -> str:
        if short == "https://v.douyin.com/SuccessCode":
            return "https://www.douyin.com/video/987654321"
        raise RuntimeError("ACCT_SENTINEL unreachable")

    url, server, thread = _start_loopback_server(
        tmp_path,
        lambda *_args, **_kwargs: [],
        douyin_short_resolver=dispatch_short_resolver,
    )
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(edge), headless=True
            )
            for viewport in (
                {"width": 1440, "height": 1000},
                {"width": 390, "height": 844},
            ):
                console_issues: list[str] = []
                jobs_so_far = [0]

                def record_console(message: object) -> None:
                    # Rejected douyin submissions are an expected 400 contract; the
                    # browser logs those failed fetches as network noise, not app
                    # errors. Only real JS console errors/warnings count here.
                    if message.type in {
                        "error",
                        "warning",
                    } and not message.text.startswith("Failed to load resource"):
                        console_issues.append(message.text)

                page = browser.new_page(viewport=viewport)
                page.on("console", record_console)
                page.goto(url)
                _open_view(page, "sources")

                def submit(raw: str) -> None:
                    page.locator("#public-url").fill(raw)
                    page.locator('button[form="url-form"]').click()

                submit("https://www.douyin.com/user/MS4wLjABAAAA")
                expect(page.locator("#notice")).to_contain_text("视频 ID")

                submit("https://v.douyin.com/ACCT_SENTINEL")
                expect(page.locator("#notice")).to_contain_text("短链接解析失败")

                submit("https://douyin.com.evil.com/video/123")
                expect(page.locator("#notice")).to_contain_text("官方域名")

                submit("https://v.douyin.com/SuccessCode")
                expect(page.locator("#notice")).to_have_text(
                    "已加入收件箱，正在整理材料。"
                )
                expect(page.locator('[data-view-panel="tasks"]')).to_be_visible()
                expect(page.locator(".source-jobs .source-job")).to_have_count(
                    jobs_so_far[0] + 1
                )
                jobs_so_far[0] += 1
                assert created[0] >= jobs_so_far[0]

                assert "ACCT_SENTINEL" not in page.locator("body").inner_text()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                assert console_issues == []
                page.close()
            browser.close()
    finally:
        _stop_loopback_server(server, thread)


def _timestamped_task(
    root: Path,
    name: str,
    task_id: str,
    *,
    created: datetime,
    completed: datetime | None = None,
    failure: bool = False,
) -> str:
    """Write one sortable task fact (ready note or in-progress failure)."""
    task_dir = root / "视频学习素材" / name
    title = f"时间任务 {name}"
    task = create_task(
        task_id=task_id,
        source_path=f"C:/private/{name}.mp4",
        source_fingerprint=name,
        title=title,
        profile="note",
        now=created,
    ).model_copy(
        update={
            "stages": (
                {"note": StageStatus.FAILED}
                if failure
                else {"publish": StageStatus.COMPLETED}
            ),
            "artifacts": (
                {}
                if failure
                else {"publish": ["note.md"], "content_pack": ["content_pack.json"]}
            ),
            "error_summary": "note 遇到临时错误。" if failure else None,
        }
    )
    task_dir.mkdir(parents=True)
    if not failure:
        (task_dir / "note.md").write_text(
            f"<!-- learnnest-task-id: {task_id} -->\n\n# {title}\n",
            encoding="utf-8",
        )
    write_task_atomic(task_dir, task, now=created)
    if completed is not None:
        write_task_atomic(
            task_dir,
            complete_task_goal(load_task(task_dir), now=completed),
            now=completed,
        )
    return task_id


def _first_task_ref(page: Page) -> str | None:
    return page.locator(
        "#task-list article[data-item-ref]:not([hidden])"
    ).first.get_attribute("data-item-ref")


def test_task_times_global_sort_and_row_text_in_real_edge(
    loopback_app: _LoopbackApp,
) -> None:
    """W5 lifecycle: newest/oldest global sort, completed/in-progress rows,
    refresh stability, 1440x1000 and 390x844 without horizontal overflow."""
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    root = loopback_app.root
    t1 = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)
    t2 = datetime(2026, 9, 7, 3, 0, tzinfo=UTC)
    t3 = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
    completed1 = _timestamped_task(
        root,
        "a-oldest",
        "20260907-tsort01",
        created=t1,
        completed=t1 + timedelta(minutes=30),
    )
    _timestamped_task(
        root,
        "b-middle",
        "20260907-tsort02",
        created=t2,
        completed=t2 + timedelta(minutes=20),
    )
    newest = _timestamped_task(
        root, "z-newest-in-progress", "20260907-tsort03", created=t3, failure=True
    )
    periods = (t1, t2, t3)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        console_issues: list[str] = []
        page.on(
            "console",
            lambda message: (
                console_issues.append(message.text)
                if message.type in {"error", "warning"}
                else None
            ),
        )
        page.goto(loopback_app.url)
        deadline = time.monotonic() + 15
        while (
            page.locator("#task-list article[data-item-ref]").count() != 3
            and time.monotonic() < deadline
        ):
            page.wait_for_timeout(100)
        assert page.locator("#task-list article[data-item-ref]").count() == 3

        # Default order is newest created first across every state.
        assert _first_task_ref(page) == newest
        expect(
            page.locator('#task-list article[data-filter="completed"]')
        ).to_have_count(2)
        expect(
            page.locator('#task-list article[data-filter="attention"]')
        ).to_have_count(1)

        # Completed rows show created + completed + elapsed; in-progress shows elapsed only.
        completed_row = page.locator(
            '#task-list article[data-filter="completed"] small.task-times'
        ).first
        expect(completed_row).to_contain_text("创建")
        expect(completed_row).to_contain_text("完成")
        expect(completed_row).to_contain_text("历时")
        in_progress_row = page.locator(
            '#task-list article[data-filter="attention"] small.task-times'
        )
        expect(in_progress_row).to_contain_text("创建")
        expect(in_progress_row).to_contain_text("已历时")
        expect(in_progress_row).not_to_contain_text("完成")

        # Switching to oldest created must clear the batch selection.
        page.locator("#task-list article[data-item-ref]:not([hidden])").first.locator(
            "input[data-task-select]"
        ).check()
        expect(page.locator("#task-selection-bar")).to_be_visible()
        page.locator("button[data-task-sort='asc']").click()
        expect(page.locator("#task-selection-bar")).to_be_hidden()
        assert _first_task_ref(page) == completed1
        rows = page.locator("#task-list article[data-item-ref]:not([hidden])")
        assert [rows.nth(i).get_attribute("data-item-ref") for i in range(3)] == [
            completed1,
            "20260907-tsort02",
            newest,
        ]

        # Back to newest, then verify stability after a full reload.
        page.locator("button[data-task-sort='desc']").click()
        assert _first_task_ref(page) == newest
        page.reload()
        deadline = time.monotonic() + 15
        while (
            page.locator("#task-list article[data-item-ref]").count() != 3
            and time.monotonic() < deadline
        ):
            page.wait_for_timeout(100)
        assert _first_task_ref(page) == newest

        # 1440x1000 has no horizontal overflow; neither does 390x844.
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        snapshot = page.evaluate(
            "async () => await (await fetch('/api/learning/snapshot')).json()"
        )
        created_times = [
            entry["created_at"]
            for entry in [
                *snapshot["inbox"],
                *snapshot["processing"],
                *snapshot["library"],
            ]
        ]
        assert created_times == [period.isoformat() for period in reversed(periods)]
        assert console_issues == []
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        assert _first_task_ref(page) == newest
        assert console_issues == []
        browser.close()
