"""Loopback-only WebUI for deterministic LearnNest task operations."""

from __future__ import annotations

from contextlib import asynccontextmanager
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from mimetypes import guess_type
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote, urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field

from learnnest.automation_store import load_status as load_automation_status
from learnnest.adapters.douyin_http import DouyinAuthenticationError
from learnnest.douyin_favorites import DouyinFavoritesStore
from learnnest.douyin_cookie_store import DouyinCookieStore
from learnnest.douyin_login import DouyinLoginError, DouyinLoginSessionManager
from learnnest.execution import plan_recovery
from learnnest.learning_workspace import (
    LearningItem,
    LearningWorkspace,
    LearningWorkspaceError,
    validate_public_web_url,
)
from learnnest.locks import LockUnavailable, task_lock
from learnnest.models import TaskRecord
from learnnest.pipeline import PipelineError, process_source, process_video, rerun_task
from learnnest.sources import SourceParseError, collect_sources
from learnnest.task_store import find_task_by_id, load_task

_STATIC_DIRECTORY = Path(__file__).parent / "static"
_WINDOWS_PATH = re.compile(r"(?i)\b[a-z]:[\\/][^\r\n]*")
_MARKDOWN = MarkdownIt("commonmark", {"html": False})
_IMAGE_SUFFIXES = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}


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


@dataclass
class WebJob:
    """In-memory status for one process or recovery operation."""

    job_id: str
    operation: str
    status: str = "queued"
    task_id: str | None = None
    error: str | None = None


class WebJobStore:
    """Small thread-safe job registry; persisted task JSON remains authoritative."""

    def __init__(self) -> None:
        self._jobs: dict[str, WebJob] = {}
        self._lock = threading.Lock()

    def submit(self, operation: str, runner: Callable[[], str]) -> WebJob:
        job = WebJob(job_id=uuid.uuid4().hex, operation=operation)
        with self._lock:
            self._jobs[job.job_id] = job
        thread = threading.Thread(
            target=self._run, args=(job.job_id, runner), daemon=True
        )
        thread.start()
        return self.get(job.job_id)

    def get(self, job_id: str) -> WebJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return WebJob(**asdict(job))

    def _run(self, job_id: str, runner: Callable[[], str]) -> None:
        with self._lock:
            self._jobs[job_id].status = "running"
        try:
            task_id = runner()
        except Exception as error:  # Error text is sanitized before display.
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.error = _safe_error_message(error)
            return
        with self._lock:
            job = self._jobs[job_id]
            job.status = "completed"
            job.task_id = task_id


