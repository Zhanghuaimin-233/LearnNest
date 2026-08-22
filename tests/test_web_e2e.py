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

from learnnest.automation_coordinator import AutomationCoordinator
from learnnest.automation_runner import AutomationProviders
from learnnest.automation_runner import run_automation_tasks
from learnnest.automation_store import load_intake, load_status, load_task_state
from learnnest.douyin_favorites import DouyinFavorite, DouyinFavoritesSnapshot
from learnnest.models import ContentPack, Evidence, StageStatus
from learnnest.pipeline import PipelineError
from learnnest.task_store import create_task, find_task_by_id, write_task_atomic
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
                douyin_favorites=_HistoricalFavorites(),
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


def _open_first_task(page: Page, list_id: str) -> None:
    page.locator(f"#{list_id} button[data-open-item-ref]").first.click()
    expect(page.locator("#task-detail-view")).to_be_visible()


def _open_settings_panel(page: Page, panel: str) -> None:
    _open_view(page, "settings")
    page.locator(f'[data-settings-tab="{panel}"]').click()
    expect(page.locator(f'[data-settings-panel="{panel}"]')).to_be_visible()


def _add_connection(page: Page, *, name: str, preset: str, key: str) -> None:
    _open_settings_panel(page, "connections")
    page.locator("#open-connection-dialog").click()
    expect(page.locator("#connection-dialog")).to_be_visible()
    page.locator("#provider-connection-form [name=name]").fill(name)
    page.locator("#provider-connection-form [name=preset]").select_option(preset)
    page.locator("#provider-connection-form [name=api_key]").fill(key)
    page.locator("#provider-connection-form button[type=submit]").click()
    expect(page.locator("#connection-dialog")).not_to_be_visible()
    expect(page.get_by_text(name, exact=True)).to_be_visible()


def _enable_note_automation(page: Page, *, key: str) -> None:
    _add_connection(page, name="offline-note", preset="mimo", key=key)
    expect(page.get_by_text("offline-note", exact=True)).to_be_visible()
    for role in ("\u7b14\u8bb0 Writer", "\u7b14\u8bb0 Reviewer"):
        page.locator(f'select[data-setup-role-select="{role}"]').select_option(
            "offline-note"
        )
        page.locator(f'button[data-bind-setup-role="{role}"]').click()
        expect(page.locator("#provider-feedback")).to_contain_text("\u5df2\u7ed1\u5b9a")
    _open_settings_panel(page, "output")
    page.locator("#automation-form [name=default_output]").select_option(
        "complete_note"
    )
    page.locator("#automation-form button[type=submit]").click()
    expect(page.locator("#automation-state")).to_have_text("\u7b49\u5f85\u786e\u8ba4")
    page.locator("#confirm-paid").check()
    page.locator("#authorize-automation").click()
    expect(page.locator("#automation-state")).to_have_text(
        "\u81ea\u52a8\u6574\u7406\u5df2\u5f00\u542f"
    )


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
    expect(page.locator("#automation-state")).to_have_text("\u7b49\u5f85\u786e\u8ba4")
    _open_settings_panel(page, "connections")
    for _ in range(2):
        select = page.locator("select[data-setup-role-select]").first
        label = select.get_attribute("data-setup-role-select")
        assert label is not None
        select.select_option(
            "offline-podcast" if label == "\u64ad\u5ba2" else "offline-tts"
        )
        page.locator(f'button[data-bind-setup-role="{label}"]').click()
        expect(page.locator("#provider-feedback")).to_contain_text("\u5df2\u7ed1\u5b9a")
    expect(page.locator("button[data-unbind-setup-role]")).to_have_count(4)
    _open_settings_panel(page, "output")
    page.locator("#confirm-paid").check()
    with page.expect_response(
        lambda response: response.url.endswith("/api/automation/authorize")
    ) as authorized:
        page.locator("#authorize-automation").click()
    assert authorized.value.status == 200
    expect(page.locator("#automation-state")).to_have_text(
        "\u81ea\u52a8\u6574\u7406\u5df2\u5f00\u542f"
    )


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

    monkeypatch.setattr(
        web_app,
        "process_video",
        lambda source, root, profile: process(source, root, profile),
    )
    monkeypatch.setattr(
        web_app,
        "process_source",
        lambda source, root, profile: process(source, root, profile),
    )
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

    url, server, thread = _start_loopback_server(tmp_path, run_tasks, lambda: clock[0])
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
        video = tmp_path / "lesson.mp4"
        video.write_bytes(b"offline video")
        _open_view(page, "tasks")
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
            page.locator("#url-form button").click()
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
            page.locator("#library-list article").count() != 3
            and time.monotonic() < deadline
        ):
            page.wait_for_timeout(100)
        snapshot = page.evaluate(
            "async () => await (await fetch('/api/learning/snapshot')).json()"
        )
        assert page.locator("#library-list article").count() == 3, json.dumps(
            snapshot, ensure_ascii=False
        )
        _open_first_task(page, "library-list")
        with page.expect_popup() as note:
            page.locator("#task-detail button[data-action='open_note']").click()
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
        page.locator("#authorize-automation").click()
        expect(page.locator("#notice")).to_contain_text("请先勾选付费确认")
        assert loopback_app.provider_runs == []
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        with page.expect_response(
            lambda response: response.url.endswith("/api/learning/submit")
        ) as submitted:
            page.locator("#url-form button").click()
        assert submitted.value.status == 202
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        expect(page.locator("#processing-list")).to_contain_text("请先完成整理设置")
        page.reload()
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        expect(page.locator("#processing-list")).to_contain_text("请先完成整理设置")
        assert loopback_app.provider_runs == []
        assert page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
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
        for role in ("笔记 Writer", "笔记 Reviewer"):
            page.locator(f'select[data-setup-role-select="{role}"]').select_option(
                "mimo-note"
            )
            page.locator(f'button[data-bind-setup-role="{role}"]').click()
            expect(page.locator("#provider-feedback")).to_contain_text("已绑定")

        expect(page.locator('select[data-setup-role-select="播客"]')).to_have_count(0)
        expect(page.locator("#setup-readiness")).to_contain_text(
            "播客需要单独的 MiMo/DeepSeek 连接，不能复用笔记连接。"
        )

        _add_connection(page, name="mimo-podcast", preset="mimo", key="same-key")
        podcast_select = page.locator('select[data-setup-role-select="播客"]')
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
        select = page.locator("select[data-setup-role-select]").first
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
        page.evaluate("window.__releaseProviderPoll()")
        page.evaluate("window.__providerPoll")

        expect(page.locator("select[data-setup-role-select]").first).to_have_value(
            "offline-note"
        )
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
        page.locator("button[data-unbind-setup-role]").first.click()
        expect(page.locator("select[data-setup-role-select]").first).to_be_visible()
        page.locator("select[data-setup-role-select]").first.select_option(
            "replacement-note"
        )
        page.locator("button[data-bind-setup-role]").first.click()
        expect(page.locator("#automation-state")).to_have_text(
            "\u9700\u8981\u91cd\u65b0\u786e\u8ba4"
        )
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        page.locator("#url-form button").click()
        expect(page.locator("#processing-list")).to_contain_text("确认授权")
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
        page.locator("#url-form button").click()
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        expect(page.locator("#processing-list")).to_contain_text("请先完成整理设置")
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
        expect(restored.locator("#processing-list")).to_contain_text("请先完成整理设置")
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
            page.locator("#url-form button").click()
        assert submitted.value.status == 202
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        expect(page.locator("#processing-list")).to_contain_text("请先完成整理设置")
        intake = next(
            (loopback_app.root / ".learnnest" / "automation" / "intake").glob("*.json")
        )
        original = intake.read_bytes()
        intake.write_text("{broken", encoding="utf-8")
        page.reload()
        expect(page.locator("#processing-list")).to_contain_text("需要你处理")
        intake.write_bytes(original)
        page.reload()
        expect(page.locator("#processing-list")).to_contain_text("请先完成整理设置")
        assert loopback_app.provider_runs == []
        browser.close()


