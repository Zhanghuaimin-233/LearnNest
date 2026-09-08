"""Loopback-only WebUI for deterministic LearnNest task operations."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterable
from datetime import UTC, datetime
import re
import threading
import uuid
from mimetypes import guess_type
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote, urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.automation_models import AutomationBudget, AutomationPolicy
from learnnest.automation_coordinator import (
    AutomationCoordinator,
    create_intake_for_task,
)
from learnnest.automation_store import (
    authorize as authorize_automation,
    disable as disable_automation,
    find_intake,
    load_task_state,
    load_status as load_automation_status,
    save_intake,
    save_policy as save_automation_policy,
    save_task_state,
)
from learnnest.adapters.douyin_http import (
    DouyinAuthenticationError,
    douyin_authentication_message,
)
from learnnest.douyin_favorites import (
    DEFAULT_DOUYIN_FOLDER_ID,
    DouyinFavoritesError,
    DouyinFavoritesSnapshot,
    DouyinFavoritesStore,
)
from learnnest.douyin_cookie_store import DouyinCookieStore
from learnnest.douyin_login import DouyinLoginError, DouyinLoginSessionManager
from learnnest.downloader import YtDlpDownloader
from learnnest.douyin_url import (
    DouyinUrlError,
    ShortUrlResolver,
    canonicalize_douyin_url,
    is_douyin_canonical_url,
)
from learnnest.execution import plan_recovery
from learnnest.learning_workspace import (
    LearningItem,
    LearningWorkspace,
    LearningWorkspaceError,
    validate_public_web_url,
)
from learnnest.launcher import (
    LauncherConfigError,
    launcher_config_path as default_launcher_config_path,
    load_launcher_config,
    save_launcher_model_root,
    save_launcher_output_root,
)
from learnnest.locks import LockUnavailable, douyin_sync_lock, task_lock
from learnnest.local_models import (
    LocalModelError,
    LocalModelNotFoundError,
    LocalModelService,
)
from learnnest.models import StageStatus, TaskRecord
from learnnest.pipeline import PipelineError, process_source, process_video, rerun_task
from learnnest.provider_profiles import (
    ProviderBudgetGroup,
    ProviderConnectionBoundError,
    ProviderConnectionNotFoundError,
    ProviderRole,
    ProviderRoleNotBoundError,
    clear_role_binding,
    connection_presets,
    connect as connect_provider,
    delete_connection,
    freeze_role_bindings,
    get_connection,
    load_settings,
    public_settings as public_provider_settings,
    set_role_binding,
    settings_sha256,
    update_limits,
)
from learnnest.provider_model_catalog import (
    ModelCatalogPreview,
    ProviderModelCatalogError,
)
from learnnest.provider_service import (
    ProviderConnectionCheckError,
    fetch_provider_model_catalog,
    preview_provider_model_catalog,
    run_provider_connection_check,
    save_provider_model,
)
from learnnest.tts_providers import (
    TtsProviderError,
    default_windows_tts_voice,
    list_windows_tts_voices,
)
from learnnest.sources import SourceParseError, collect_sources
from learnnest.task_store import find_task_by_id, load_task
from learnnest.task_trash import (
    TaskTrashError,
    list_trashed_tasks,
    purge_trashed_task,
    restore_trashed_task,
    trash_task,
)
from learnnest.web_jobs import WebJob, WebJobStore
from learnnest.learning_state import (
    automation_budget_restart_is_due,
    automation_restart_is_safe,
    automation_retry_admission,
    public_connection_readability,
    public_provider_roles,
    public_provider_label,
    public_setup_readiness,
    required_automation_roles,
)

_STATIC_DIRECTORY = Path(__file__).parent / "static"
_WINDOWS_PATH = re.compile(r"(?i)\b[a-z]:[\\/][^\r\n]*")
_MARKDOWN = MarkdownIt("commonmark", {"html": False})
_IMAGE_SUFFIXES = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
_VIDEO_SUFFIXES = {".avi", ".mkv", ".mov", ".mp4", ".mpeg", ".webm"}
_SETUP_ROLE_NAMES = {
    "笔记 Writer": "note_writer",
    "笔记 Reviewer": "note_reviewer",
    "播客": "podcast",
    "TTS": "tts",
    "语音识别（ASR）": "asr",
    "画面文字（OCR）": "ocr",
}
_CAPABILITY_LABELS = {
    "llm": "文本模型",
    "tts": "语音服务",
    "asr": "语音识别",
    "ocr": "画面文字识别",
}
_PROVIDER_ROLE_ORDER = tuple(_SETUP_ROLE_NAMES.values())
_PROVIDER_ROLE_LABELS = {role: label for label, role in _SETUP_ROLE_NAMES.items()}


class ProcessRequest(BaseModel):
    """One source submitted from the local task workspace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=4096)


class LearningItemRequest(BaseModel):
    """One human-facing request to add a learning item."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=4096)
    desired_output: Literal["readable_note", "materials_only"] = "readable_note"


class DouyinFavoritesSyncRequest(BaseModel):
    """A local login-session handle that never contains credentials."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=16, max_length=128)


class DouyinFavoritesSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    aweme_ids: list[str] = Field(min_length=1, max_length=20)


_PROVIDER_PRESET_KEYS = (
    "windows-tts",
    "mimo",
    "deepseek",
    "openai",
    "kimi",
    "glm",
    "bailian",
    "ark",
    "hunyuan",
    "minimax",
    "longcat",
    "antling",
    "xai",
    "openrouter",
    "modelscope",
    "nvidia-nim",
    "anthropic",
    "gemini",
    "mimo-tts",
    "local-asr",
    "local-ocr",
)


class ProviderConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    preset: Literal[_PROVIDER_PRESET_KEYS]
    api_key: str | None = Field(default=None, min_length=1, max_length=2048)
    endpoint: str | None = Field(default=None, max_length=512)
    model: str | None = Field(default=None, max_length=128)
    voice: str | None = Field(default=None, max_length=256)


class ProviderRoleBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    connection_name: str = Field(min_length=1, max_length=64)


class ProviderLimitsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    retries_per_role: int = Field(ge=0, le=3)
    global_calls_per_day: int = Field(ge=0, le=800)
    budget_group_calls_per_day: dict[ProviderBudgetGroup, int]


class ProviderConnectionCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_paid: Literal[True]


class ProviderModelUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model: str = Field(min_length=1, max_length=128)