class WebService:
    """Adapts the existing task and pipeline services for a local browser."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        douyin_login: DouyinLoginSessionManager | None = None,
        douyin_favorites: DouyinFavoritesStore | None = None,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.jobs = WebJobStore()
        self.douyin_login = douyin_login or DouyinLoginSessionManager(
            cookie_store=DouyinCookieStore(self.output_root)
        )
        self.douyin_favorites = douyin_favorites or DouyinFavoritesStore(
            self.output_root
        )

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
        tasks.sort(key=lambda item: item[0].name, reverse=True)
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

    def submit_process(self, source: str) -> WebJob:
        try:
            source_item = collect_sources(input_value=source)[0]
            if source_item.input_type == "url":
                validate_public_web_url(source_item.input)
        except (LearningWorkspaceError, SourceParseError) as error:
            raise ValueError(
                "输入无效，请提供存在的本地视频或公开 HTTP(S) URL。"
            ) from error

        def runner() -> str:
            if source_item.input_type == "local_file":
                task = process_video(
                    Path(source_item.input), self.output_root, "evidence"
                )
            else:
                task = process_source(source_item, self.output_root, "evidence")
            return task.task_id

        return self.jobs.submit("process", runner)

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

        return self.jobs.submit("recover", runner)

    def automation_status(self) -> dict[str, Any]:
        status = load_automation_status(self.output_root)
        if status is None:
            return {"configured": False, "enabled": False}
        return {
            "configured": True,
            "enabled": status.policy.enabled,
            "schedule_id": status.policy.schedule_id,
            "authorized_at": _isoformat(status.policy.authorized_at),
            "last_tick_at": _isoformat(status.last_tick_at),
            "last_tick_summary": _safe_text(status.last_tick_summary),
        }

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
        cookie = self.douyin_login.cookie_for(session_id)
        snapshot = self.douyin_favorites.sync(
            cookie,
            on_authentication_failure=lambda: self.douyin_login.invalidate(session_id),
        )
        return snapshot.payload()

    def douyin_thumbnail(self, relative_path: str) -> Path:
        return self.douyin_favorites.thumbnail_file(relative_path)

    def shutdown(self) -> None:
        self.douyin_login.shutdown()


def create_web_app(
    output_root: str | Path,
    *,
    douyin_login: DouyinLoginSessionManager | None = None,
    douyin_favorites: DouyinFavoritesStore | None = None,
) -> FastAPI:
    """Create the loopback WebUI application without starting a server."""
    service = WebService(
        output_root,
        douyin_login=douyin_login,
        douyin_favorites=douyin_favorites,
    )
    workspace = LearningWorkspace(output_root)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        yield
        service.shutdown()

    app = FastAPI(
        title="语栖学习收件箱",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.web_service = service
    app.state.learning_workspace = workspace
    app.mount("/static", StaticFiles(directory=_STATIC_DIRECTORY), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIRECTORY / "index.html")

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

    @app.post("/api/douyin/favorites")
    def sync_douyin_favorites(
        request: DouyinFavoritesSyncRequest,
    ) -> dict[str, Any]:
        try:
            return service.sync_douyin_favorites(request.session_id)
        except DouyinAuthenticationError as error:
            raise HTTPException(
                status_code=401,
                detail="登录已失效，请重新连接抖音。",
            ) from error
        except DouyinLoginError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
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

    @app.get("/api/learning/snapshot")
    def learning_snapshot(revision: str | None = None) -> dict[str, Any]:
        return _learning_snapshot_payload(workspace.snapshot(revision))

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

    @app.get("/api/learning/items/{item_ref}/note")
    def learning_note(item_ref: str) -> HTMLResponse:
        try:
            note = workspace.note(item_ref)
            html, _ = _render_learning_note(note)
            return HTMLResponse(html)
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
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="作业不存在或服务已重启。"
            ) from error

    @app.post("/api/tasks/{task_id}/recover", status_code=202)
    def recover(task_id: str) -> dict[str, Any]:
        try:
            job = service.submit_recovery(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="任务不存在。") from error
        return _job_payload(job)

    @app.get("/api/automation/status")
    def automation_status() -> dict[str, Any]:
        try:
            return service.automation_status()
        except ValueError as error:
            raise HTTPException(
                status_code=500, detail="自动化状态文件无效。"
            ) from error

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
    }


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


def _job_payload(job: WebJob) -> dict[str, str | None]:
    return {
        "job_id": job.job_id,
        "operation": job.operation,
        "status": job.status,
        "task_id": job.task_id,
        "error": job.error,
    }


def _learning_item_payload(item: LearningItem) -> dict[str, str | None]:
    return {
        "item_ref": item.item_ref,
        "title": item.title,
        "source": item.source,
        "state": item.state,
        "message": item.message,
        "action": item.action,
    }


def _learning_snapshot_payload(snapshot: Any) -> dict[str, Any]:
    return {
        "revision": snapshot.revision,
        "unchanged": snapshot.unchanged,
        "inbox": [_learning_item_payload(item) for item in snapshot.inbox],
        "processing": [_learning_item_payload(item) for item in snapshot.processing],
        "library": [_learning_item_payload(item) for item in snapshot.library],
    }


def _render_learning_note(note: Any) -> tuple[str, dict[str, Path]]:
    """Render safe Markdown and expose only image files actually referenced by it."""
    tokens = _MARKDOWN.parse(note.markdown)
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