def test_goal4_user_can_move_one_stopped_task_to_trash(
    loopback_app: _LoopbackApp,
) -> None:
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(loopback_app.url)
        _open_view(page, "sources")
        page.locator("#public-url").fill("https://www.bilibili.com/video/BV1xx411c7mD")
        page.locator("#url-form button").click()
        expect(page.locator(".source-jobs .source-job")).to_have_count(1)
        _open_view(page, "tasks")
        expect(page.locator("#task-list article")).to_have_count(1)
        page.locator("#task-list article").click()
        page.locator("#task-detail button[data-delete-item-ref]").click()
        expect(page.locator("#delete-task-dialog")).to_be_visible()
        with page.expect_response(
            lambda response: (
                response.request.method == "DELETE"
                and "/api/learning/items/" in response.url
            )
        ) as deleted:
            page.locator("#confirm-delete-task").click()
        assert deleted.value.status == 200
        expect(page.locator("#task-list article")).to_have_count(0)
        expect(page.locator("#notice")).to_contain_text("已移入回收区")
        assert loopback_app.provider_runs == []
        assert list(
            (loopback_app.root / ".learnnest" / "trash" / "tasks").glob(
                "*/task/task.json"
            )
        )
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
        page.locator("#url-form button").click()
        expect(page.locator("#library-list article")).to_have_count(1)
        expect(page.locator("#library-list")).to_contain_text("\u53ef\u64ad\u653e")
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
        _open_first_task(page, "library-list")
        assert page.locator("#task-detail audio").count() == 1, json.dumps(
            {"snapshot": snapshot, "audio_errors": audio_errors}, ensure_ascii=False
        )
        with page.expect_popup() as note:
            page.locator("#task-detail button[data-action='open_note']").click()
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
        page.locator("#url-form button").click()
        expect(page.locator("#library-list article")).to_have_count(1)
        expect(page.locator("#library-list")).to_contain_text("音频仍在处理中")
        _open_first_task(page, "library-list")
        assert page.locator("#task-detail audio").count() == 0
        with page.expect_popup() as note:
            page.locator("#task-detail button[data-action='open_note']").click()
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
        page.locator("#url-form button").click()
        expect(page.locator("#processing-list")).to_contain_text("需要你处理")
        _open_first_task(page, "processing-list")
        expect(
            page.locator("#task-detail button[data-action='retry_automation']")
        ).to_have_count(1)
        task_id = next(
            (loopback_app.root / ".learnnest" / "automation" / "intake").glob("*.json")
        ).stem
        loopback_app.clock[0] = datetime.now(UTC)
        page.locator("#task-detail button[data-action='retry_automation']").click()
        expect(page.locator("#library-list article")).to_have_count(1)
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
        page.locator("#url-form button").click()
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
        page.locator("#url-form button").click()
        expect(page.locator("#processing-list")).to_contain_text(
            "\u9700\u8981\u4f60\u5904\u7406"
        )
        _open_first_task(page, "processing-list")
        assert (
            page.locator("#task-detail button[data-action='retry_automation']").count()
            == 0
        )
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