class ProviderPresetCatalogRequest(BaseModel):
    """One transient key used only for this catalog preview request."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    api_key: str = Field(min_length=1, max_length=2048)


class AutomationConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    default_output: Literal["complete_note", "complete_note_with_audio"]
    auto_organize_new_favorites: bool = False
    check_interval_minutes: int = Field(ge=1, le=60)
    max_items_per_tick: int = Field(ge=1, le=20)


class AutomationAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm_paid: Literal[True]


class OutputRootRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    output_root: str = Field(min_length=1, max_length=4096)


class ModelRootRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model_root: str = Field(min_length=1, max_length=4096)


class LearningSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=4096)


class WebService:
    """Adapts the existing task and pipeline services for a local browser."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        douyin_login: DouyinLoginSessionManager | None = None,
        douyin_favorites: DouyinFavoritesStore | None = None,
        coordinator: AutomationCoordinator | None = None,
        launcher_config_path: Path | None = None,
        local_models: LocalModelService | None = None,
        douyin_short_resolver: ShortUrlResolver | None = None,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.jobs = WebJobStore(self.output_root)
        self.douyin_login = douyin_login or DouyinLoginSessionManager(
            cookie_store=DouyinCookieStore(self.output_root)
        )
        self.douyin_favorites = douyin_favorites or DouyinFavoritesStore(
            self.output_root
        )
        self.coordinator = coordinator
        self.launcher_config_path = launcher_config_path
        self.local_models = local_models or LocalModelService()
        self._douyin_short_resolver = douyin_short_resolver
        self._sync_active = False
        self._sync_active_lock = threading.Lock()
        self._shutdown = False

    def wake_automation(self) -> None:
        if self.coordinator is not None:
            self.coordinator.wake()

    def default_automation_output(
        self,
    ) -> Literal["complete_note", "complete_note_with_audio"]:
        status = load_automation_status(self.output_root)
        if status is None:
            return "complete_note_with_audio"
        return status.policy.default_output

    def list_tasks(self) -> list[dict[str, Any]]:
        task_root = self.output_root / "视频学习素材"
        if not task_root.is_dir():
            return []
        tasks: list[tuple[Path, TaskRecord]] = []
        for task_json in task_root.glob("*/task.json"):
            try:
                tasks.append((task_json.parent, load_task(task_json)))
            except (OSError, ValueError):
                continue
        tasks.sort(
            key=lambda item: (
                item[1].created_at is None,
                -(
                    item[1].created_at.timestamp()
                    if item[1].created_at is not None
                    else 0.0
                ),
                item[0].name,
            )
        )
        return [_task_summary(task) for _, task in tasks]

    def task_detail(self, task_id: str) -> dict[str, Any]:
        found = find_task_by_id(self.output_root, task_id)
        if found is None:
            raise KeyError(task_id)
        task_dir, task = found
        return {
            **_task_summary(task),
            "stages": {name: status.value for name, status in task.stages.items()},
            "artifacts": _public_artifacts(task_dir, task),
        }

    def submit_process(
        self,
        source: str,
        *,
        source_kind: Literal["local_video", "public_url", "douyin_favorite"]
        | None = None,
    ) -> WebJob:
        try:
            canonical = canonicalize_douyin_url(
                source, resolver=self._douyin_short_resolver
            )
        except DouyinUrlError as error:
            raise ValueError(str(error)) from error
        candidate = canonical if canonical is not None else source
        try:
            source_item = collect_sources(
                input_value=candidate,
                douyin_short_resolver=self._douyin_short_resolver,
            )[0]
            if source_item.input_type == "url":
                validate_public_web_url(source_item.input)
        except (LearningWorkspaceError, SourceParseError) as error:
            raise ValueError(
                "输入无效，请提供存在的本地视频或公开 HTTP(S) URL。"
            ) from error

        kind = source_kind or (
            "public_url" if source_item.input_type == "url" else "local_video"
        )
        job = self.jobs.create(kind, source_item.input)

        self._start_job(job, self._process_job_runner(job))
        return job

    def submit_recovery(self, task_id: str) -> WebJob:
        found = find_task_by_id(self.output_root, task_id)
        if found is None:
            raise KeyError(task_id)
        task_dir, persisted = found

        def runner() -> str:
            with task_lock(self.output_root, persisted.task_id, timeout=0):
                recovery = plan_recovery(task_dir)
                if recovery.from_stage is None:
                    return persisted.task_id
                if recovery.requires_paid:
                    raise ValueError("恢复会进入付费阶段；请通过对应 CLI 显式执行。")
                task = rerun_task(
                    task_dir,
                    recovery.from_stage,
                    reason="resume",
                    _task_lock_held=True,
                )
                return task.task_id

        job = self.jobs.create("local_video", "")
        self._start_job(job, lambda: self.jobs.complete(job.job_id, runner()))
        return job

    def retry_source_job(self, job_id: str) -> WebJob:
        job = self.jobs.retry(job_id)
        try:
            self._start_job(job, self._process_job_runner(job))
        except Exception:
            # A second spawn failure must land back on a visible, retryable
            # failed fact instead of a never-reclaimed queued intent.
            self.jobs.fail(job.job_id, "任务未能启动，请在来源处理中重试。")
            raise ValueError("任务未能启动，请稍后重试。")
        return job

    def _douyin_runtime_downloader(self) -> YtDlpDownloader:
        session = self.douyin_login.current_session()
        session_id = session.get("session_id")
        if session.get("status") != "connected" or not isinstance(session_id, str):
            raise ValueError("抖音登录状态不可用，请在来源页重新连接后重试。")
        try:
            cookie = self.douyin_login.cookie_for(session_id)
        except DouyinLoginError as error:
            raise ValueError(
                "抖音登录状态不可用，请在来源页重新连接后重试。"
            ) from error
        return YtDlpDownloader(cookie=cookie)

    def _runtime_cookie_downloader(self) -> YtDlpDownloader | None:
        """Return a logged-in downloader only when a live session is connected.

        Re-read every attempt so a retry can reuse the login state without
        falling back to any original URL.
        """
        session = self.douyin_login.current_session()
        session_id = session.get("session_id") if isinstance(session, dict) else None
        if session.get("status") != "connected" or not isinstance(session_id, str):
            return None
        try:
            return YtDlpDownloader(cookie=self.douyin_login.cookie_for(session_id))
        except DouyinLoginError:
            return None

    def _process_job_runner(self, job: WebJob) -> Callable[[], None]:
        def runner() -> None:
            self.jobs.start(job.job_id)
            sources = collect_sources(
                input_value=job.source_input,
                douyin_short_resolver=self._douyin_short_resolver,
            )
            if len(sources) != 1:
                raise ValueError("source job input is invalid")
            source_item = sources[0]
            if source_item.input_type == "url":
                validate_public_web_url(source_item.input)
            expected_kind = (
                "public_url" if source_item.input_type == "url" else "local_video"
            )
            if job.source_kind not in {expected_kind, "douyin_favorite"}:
                raise ValueError("source job identity is invalid")
            if source_item.input_type == "local_file":
                task = process_video(
                    Path(source_item.input), self.output_root, "evidence"
                )
            elif job.source_kind == "douyin_favorite":
                task = process_source(
                    source_item,
                    self.output_root,
                    "evidence",
                    downloader=self._douyin_runtime_downloader(),
                )
            elif is_douyin_canonical_url(source_item.input):
                task = process_source(
                    source_item,
                    self.output_root,
                    "evidence",
                    downloader=self._runtime_cookie_downloader(),
                )
            else:
                task = process_source(source_item, self.output_root, "evidence")
            create_intake_for_task(
                self.output_root,
                task_id=task.task_id,
                source_kind=job.source_kind,
                default_output=self.default_automation_output(),
            )
            self.jobs.complete(job.job_id, task.task_id)
            self.wake_automation()

        return runner

    def start_automation(self, task_id: str) -> None:
        with task_lock(self.output_root, task_id, timeout=0):
            found = find_task_by_id(self.output_root, task_id)
            if found is None:
                raise KeyError(task_id)
            _task_dir, task = found
            if task.stages.get("content_pack") is not StageStatus.COMPLETED:
                raise ValueError("材料尚未准备完成。")
            try:
                intake = find_intake(self.output_root, task_id)
            except ValueError as error:
                raise ValueError(
                    "任务状态无法安全读取，请重新选择原视频处理。"
                ) from error
            if intake is None:
                create_intake_for_task(
                    self.output_root,
                    task_id=task.task_id,
                    source_kind=(
                        "public_url" if task.source_type == "url" else "local_video"
                    ),
                    default_output=self.default_automation_output(),
                )
            elif intake.status == "needs_attention":
                budget_restart = automation_budget_restart_is_due(
                    self.output_root, task_id
                )
                safe_restart = automation_restart_is_safe(self.output_root, task_id)
                if not (budget_restart or safe_restart):
                    raise ValueError("当前任务不能在原任务上安全继续。")
                previous_state = None
                if safe_restart:
                    status = load_automation_status(self.output_root)
                    if status is not None:
                        previous_state = load_task_state(
                            self.output_root, task_id, status.policy_sha256
                        )
                        if previous_state is not None:
                            save_task_state(
                                self.output_root,
                                previous_state.model_copy(
                                    update={
                                        "status": "pending",
                                        "blocked_reason": None,
                                        "failure_summary": None,
                                    }
                                ),
                            )
                try:
                    save_intake(
                        self.output_root,
                        intake.model_copy(update={"status": "pending"}),
                    )
                except (OSError, ValueError):
                    if previous_state is not None:
                        save_task_state(self.output_root, previous_state)
                    raise
            elif intake.status == "completed":
                raise ValueError("任务已经完成，不需要重新开始整理。")
        self.wake_automation()

    def retry_automation(self, task_id: str) -> None:
        try:
            with task_lock(self.output_root, task_id, timeout=0):
                admission = automation_retry_admission(self.output_root, task_id)
                if admission is None:
                    raise ValueError("当前整理不能安全重试。")
                pending_state = admission.state.model_copy(
                    update={"status": "pending", "blocked_reason": None}
                )
                pending_intake = admission.intake.model_copy(
                    update={"status": "pending"}
                )
                save_task_state(self.output_root, pending_state)
                try:
                    save_intake(self.output_root, pending_intake)
                except (OSError, ValueError) as error:
                    try:
                        save_task_state(self.output_root, admission.state)
                    except (OSError, ValueError) as rollback_error:
                        raise ValueError("当前整理不能安全重试。") from rollback_error
                    raise ValueError("当前整理不能安全重试。") from error
        except (LockUnavailable, OSError) as error:
            raise ValueError("当前整理不能安全重试。") from error
        self.wake_automation()

    def _start_job(self, job: WebJob, runner: Callable[[], None]) -> None:
        def run() -> None:
            try:
                runner()
            except Exception as error:
                self.jobs.fail(job.job_id, _safe_error_message(error))

        threading.Thread(target=run, daemon=True).start()

    async def save_uploaded_video(
        self, name: str, chunks: AsyncIterable[bytes]
    ) -> Path:
        """Stream a browser upload into this root before any task is created."""
        uploaded_name = Path(name)
        if (
            not name
            or uploaded_name.name != name
            or uploaded_name.suffix.lower() not in _VIDEO_SUFFIXES
        ):
            raise ValueError("请选择支持的视频文件。")
        directory = self.output_root / ".learnnest" / "uploads"
        directory.mkdir(parents=True, exist_ok=True)
        final = directory / f"{uuid.uuid4().hex}{uploaded_name.suffix.lower()}"
        partial = final.with_suffix(final.suffix + ".partial")
        written = 0
        try:
            with partial.open("xb") as stream:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes) or not chunk:
                        continue
                    stream.write(chunk)
                    written += len(chunk)
                stream.flush()
            if written == 0:
                raise ValueError("上传的视频为空。")
            partial.replace(final)
        except (OSError, ValueError) as error:
            partial.unlink(missing_ok=True)
            raise ValueError("视频上传未完成，请重新选择文件。") from error
        return final

    def automation_status(self) -> dict[str, Any]:
        status = load_automation_status(self.output_root)
        if status is None:
            return {
                "configured": False,
                "enabled": False,
                "paid_authorized": False,
                "auto_new_favorites_enabled": False,
                "auto_new_favorites_active": False,
            }
        authorization_valid = (
            status.policy.enabled
            and status.policy.authorized_at is not None
            and status.policy.provider_settings_sha256
            == settings_sha256(load_settings(self.output_root))
        )
        auto_new_favorites_enabled = status.policy.auto_organize_new_favorites
        return {
            "configured": True,
            "enabled": authorization_valid,
            "paid_authorized": authorization_valid,
            "auto_new_favorites_enabled": auto_new_favorites_enabled,
            "auto_new_favorites_active": (
                authorization_valid and auto_new_favorites_enabled
            ),
            "needs_authorization": status.policy.enabled and not authorization_valid,
            "schedule_id": status.policy.schedule_id,
            "default_output": status.policy.default_output,
            "auto_organize_new_favorites": auto_new_favorites_enabled,
            "check_interval_minutes": status.policy.check_interval_minutes,
            "max_items_per_tick": status.policy.max_items_per_tick,
            "authorized_at": _isoformat(status.policy.authorized_at),
            "last_tick_at": _isoformat(status.last_tick_at),
            "last_tick_summary": _safe_text(status.last_tick_summary),
        }

    def configure_automation(
        self, request: AutomationConfigureRequest
    ) -> dict[str, Any]:
        try:
            settings = load_settings(self.output_root)
            current_settings_sha256 = settings_sha256(settings)
            writer_binding = settings.role_bindings["note_writer"]
            reviewer_binding = settings.role_bindings["note_reviewer"]
            writer = _automation_connection_snapshot(
                get_connection(self.output_root, writer_binding.connection_id),
                settings_sha256=current_settings_sha256,
            )
            reviewer = _automation_connection_snapshot(
                get_connection(self.output_root, reviewer_binding.connection_id),
                settings_sha256=current_settings_sha256,
            )
            save_automation_policy(
                self.output_root,
                AutomationPolicy(
                    schedule_id=None,
                    writer=writer,
                    reviewer=reviewer,
                    default_output=request.default_output,
                    auto_organize_new_favorites=request.auto_organize_new_favorites,
                    check_interval_minutes=request.check_interval_minutes,
                    max_items_per_tick=request.max_items_per_tick,
                    retries_per_stage=settings.retries_per_role,
                    budget=AutomationBudget(
                        provider_calls_per_day=settings.global_calls_per_day,
                        budget_group_calls_per_day=settings.budget_group_calls_per_day,
                    ),
                ),
            )
        except (KeyError, ValueError) as error:
            raise ValueError("整理设置无法保存。") from error
        self.wake_automation()
        return self.automation_status()

    def authorize_automation(
        self, request: AutomationAuthorizeRequest
    ) -> dict[str, Any]:
        assert request.confirm_paid is True
        try:
            authorize_automation(self.output_root)
        except ValueError as error:
            raise ValueError("付费整理许可尚未完成设置。") from error
        self.wake_automation()
        return self.automation_status()

    def disable_automation(self) -> dict[str, Any]:
        try:
            disable_automation(self.output_root)
        except ValueError as error:
            raise ValueError("付费整理许可尚未完成设置。") from error
        return self.automation_status()

    def storage_status(self) -> dict[str, Any]:
        next_root = self.output_root
        try:
            config_path = self.launcher_config_path or default_launcher_config_path()
            if config_path.is_file():
                next_root = Path(load_launcher_config(config_path).output_root)
        except LauncherConfigError:
            next_root = self.output_root
        return {
            "current_output_root": str(self.output_root),
            "next_output_root": str(next_root),
            "restart_required": next_root != self.output_root,
        }

    def save_output_root(self, request: OutputRootRequest) -> dict[str, Any]:
        candidate = Path(request.output_root)
        if not candidate.is_absolute():
            raise ValueError("保存位置必须填写绝对路径。")
        try:
            config_path = self.launcher_config_path or default_launcher_config_path()
            save_launcher_output_root(candidate, config_path)
        except LauncherConfigError as error:
            raise ValueError("无法使用这个保存位置，请选择可写文件夹。") from error
        return self.storage_status()

    def save_model_root(self, request: ModelRootRequest) -> dict[str, object]:
        candidate = Path(request.model_root)
        if not candidate.is_absolute():
            raise ValueError("模型保存位置必须填写绝对路径。")
        try:
            config_path = self.launcher_config_path or default_launcher_config_path()

            def persist(root: Path) -> None:
                save_launcher_model_root(
                    root,
                    output_root=self.output_root,
                    config_path=config_path,
                )

            self.local_models.change_root(
                candidate,
                persist=persist,
            )
        except LauncherConfigError as error:
            raise ValueError("无法使用这个模型保存位置，请选择可写文件夹。") from error
        except OSError as error:
            raise ValueError("无法使用这个模型保存位置，请选择可写文件夹。") from error
        return self.local_models.snapshot()

    def provider_settings(
        self,
        default_output: Literal["complete_note", "complete_note_with_audio"]
        | None = None,
    ) -> dict[str, object]:
        """Return the intentionally public projection, never settings JSON itself."""
        settings = public_provider_settings(self.output_root)
        current = load_settings(self.output_root)
        adapters = connection_presets()

        def _provider_label(preset_key: str, provider: str) -> str:
            adapter = adapters.get(preset_key)
            return (adapter.display_name if adapter else None) or public_provider_label(
                provider
            )

        projected_connections: list[dict[str, object]] = []
        for item in settings["connections"]:
            name = str(item["name"])
            stored = current.connections[name]
            bound_roles = [
                _PROVIDER_ROLE_LABELS[role]
                for role in _PROVIDER_ROLE_ORDER
                if current.role_bindings.get(role) is not None
                and current.role_bindings[role].connection_id == name
            ]
            projected_connections.append(
                {
                    "name": name,
                    "provider": _provider_label(stored.preset, str(item["provider"])),
                    "state": public_connection_readability(self.output_root, stored),
                    "capability": item["capability"],
                    "model": item["model"],
                    "preset": stored.preset,
                    "local": stored.api_family == "local",
                    "bound_roles": bound_roles,
                    "deletable": not bound_roles,
                    "delete_reason": (
                        None
                        if not bound_roles
                        else f"当前用于{'、'.join(bound_roles)}，请先切换职责连接。"
                    ),
                    "voice": item["voice"],
                }
            )
        return {
            "connections": projected_connections,
            "adapters": [
                {
                    "preset": preset,
                    "name": _provider_label(preset, adapter.provider),
                    "capability": _CAPABILITY_LABELS[adapter.capability],
                    "capability_key": adapter.capability,
                    "local": not adapter.requires_secret,
                    "api_family": adapter.api_family,
                    "catalog_mode": adapter.catalog_mode,
                    "requires_model": adapter.default_model is None,
                    "key_entry": adapter.key_entry_url,
                }
                for preset, adapter in adapters.items()
            ],
            "limits": {
                "retries_per_role": settings["retries_per_role"],
                "global_calls_per_day": settings["global_calls_per_day"],
                "budget_group_calls_per_day": settings["budget_group_calls_per_day"],
            },
            "roles": public_provider_roles(self.output_root),
            "readiness": public_setup_readiness(self.output_root, default_output),
        }

    def save_provider_connection(
        self, request: ProviderConnectionRequest
    ) -> dict[str, object]:
        adapter = connection_presets().get(request.preset)
        if (
            adapter is not None
            and adapter.default_model is None
            and not (request.model or "").strip()
        ):
            raise ValueError("请先获取并选择具体模型，再保存连接。")
        try:
            connect_provider(
                self.output_root,
                name=request.name,
                preset=request.preset,
                secret_value=request.api_key,
                endpoint=request.endpoint,
                model=request.model,
                voice=request.voice,
            )
        except ValueError as error:
            raise ValueError("连接配置无法保存。") from error
        return self.provider_settings()

    def provider_model_catalog(self, connection_name: str) -> dict[str, object]:
        return {
            "models": fetch_provider_model_catalog(
                str(self.output_root), connection_name
            )
        }

    def provider_preset_model_catalog(
        self, preset: str, api_key: str
    ) -> ModelCatalogPreview:
        return preview_provider_model_catalog(preset, api_key)

    def save_provider_model(
        self, connection_name: str, request: ProviderModelUpdateRequest
    ) -> dict[str, object]:
        try:
            save_provider_model(str(self.output_root), connection_name, request.model)
        except ValueError as error:
            raise ValueError(str(error)) from error
        return self.provider_settings()

    def delete_provider_connection(self, name: str) -> dict[str, object]:
        delete_connection(self.output_root, name=name)
        return self.provider_settings()

    def windows_tts_voices(self) -> dict[str, object]:
        try:
            voices = list_windows_tts_voices()
            default = default_windows_tts_voice()
        except TtsProviderError as error:
            raise ValueError("Windows 语音暂不可用。") from error
        return {
            "voices": [
                {"name": voice.name, "culture": voice.culture} for voice in voices
            ],
            "default_voice": default,
        }

    def set_provider_role(
        self, role: ProviderRole, request: ProviderRoleBindingRequest
    ) -> dict[str, object]:
        try:
            set_role_binding(
                self.output_root, role=role, connection_name=request.connection_name
            )
        except ValueError as error:
            raise ValueError("连接不能承担这个职责。") from error
        return self.provider_settings()

    def clear_provider_role(self, role: ProviderRole) -> dict[str, object]:
        clear_role_binding(self.output_root, role=role)
        return self.provider_settings()

    def check_provider_connection(
        self, name: str, *, confirm_paid: bool = False
    ) -> dict[str, str | bool]:
        """Run one user-confirmed functional request without automatic retries."""
        connection = load_settings(self.output_root).connections.get(name)
        if connection is None:
            raise KeyError(name)
        if connection.api_family != "local" and not confirm_paid:
            raise PermissionError("真实 Provider 检测需要明确确认。")
        try:
            result = run_provider_connection_check(str(self.output_root), name)
        except ProviderConnectionCheckError as error:
            reason = str(error)
            if "secret is unavailable" in reason:
                message = "连接检测未发送：API Key 无法读取，请重新保存连接。"
            else:
                message = "当前连接暂时无法执行功能检测。"
            raise ValueError(message) from error
        if result.status == "completed":
            message = (
                "已完成 1 次真实 Provider 请求，连接可用；可能产生少量费用，"
                "不计入任务每日调用限额。"
                if result.billable
                else "已完成 1 次本地功能检测，连接可用；未产生 Provider 费用。"
            )
        elif result.status == "unknown":
            message = (
                "已发送 1 次真实 Provider 请求，但结果无法确认；可能已经计费，"
                "且不会自动重试。"
            )
        else:
            message = (
                "真实 Provider 请求失败；本次未自动重试。请检查 API Key、"
                "服务地址和模型。"
                if result.billable
                else "本地功能检测失败；请检查本地模型、测试素材和系统语音。"
            )
        return {
            "status": result.status,
            "billable": result.billable,
            "message": message,
        }

    def save_provider_limits(self, request: ProviderLimitsRequest) -> dict[str, object]:
        try:
            update_limits(
                self.output_root,
                retries_per_role=request.retries_per_role,
                global_calls_per_day=request.global_calls_per_day,
                budget_group_calls_per_day=request.budget_group_calls_per_day,
            )
        except ValueError as error:
            raise ValueError("Provider 限额无效。") from error
        return self.provider_settings()

    def artifact(self, task_id: str, relative_path: str) -> Path:
        found = find_task_by_id(self.output_root, task_id)
        if found is None:
            raise KeyError(task_id)
        task_dir, task = found
        for artifact in _public_artifacts(task_dir, task):
            if artifact["path"] == relative_path:
                return (task_dir / relative_path).resolve()
        raise FileNotFoundError(relative_path)

    def create_douyin_login(self) -> dict[str, Any]:
        return self.douyin_login.create_session()

    def create_douyin_browser_login(self) -> dict[str, Any]:
        return self.douyin_login.create_browser_session()

    def current_douyin_login(self) -> dict[str, Any]:
        return self.douyin_login.current_session()

    def douyin_login_state(self, session_id: str) -> dict[str, Any]:
        return self.douyin_login.get_session(session_id)

    def refresh_douyin_login(self, session_id: str) -> dict[str, Any]:
        return self.douyin_login.refresh_session(session_id)

    def cancel_douyin_login(self, session_id: str) -> dict[str, Any]:
        return self.douyin_login.cancel_session(session_id)

    def douyin_qr_image(self, session_id: str) -> bytes:
        return self.douyin_login.qr_image(session_id)

    def douyin_favorites_snapshot(self) -> dict[str, Any]:
        return self.douyin_favorites.read_snapshot().payload()

    def sync_douyin_favorites(self, session_id: str) -> dict[str, Any]:
        """One user-triggered sync; it must never overlap a background sync."""
        if self._shutdown:
            raise DouyinFavoritesError("服务已关闭，无法同步收藏。")
        try:
            with douyin_sync_lock(self.output_root, timeout=0):
                self._set_sync_active(True)
                try:
                    return self._manual_sync_locked(session_id)
                finally:
                    self._set_sync_active(False)
        except LockUnavailable as error:
            raise DouyinFavoritesError("抖音收藏正在自动同步，请稍后再试。") from error

    def _manual_sync_locked(self, session_id: str) -> dict[str, Any]:
        cookie = self.douyin_login.cookie_for(session_id)
        browser_state_getter = getattr(
            self.douyin_login,
            "browser_storage_state_for",
            None,
        )
        browser_storage_state = (
            browser_state_getter(session_id) if callable(browser_state_getter) else None
        )
        pending_jobs: list[WebJob] = []

        def on_collected(
            old: DouyinFavoritesSnapshot, collected: DouyinFavoritesSnapshot
        ) -> None:
            if not self._manual_can_organize(old):
                return
            known = {item.aweme_id for item in old.items}
            for favorite in collected.items:
                if (
                    favorite.aweme_id not in known
                    and DEFAULT_DOUYIN_FOLDER_ID in favorite.folder_ids
                ):
                    pending_jobs.append(self._ensure_favorite_intent(favorite))

        try:
            snapshot = self.douyin_favorites.sync(
                cookie,
                browser_storage_state=browser_storage_state,
                on_authentication_failure=lambda: self.douyin_login.invalidate(
                    session_id
                ),
                on_collected=on_collected,
            )
        except (DouyinAuthenticationError, DouyinFavoritesError):
            raise
        except (DouyinLoginError, OSError, ValueError) as error:
            raise DouyinFavoritesError("抖音收藏同步失败。") from error
        for job in pending_jobs:
            try:
                self._start_job(job, self._process_job_runner(job))
            except Exception:
                self.jobs.fail(job.job_id, "任务未能启动，请在来源处理中重试。")
        self._write_favorites_status("success", "最近一次同步成功。")
        return snapshot.payload()

    def auto_sync_favorites(self) -> None:
        """Startup and periodic refresh for a connected, baseline-enabled root.

        Auto-discovery may refresh the favorites snapshot even when the paid
        gate is closed, but source task intents are created only when the full
        automatic gate is valid. Never raises: failures stay observable in the
        sync status fact and are fail-closed.
        """
        if self._shutdown or not self._auto_sync_should_run():
            return
        try:
            with douyin_sync_lock(self.output_root, timeout=0):
                self._auto_sync_locked()
        except LockUnavailable:
            # A manual or another background sync already holds the lock.
            return
        except Exception as error:
            self._write_favorites_status(
                "failed", _safe_error_message(error) or "抖音收藏自动同步失败。"
            )

    def _auto_sync_should_run(self) -> bool:
        """Cheap pre-lock guard: a connected session, a configured policy, and
        an existing successful baseline are all required for auto-discovery."""
        try:
            session = self.douyin_login.current_session()
        except DouyinLoginError:
            return False
        session_id = session.get("session_id") if isinstance(session, dict) else None
        if session.get("status") != "connected" or not isinstance(session_id, str):
            return False
        if load_automation_status(self.output_root) is None:
            # No automation policy means auto-discovery was never set up.
            return False
        previous = self.douyin_favorites.read_snapshot()
        if previous.synced_at is None:
            # The first successful sync only builds a baseline (manual/login).
            return False
        return True

    def _auto_sync_locked(self) -> None:
        try:
            session = self.douyin_login.current_session()
        except DouyinLoginError:
            return
        session_id = session.get("session_id") if isinstance(session, dict) else None
        if session.get("status") != "connected" or not isinstance(session_id, str):
            return
        if load_automation_status(self.output_root) is None:
            # No automation policy means auto-discovery was never set up.
            return
        previous = self.douyin_favorites.read_snapshot()
        if previous.synced_at is None:
            # The first successful sync only builds a baseline (manual/login).
            return
        self._set_sync_active(True)
        try:
            self._sync_once(session_id, previous=previous)
        except DouyinAuthenticationError:
            self._write_favorites_status(
                "reconnect_required", "登录已失效，请重新连接抖音。"
            )
        except DouyinFavoritesError as error:
            self._write_favorites_status("failed", str(error))
        except (DouyinLoginError, OSError, ValueError) as error:
            self._write_favorites_status(
                "failed", _safe_error_message(error) or "抖音收藏自动同步失败。"
            )
        finally:
            self._set_sync_active(False)

    def _sync_once(
        self,
        session_id: str,
        *,
        previous: DouyinFavoritesSnapshot,
    ) -> dict[str, Any]:
        """Full critical section: old snapshot -> pagination -> new detection
        -> recoverable intent -> publish -> start the source WebJobs."""
        cookie = self.douyin_login.cookie_for(session_id)
        browser_state_getter = getattr(
            self.douyin_login,
            "browser_storage_state_for",
            None,
        )
        browser_storage_state = (
            browser_state_getter(session_id) if callable(browser_state_getter) else None
        )
        pending_jobs: list[WebJob] = []

        def on_collected(
            old: DouyinFavoritesSnapshot, collected: DouyinFavoritesSnapshot
        ) -> None:
            if not self._auto_queue_gate(old):
                return
            known = {item.aweme_id for item in old.items}
            for favorite in collected.items:
                if (
                    favorite.aweme_id not in known
                    and DEFAULT_DOUYIN_FOLDER_ID in favorite.folder_ids
                ):
                    pending_jobs.append(self._ensure_favorite_intent(favorite))

        snapshot = self.douyin_favorites.sync(
            cookie,
            browser_storage_state=browser_storage_state,
            on_authentication_failure=lambda: self.douyin_login.invalidate(session_id),
            on_collected=on_collected,
        )
        for job in pending_jobs:
            try:
                self._start_job(job, self._process_job_runner(job))
            except Exception:
                # A thread-spawn failure must leave a visible, retryable fact
                # instead of a never-reclaimed queued intent.
                self.jobs.fail(job.job_id, "任务未能启动，请在来源处理中重试。")
        self._write_favorites_status("success", "最近一次自动同步成功。")
        return snapshot.payload()

    def _ensure_favorite_intent(self, favorite: Any) -> WebJob:
        """Create-or-reuse one durable source job intent per canonical URL."""
        existing = self.jobs.find_by_source("douyin_favorite", favorite.url)
        if existing is not None:
            return existing
        return self.jobs.create("douyin_favorite", favorite.url)

    def _manual_can_organize(self, previous: DouyinFavoritesSnapshot) -> bool:
        status = load_automation_status(self.output_root)
        return (
            previous.synced_at is not None
            and status is not None
            and status.policy.enabled
            and status.policy.authorized_at is not None
            and status.policy.auto_organize_new_favorites
            and status.policy.provider_settings_sha256
            == settings_sha256(load_settings(self.output_root))
        )

    def _auto_queue_gate(self, previous: DouyinFavoritesSnapshot) -> bool:
        if previous.synced_at is None:
            return False
        status = load_automation_status(self.output_root)
        if status is None:
            return False
        policy = status.policy
        if not (
            policy.enabled
            and policy.authorized_at is not None
            and policy.auto_organize_new_favorites
            and policy.provider_settings_sha256
            == settings_sha256(load_settings(self.output_root))
        ):
            return False
        try:
            bindings = freeze_role_bindings(self.output_root)
        except (KeyError, ValueError):
            return False
        return all(
            role in bindings
            for role in required_automation_roles(policy.default_output)
        )

    def douyin_favorites_sync_status(self) -> dict[str, Any]:
        if self._sync_active:
            return {
                "state": "syncing",
                "message": "正在同步收藏…",
                "checked_at": None,
            }
        reader = getattr(self.douyin_favorites, "read_sync_status", None)
        if not callable(reader):
            return {"state": "idle", "message": "", "checked_at": None}
        try:
            fact = reader()
        except (OSError, ValueError):
            return {"state": "idle", "message": "", "checked_at": None}
        return {
            "state": getattr(fact, "state", "idle"),
            "message": getattr(fact, "message", "") or "",
            "checked_at": getattr(fact, "checked_at", None),
        }

    def _write_favorites_status(self, state: str, message: str) -> None:
        writer = getattr(self.douyin_favorites, "write_sync_status", None)
        if not callable(writer):
            return
        try:
            writer(state, message)
        except (OSError, ValueError):
            pass

    def _set_sync_active(self, active: bool) -> None:
        with self._sync_active_lock:
            self._sync_active = active

    def douyin_thumbnail(self, relative_path: str) -> Path:
        return self.douyin_favorites.thumbnail_file(relative_path)

    def select_douyin_favorites(self, aweme_ids: list[str]) -> list[WebJob]:
        selected = set(aweme_ids)
        if len(selected) != len(aweme_ids) or not all(
            item.isdigit() for item in selected
        ):
            raise ValueError("请选择有效且不重复的收藏。")
        favorites = {
            item.aweme_id: item for item in self.douyin_favorites.read_snapshot().items
        }
        if any(item_id not in favorites for item_id in aweme_ids):
            raise ValueError("选择的收藏已不存在，请重新同步。")
        return [
            self.submit_process(favorites[item_id].url, source_kind="douyin_favorite")
            for item_id in aweme_ids
        ]

    def startup_sync(self) -> None:
        """One best-effort favorites refresh at app start for a connected,
        baseline-enabled output root; never blocks or fails the lifespan."""
        self.auto_sync_favorites()

    def shutdown(self) -> None:
        self._shutdown = True
        self.local_models.shutdown()
        self.douyin_login.shutdown()


def create_web_app(
    output_root: str | Path,
    *,
    douyin_login: DouyinLoginSessionManager | None = None,
    douyin_favorites: DouyinFavoritesStore | None = None,
    coordinator: AutomationCoordinator | None = None,
    launcher_config_path: Path | None = None,
    local_models: LocalModelService | None = None,
    douyin_short_resolver: ShortUrlResolver | None = None,
) -> FastAPI:
    """Create the loopback WebUI application without starting a server."""
    workspace = LearningWorkspace(output_root)
    coordinator = coordinator or AutomationCoordinator(output_root)
    service = WebService(
        output_root,
        douyin_login=douyin_login,
        douyin_favorites=douyin_favorites,
        coordinator=coordinator,
        launcher_config_path=launcher_config_path,
        local_models=local_models,
        douyin_short_resolver=douyin_short_resolver,
    )
    # The coordinator owns both the single startup sync and each periodic
    # timeout sync; a plain wake only drains intakes.
    coordinator.set_favorites_sync(service.auto_sync_favorites)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        await coordinator.start()
        try:
            yield
        finally:
            await coordinator.shutdown()
            service.shutdown()

    app = FastAPI(
        title="语栖学习收件箱",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.web_service = service
    app.state.learning_workspace = workspace
    app.state.automation_coordinator = coordinator

    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Never echo request inputs back: they may contain API keys."""

        def _scrub(error: dict[str, object]) -> dict[str, object]:
            return {key: value for key, value in error.items() if key != "input"}

        return JSONResponse(
            status_code=422,
            content={"detail": [_scrub(error) for error in exc.errors()]},
        )

    @app.middleware("http")
    async def revalidate_loopback_ui(
        request: Request, call_next: Callable[[Request], Any]
    ) -> Response:
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=_STATIC_DIRECTORY), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        page = (_STATIC_DIRECTORY / "index.html").read_text(encoding="utf-8")
        initial_hash_sync = """
<script>
function syncInitialHashBookmark() {
  const hash = window.location.hash;
  if (!hash) return;
  const current = document.querySelector(`.bookmark[href="${CSS.escape(hash)}"]`);
  if (!current) return;
  document.querySelectorAll(".bookmark").forEach((item) => item.classList.toggle("active", item === current));
}
syncInitialHashBookmark();
</script>"""
        return HTMLResponse(page.replace("</body>", f"{initial_hash_sync}\n  </body>"))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "scope": "loopback"}

    @app.post("/api/douyin/login/qr", status_code=201)
    def create_douyin_login() -> dict[str, Any]:
        try:
            return service.create_douyin_login()
        except DouyinLoginError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/douyin/login/browser", status_code=201)
    def create_douyin_browser_login() -> dict[str, Any]:
        try:
            return service.create_douyin_browser_login()
        except DouyinLoginError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.get("/api/douyin/login/current")
    def current_douyin_login() -> dict[str, Any]:
        return service.current_douyin_login()

    @app.get("/api/douyin/login/{session_id}")
    def douyin_login_state_general(session_id: str) -> dict[str, Any]:
        try:
            return service.douyin_login_state(session_id)
        except DouyinLoginError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.delete("/api/douyin/login/{session_id}")
    def cancel_douyin_login_general(session_id: str) -> dict[str, Any]:
        try:
            return service.cancel_douyin_login(session_id)
        except DouyinLoginError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/douyin/login/qr/{session_id}")
    def douyin_login_state(session_id: str) -> dict[str, Any]:
        try:
            return service.douyin_login_state(session_id)
        except DouyinLoginError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/douyin/login/qr/{session_id}/refresh")
    def refresh_douyin_login(session_id: str) -> dict[str, Any]:
        try:
            return service.refresh_douyin_login(session_id)
        except DouyinLoginError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.delete("/api/douyin/login/qr/{session_id}")
    def cancel_douyin_login(session_id: str) -> dict[str, Any]:
        try:
            return service.cancel_douyin_login(session_id)
        except DouyinLoginError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/douyin/login/qr/{session_id}/image")
    def douyin_qr_image(session_id: str) -> Response:
        try:
            return Response(service.douyin_qr_image(session_id), media_type="image/png")
        except DouyinLoginError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/douyin/favorites")
    def douyin_favorites() -> dict[str, Any]:
        return service.douyin_favorites_snapshot()

    @app.get("/api/douyin/favorites/status")
    def douyin_favorites_status() -> dict[str, Any]:
        return service.douyin_favorites_sync_status()

    @app.post("/api/douyin/favorites")
    def sync_douyin_favorites(
        request: DouyinFavoritesSyncRequest,
    ) -> dict[str, Any]:
        try:
            return service.sync_douyin_favorites(request.session_id)
        except DouyinAuthenticationError as error:
            detail = (
                "登录已失效，请重新连接抖音。"
                if error.reason == "credentials_rejected" and error.status_code is None
                else douyin_authentication_message(error)
            )
            raise HTTPException(
                status_code=502 if error.reason == "request_rejected" else 401,
                detail=detail,
            ) from error
        except DouyinLoginError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except DouyinFavoritesError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(
                status_code=502,
                detail="抖音收藏暂时无法同步。",
            ) from error

    @app.get("/api/douyin/favorites/thumbnails/{relative_path:path}")
    def douyin_thumbnail(relative_path: str) -> FileResponse:
        try:
            thumbnail = service.douyin_thumbnail(relative_path)
            media_type, _ = guess_type(thumbnail.name)
            return FileResponse(
                thumbnail,
                media_type=media_type or "application/octet-stream",
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="缩略图不存在。") from error

    @app.post("/api/douyin/favorites/select", status_code=202)
    def select_douyin_favorites(
        request: DouyinFavoritesSelectRequest,
    ) -> dict[str, list[dict[str, str | int | None]]]:
        try:
            return {
                "jobs": [
                    _job_payload(job)
                    for job in service.select_douyin_favorites(request.aweme_ids)
                ]
            }
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/learning/snapshot")
    def learning_snapshot(revision: str | None = None) -> dict[str, Any]:
        return _learning_snapshot_payload(workspace.snapshot(revision), workspace)

    @app.post("/api/learning/items", status_code=201)
    def add_learning_item(request: LearningItemRequest) -> dict[str, Any]:
        try:
            return {
                "item": _learning_item_payload(
                    workspace.add_content(request.source, request.desired_output)
                )
            }
        except LearningWorkspaceError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/learning/submit", status_code=202)
    def submit_learning_item(
        request: LearningSubmitRequest,
    ) -> dict[str, str | int | None]:
        try:
            return _job_payload(service.submit_process(request.source))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/learning/jobs")
    def learning_jobs() -> dict[str, list[dict[str, str | int | None]]]:
        try:
            return {"jobs": [_job_payload(job) for job in service.jobs.list()]}
        except ValueError as error:
            raise HTTPException(
                status_code=409, detail="来源操作记录无法读取。"
            ) from error

    @app.get("/api/learning/jobs/{job_id}")
    def learning_job(job_id: str) -> dict[str, str | int | None]:
        try:
            return _job_payload(service.jobs.get(job_id))
        except (KeyError, ValueError) as error:
            raise HTTPException(
                status_code=404, detail="来源操作不存在或不可读取。"
            ) from error

    @app.post("/api/learning/jobs/{job_id}/retry", status_code=202)
    def retry_learning_job(job_id: str) -> dict[str, str | int | None]:
        try:
            return _job_payload(service.retry_source_job(job_id))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="来源操作不存在。") from error
        except ValueError as error:
            raise HTTPException(
                status_code=409, detail="来源操作当前不能重试。"
            ) from error

    @app.post("/api/learning/uploads", status_code=202)
    async def upload_learning_video(request: Request, name: str) -> dict[str, Any]:
        try:
            uploaded = await service.save_uploaded_video(name, request.stream())
            job = service.submit_process(str(uploaded))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return _job_payload(job)

    @app.get("/api/learning/items/{item_ref}")
    def learning_item(item_ref: str) -> dict[str, Any]:
        try:
            return {"item": _learning_item_payload(workspace.item(item_ref))}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="内容不存在。") from error

    @app.post("/api/learning/items/{item_ref}/continue")
    def continue_learning_item(item_ref: str) -> dict[str, Any]:
        try:
            result = workspace.continue_item(item_ref)
            return {
                "item": _learning_item_payload(result.item),
                "outcome": result.outcome,
            }
        except KeyError as error:
            raise HTTPException(status_code=404, detail="内容不存在。") from error
        except LearningWorkspaceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/learning/items/{item_ref}/pause")
    def pause_learning_item(item_ref: str) -> dict[str, Any]:
        try:
            return {"item": _learning_item_payload(workspace.pause_item(item_ref))}
        except KeyError as error:
            raise HTTPException(status_code=404, detail="内容不存在。") from error
        except LearningWorkspaceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/learning/items/{item_ref}/resume")
    def resume_learning_item(item_ref: str) -> dict[str, Any]:
        try:
            item = workspace.resume_item(item_ref)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="内容不存在。") from error
        except LearningWorkspaceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        service.wake_automation()
        return {"item": _learning_item_payload(item)}

    @app.post("/api/learning/items/{item_ref}/start-automation", status_code=202)
    def start_learning_automation(item_ref: str) -> Response:
        try:
            service.start_automation(item_ref)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="内容不存在。") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(status_code=202)

    @app.delete("/api/learning/items/{item_ref}")
    def trash_learning_item(item_ref: str) -> dict[str, str]:
        try:
            result = trash_task(service.output_root, item_ref)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="任务不存在。") from error
        except TaskTrashError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"status": "trashed", "task_id": result.task_id}

    @app.get("/api/learning/trash")
    def list_learning_trash() -> dict[str, list[dict[str, str]]]:
        return {
            "items": [
                {
                    "bundle_id": item.bundle_id,
                    "task_id": item.task_id,
                    "title": item.title,
                    "trashed_at": item.trashed_at.isoformat(),
                }
                for item in list_trashed_tasks(service.output_root)
            ]
        }

    @app.post("/api/learning/trash/{bundle_id}/restore")
    def restore_learning_trash(bundle_id: str) -> dict[str, str]:
        try:
            restored = restore_trashed_task(service.output_root, bundle_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="回收任务不存在。") from error
        except TaskTrashError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"status": "restored", "task_id": restored.task_id}

    @app.delete("/api/learning/trash/{bundle_id}")
    def purge_learning_trash(bundle_id: str) -> dict[str, str]:
        try:
            task_id = purge_trashed_task(service.output_root, bundle_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="回收任务不存在。") from error
        except TaskTrashError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"status": "purged", "task_id": task_id}

    @app.post("/api/learning/items/{item_ref}/retry-automation", status_code=202)
    def retry_learning_automation(item_ref: str) -> Response:
        try:
            service.retry_automation(item_ref)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="内容不存在。") from error
        except ValueError as error:
            raise HTTPException(
                status_code=409, detail="当前整理不能安全重试。"
            ) from error
        return Response(status_code=202)

    @app.get("/api/learning/items/{item_ref}/audio")
    def learning_audio(item_ref: str) -> FileResponse:
        try:
            return FileResponse(workspace.audio(item_ref), media_type="audio/mpeg")
        except KeyError as error:
            raise HTTPException(status_code=404, detail="学习内容不存在。") from error
        except LearningWorkspaceError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get("/api/learning/items/{item_ref}/note")
    def learning_note(item_ref: str) -> HTMLResponse:
        try:
            note = workspace.note(item_ref)
            fragment, _ = _render_learning_note(note)
            try:
                workspace.audio(item_ref)
            except (KeyError, LearningWorkspaceError):
                audio_href = None
            else:
                audio_href = f"/api/learning/items/{quote(item_ref, safe='')}/audio"
            return HTMLResponse(_learning_note_page(note, fragment, audio_href))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="内容不存在。") from error
        except LearningWorkspaceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/learning/items/{item_ref}/images/{relative_path:path}")
    def learning_image(item_ref: str, relative_path: str) -> FileResponse:
        try:
            note = workspace.note(item_ref)
            _, images = _render_learning_note(note)
            image = images.get(relative_path)
            if image is None:
                raise FileNotFoundError(relative_path)
            media_type, _ = guess_type(image.name)
            return FileResponse(
                image, media_type=media_type or "application/octet-stream"
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="内容不存在。") from error
        except LearningWorkspaceError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=404, detail="图片不存在或不可读取。"
            ) from error

    @app.get("/api/tasks")
    def list_tasks() -> dict[str, list[dict[str, Any]]]:
        return {"tasks": service.list_tasks()}

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str) -> dict[str, Any]:
        try:
            return service.task_detail(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="任务不存在。") from error

    @app.get("/api/tasks/{task_id}/artifacts/{relative_path:path}")
    def task_artifact(task_id: str, relative_path: str) -> FileResponse:
        try:
            return FileResponse(service.artifact(task_id, relative_path))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="任务不存在。") from error
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=404, detail="产物不存在或不可公开访问。"
            ) from error

    @app.post("/api/jobs/process", status_code=202)
    def process(request: ProcessRequest) -> dict[str, Any]:
        try:
            job = service.submit_process(request.source)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return _job_payload(job)

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        try:
            return _job_payload(service.jobs.get(job_id))
        except (KeyError, ValueError) as error:
            raise HTTPException(
                status_code=404, detail="作业不存在或不可读取。"
            ) from error

    @app.post("/api/tasks/{task_id}/recover", status_code=202)
    def recover(task_id: str) -> dict[str, Any]:
        try:
            job = service.submit_recovery(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="任务不存在。") from error
        return _job_payload(job)

    @app.get("/api/providers/settings")
    def provider_settings(
        default_output: Literal["complete_note", "complete_note_with_audio"]
        | None = None,
    ) -> dict[str, object]:
        try:
            return service.provider_settings(default_output)
        except ValueError as error:
            raise HTTPException(
                status_code=500, detail="Provider 设置无效。"
            ) from error

    @app.post("/api/providers/connections")
    def save_provider_connection(
        request: ProviderConnectionRequest,
    ) -> dict[str, object]:
        try:
            return service.save_provider_connection(request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete("/api/providers/connections/{name}")
    def delete_provider_connection(name: str) -> dict[str, object]:
        try:
            return service.delete_provider_connection(name)
        except ProviderConnectionNotFoundError as error:
            raise HTTPException(status_code=404, detail="连接不存在。") from error
        except ProviderConnectionBoundError as error:
            labels = {
                "note_writer": "笔记 Writer",
                "note_reviewer": "笔记 Reviewer",
                "podcast": "播客",
                "tts": "TTS",
                "asr": "ASR",
                "ocr": "OCR",
            }
            roles = "、".join(labels[role] for role in error.roles)
            raise HTTPException(
                status_code=409,
                detail=f"连接仍被 {roles} 绑定，请先替换职责连接。",
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail="连接无法删除。") from error

    @app.post("/api/providers/connections/{name}/models")
    async def provider_model_catalog(name: str, request: Request) -> dict[str, object]:
        if (await request.body()).strip() or request.query_params:
            raise HTTPException(
                status_code=422,
                detail="模型目录请求不接受 URL、Key、Provider 或自定义参数。",
            )
        try:
            return service.provider_model_catalog(name)
        except ProviderModelCatalogError as error:
            status_code = 404 if error.code == "connection_not_found" else 409
            raise HTTPException(
                status_code=status_code, detail=error.public_message
            ) from error

    @app.put("/api/providers/connections/{name}/model")
    def save_provider_model_endpoint(
        name: str, request: ProviderModelUpdateRequest
    ) -> dict[str, object]:
        try:
            return service.save_provider_model(name, request)
        except ProviderConnectionNotFoundError as error:
            raise HTTPException(status_code=404, detail="连接不存在。") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/providers/presets/{preset}/models")
    def provider_preset_model_catalog_endpoint(
        preset: str,
        request: ProviderPresetCatalogRequest,
        raw_request: Request,
    ) -> dict[str, object]:
        if raw_request.query_params:
            raise HTTPException(
                status_code=422,
                detail="模型目录请求不接受 URL、Key、Provider 或自定义参数。",
            )
        try:
            preview = service.provider_preset_model_catalog(preset, request.api_key)
        except ProviderModelCatalogError as error:
            status_code = (
                404
                if error.code in {"unsupported_preset", "connection_not_found"}
                else 409
            )
            raise HTTPException(
                status_code=status_code, detail=error.public_message
            ) from error
        return {
            "source": preview.source,
            "models": preview.models,
            "note": preview.note,
            "adapter_revision": preview.adapter_revision,
        }

    @app.get("/api/providers/windows-tts/voices")
    def windows_tts_voices() -> dict[str, object]:
        try:
            return service.windows_tts_voices()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/providers/roles/{role}")
    def save_provider_role(
        role: ProviderRole, request: ProviderRoleBindingRequest
    ) -> dict[str, object]:
        try:
            return service.set_provider_role(role, request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/providers/setup-roles/{role_name}")
    def save_provider_setup_role(
        role_name: Literal[
            "笔记 Writer",
            "笔记 Reviewer",
            "播客",
            "TTS",
            "语音识别（ASR）",
            "画面文字（OCR）",
        ],
        request: ProviderRoleBindingRequest,
    ) -> dict[str, object]:
        try:
            return service.set_provider_role(_SETUP_ROLE_NAMES[role_name], request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete("/api/providers/roles/{role}")
    def clear_provider_role(role: ProviderRole) -> dict[str, object]:
        try:
            return service.clear_provider_role(role)
        except ProviderRoleNotBoundError as error:
            raise HTTPException(
                status_code=404, detail="该职责尚未绑定连接。"
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail="职责无法解绑。") from error

    @app.delete("/api/providers/setup-roles/{role_name}")
    def clear_provider_setup_role(
        role_name: Literal[
            "笔记 Writer",
            "笔记 Reviewer",
            "播客",
            "TTS",
            "语音识别（ASR）",
            "画面文字（OCR）",
        ],
    ) -> dict[str, object]:
        try:
            return service.clear_provider_role(_SETUP_ROLE_NAMES[role_name])
        except ProviderRoleNotBoundError as error:
            raise HTTPException(
                status_code=404, detail="该职责尚未绑定连接。"
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail="职责无法解绑。") from error

    @app.post("/api/providers/connections/{name}/check")
    def check_provider_connection(
        name: str, request: ProviderConnectionCheckRequest | None = None
    ) -> dict[str, str | bool]:
        try:
            return service.check_provider_connection(
                name,
                confirm_paid=request is not None and request.confirm_paid is True,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="连接不存在。") from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/local-models")
    def local_models_status() -> dict[str, object]:
        return service.local_models.snapshot()

    @app.put("/api/local-models/root")
    def save_local_model_root(request: ModelRootRequest) -> dict[str, object]:
        try:
            return service.save_model_root(request)
        except LocalModelError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/local-models/{package_id}/download", status_code=202)
    def download_local_model(package_id: str) -> dict[str, object]:
        try:
            return service.local_models.install(package_id)
        except LocalModelNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except LocalModelError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/local-models/{package_id}/cancel", status_code=202)
    def cancel_local_model_install(package_id: str) -> dict[str, object]:
        try:
            return service.local_models.cancel(package_id)
        except LocalModelNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except LocalModelError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.put("/api/providers/limits")
    def save_provider_limits(request: ProviderLimitsRequest) -> dict[str, object]:
        try:
            return service.save_provider_limits(request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/automation/status")
    def automation_status() -> dict[str, Any]:
        try:
            return service.automation_status()
        except ValueError as error:
            raise HTTPException(
                status_code=500, detail="自动化状态文件无效。"
            ) from error

    @app.get("/api/storage")
    def storage_status() -> dict[str, Any]:
        return service.storage_status()

    @app.put("/api/storage/output-root")
    def save_output_root(request: OutputRootRequest) -> dict[str, Any]:
        try:
            return service.save_output_root(request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/automation/configure")
    def configure_automation(request: AutomationConfigureRequest) -> dict[str, Any]:
        try:
            return service.configure_automation(request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/automation/authorize")
    def authorize_automation_endpoint(
        request: AutomationAuthorizeRequest,
    ) -> dict[str, Any]:
        try:
            return service.authorize_automation(request)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/automation/disable")
    def disable_automation_endpoint() -> dict[str, Any]:
        try:
            return service.disable_automation()
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return app


def serve_web_app(output_root: str | Path, *, port: int) -> None:
    """Start Uvicorn with the non-configurable loopback host."""
    uvicorn.run(create_web_app(output_root), host="127.0.0.1", port=port)


def _task_summary(task: TaskRecord) -> dict[str, Any]:
    completed = sum(status.value == "completed" for status in task.stages.values())
    return {
        "task_id": task.task_id,
        "title": task.title,
        "source": _safe_source_label(task),
        "source_type": task.source_type,
        "profile": task.profile,
        "state": _task_state(task),
        "completed_stages": completed,
        "total_stages": len(task.stages),
        "error_summary": _safe_text(task.error_summary),
        "created_at": _utc_iso(task.created_at),
        "updated_at": _utc_iso(task.updated_at),
        "completed_at": _utc_iso(task.completed_at),
    }


def _utc_iso(value: datetime | None) -> str | None:
    """Serialise one lifecycle time as timezone-attached UTC ISO text."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _task_state(task: TaskRecord) -> str:
    if task.error_summary:
        return "failed"
    if task.active_attempt_id is not None:
        return "running"
    if task.stages and all(
        status.value == "completed" for status in task.stages.values()
    ):
        return "completed"
    return "pending"


def _safe_source_label(task: TaskRecord) -> str:
    source = task.source_input or task.source_path
    if task.source_type == "url":
        parsed = urlsplit(source)
        host = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            host = f"{host}:{port}"
        return f"{host}{parsed.path}".strip("/") or host
    return Path(source).name


def _public_artifacts(task_dir: Path, task: TaskRecord) -> list[dict[str, str]]:
    public: list[dict[str, str]] = []
    resolved_task_dir = task_dir.resolve()
    for stage, paths in task.artifacts.items():
        for artifact in paths:
            candidate = (task_dir / artifact).resolve()
            try:
                relative = candidate.relative_to(resolved_task_dir).as_posix()
            except ValueError:
                continue
            if candidate.is_file():
                public.append(
                    {
                        "stage": stage,
                        "name": candidate.name,
                        "path": relative,
                        "href": (
                            f"/api/tasks/{quote(task.task_id, safe='')}/artifacts/"
                            f"{quote(relative, safe='/')}"
                        ),
                    }
                )
    return public


def _job_payload(job: WebJob) -> dict[str, str | int | None]:
    return job.public()


def _learning_item_payload(
    item: LearningItem, workspace: LearningWorkspace | None = None
) -> dict[str, str | bool | None]:
    payload = {
        "item_ref": item.item_ref,
        "title": item.title,
        "source": item.source,
        "state": item.state,
        "message": item.message,
        "action": item.action,
        "action_kind": item.action_kind,
        "failure_reason": item.failure_reason,
        "failure_stage": item.failure_stage,
        "failure_stage_code": item.failure_stage_code,
        "output_goal": item.output_goal,
        "manually_paused": item.manually_paused,
        "can_pause": item.can_pause,
        "created_at": _utc_iso(item.created_at),
        "updated_at": _utc_iso(item.updated_at),
        "completed_at": _utc_iso(item.completed_at),
    }
    if workspace is not None:
        try:
            workspace.note(item.item_ref)
        except (KeyError, LearningWorkspaceError):
            payload["note_href"] = None
        else:
            payload["note_href"] = (
                f"/api/learning/items/{quote(item.item_ref, safe='')}/note"
            )
        try:
            workspace.audio(item.item_ref)
        except (KeyError, LearningWorkspaceError):
            payload["audio_href"] = None
        else:
            payload["audio_href"] = (
                f"/api/learning/items/{quote(item.item_ref, safe='')}/audio"
            )
    return payload


def _learning_snapshot_payload(
    snapshot: Any, workspace: LearningWorkspace | None = None
) -> dict[str, Any]:
    return {
        "revision": snapshot.revision,
        "unchanged": snapshot.unchanged,
        "inbox": [_learning_item_payload(item, workspace) for item in snapshot.inbox],
        "processing": [
            _learning_item_payload(item, workspace) for item in snapshot.processing
        ],
        "library": [
            _learning_item_payload(item, workspace) for item in snapshot.library
        ],
    }


def _render_learning_note(note: Any) -> tuple[str, dict[str, Path]]:
    """Render safe Markdown and expose only image files actually referenced by it."""
    markdown = re.sub(r"^<!-- learnnest-task-id: [^>]+ -->\s*", "", note.markdown)
    tokens = _MARKDOWN.parse(markdown)
    images: dict[str, Path] = {}
    for token in tokens:
        for child in token.children or ():
            if child.type != "image":
                continue
            source = child.attrGet("src")
            if source is None:
                raise LearningWorkspaceError("笔记图片无法安全读取。")
            image = _resolve_note_image(note.note_path, note.task_dir, source)
            relative = image.relative_to(note.task_dir.resolve()).as_posix()
            images[relative] = image
            child.attrSet(
                "src",
                f"/api/learning/items/{quote(note.item.item_ref, safe='')}/images/"
                f"{quote(relative, safe='/')}",
            )
    return _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, {}), images


def _learning_note_page(note: Any, fragment: str, audio_href: str | None) -> str:
    """Wrap one already-safe note fragment in a readable, same-origin document."""
    from html import escape

    title = escape(str(note.item.title))
    audio = (
        f'<section class="note-audio" aria-label="本篇笔记的音频">'
        f'<p>边读边听</p><audio controls preload="metadata" '
        f'aria-label="播放本篇笔记的音频" src="{escape(audio_href)}">'
        "音频暂时不能播放。</audio></section>"
        if audio_href is not None
        else ""
    )
    audio_status = "可播放" if audio_href is not None else "未生成"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title} · 语栖</title><link rel="icon" href="data:," />
<link rel="stylesheet" href="/static/workspace.css?v=20260831-1" />
</head><body class="note-page">
<header class="note-app-header"><div class="note-app-header-inner">
<a class="note-brand" href="/#tasks"><span class="brand-mark" aria-hidden="true">语</span><span><strong>语栖</strong><small>LEARNNEST</small></span></a>
<a class="note-return" href="/#tasks">返回任务工作台 →</a>
</div></header>
<main class="note-shell">
<aside class="note-rail"><p class="rail-label">学习成品</p><h2>完整笔记</h2><p>这是当前任务已经验证并发布的阅读版本。</p><dl class="note-rail-list"><div><dt>笔记状态</dt><dd>可阅读</dd></div><div><dt>配套音频</dt><dd>{audio_status}</dd></div></dl><a class="note-back" href="/#tasks">← 返回任务工作台</a></aside>
<section class="note-reading"><header class="note-reading-header"><p class="eyebrow">语栖学习笔记</p><h1>{title}</h1></header>
{audio}<article class="note-content">{fragment}</article></section>
</main></body></html>"""


def _resolve_note_image(note_path: Path, task_dir: Path, source: str) -> Path:
    parsed = urlsplit(source)
    raw_path = parsed.path
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not raw_path
        or raw_path.startswith(("/", "\\"))
        or Path(raw_path).is_absolute()
    ):
        raise LearningWorkspaceError("笔记图片无法安全读取。")
    root = task_dir.resolve()
    candidate = (note_path.parent / raw_path).resolve()
    if (
        not candidate.is_relative_to(root)
        or candidate.suffix.lower() not in _IMAGE_SUFFIXES
        or not candidate.is_file()
    ):
        raise LearningWorkspaceError("笔记图片无法安全读取。")
    return candidate


def _automation_connection_snapshot(
    connection: object, *, settings_sha256: str
) -> AssistedConnectionSnapshot:
    if getattr(connection, "api_family", None) != "openai_chat":
        raise ValueError("connection cannot generate learning notes")
    return AssistedConnectionSnapshot(
        connection_name=str(getattr(connection, "name")),
        connection_id=str(getattr(connection, "connection_id")),
        secret_id=getattr(connection, "secret_id"),
        provider=str(getattr(connection, "provider")),
        endpoint_identity=str(getattr(connection, "endpoint"))
        .strip()
        .rstrip("/")
        .lower(),
        model=str(getattr(connection, "model")),
        adapter_revision=str(getattr(connection, "adapter_revision")),
        settings_sha256=settings_sha256,
    )


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, LockUnavailable):
        return "任务正在运行，请等待当前处理结束后再试。"
    if isinstance(error, (SourceParseError, ValueError)):
        return "操作无法执行，请检查输入或在 CLI 中查看恢复计划。"
    if isinstance(error, (PipelineError, OSError)):
        return "处理失败，请在本地任务目录查看任务报告后重试。"
    return "操作失败，请在 CLI 中查看本地诊断信息。"


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _WINDOWS_PATH.sub("[本地路径]", value)


def _isoformat(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None
