"""Explicit, resumable installation and local-only resolution of model packages."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from filelock import FileLock, Timeout

from learnnest.runtime_config import load_runtime_environment


@dataclass(frozen=True)
class ModelComponent:
    name: str
    repo_id: str
    revision: str
    required_files: tuple[str, ...]


@dataclass(frozen=True)
class ModelPackage:
    package_id: str
    capability: str
    display_name: str
    expected_bytes: int
    components: tuple[ModelComponent, ...]


_PACKAGES = (
    ModelPackage(
        package_id="faster-whisper-large-v3",
        capability="asr",
        display_name="Faster Whisper large-v3",
        expected_bytes=3_090_835_702,
        components=(
            ModelComponent(
                name="model",
                repo_id="Systran/faster-whisper-large-v3",
                revision="edaa852ec7e145841d8ffdb056a99866b5f0a478",
                required_files=(
                    "config.json",
                    "model.bin",
                    "preprocessor_config.json",
                    "tokenizer.json",
                    "vocabulary.json",
                ),
            ),
        ),
    ),
    ModelPackage(
        package_id="paddleocr-pp-ocrv6-medium",
        capability="ocr",
        display_name="PaddleOCR PP-OCRv6 medium",
        expected_bytes=139_110_993,
        components=(
            ModelComponent(
                name="detection",
                repo_id="PaddlePaddle/PP-OCRv6_medium_det",
                revision="8e0f56fb2ef86b461d99cfc7ac5c137738985f61",
                required_files=(
                    "inference.json",
                    "inference.pdiparams",
                    "inference.yml",
                ),
            ),
            ModelComponent(
                name="recognition",
                repo_id="PaddlePaddle/PP-OCRv6_medium_rec",
                revision="e5a92bcbc5cc1b494628e458d267778f0704fd7c",
                required_files=(
                    "inference.json",
                    "inference.pdiparams",
                    "inference.yml",
                ),
            ),
        ),
    ),
)
_PACKAGE_BY_ID = {package.package_id: package for package in _PACKAGES}
_ACTIVE_STATES = {"queued", "downloading", "verifying"}
_RETRY_STATES = {"failed", "cancelled", "interrupted"}
_INSTALL_ID = re.compile(r"^[0-9a-f]{32}$")


class LocalModelError(RuntimeError):
    """A safe local-model error suitable for the API boundary."""


class LocalModelNotFoundError(LocalModelError):
    pass


class LocalModelCancelled(Exception):
    pass


class LocalModelInstaller(Protocol):
    def __call__(
        self,
        package: ModelPackage,
        staging: Path,
        progress: Callable[[str, int], None],
        cancel: threading.Event,
    ) -> None: ...


def package_specs() -> tuple[ModelPackage, ...]:
    return _PACKAGES


def package_spec(package_id: str) -> ModelPackage:
    try:
        return _PACKAGE_BY_ID[package_id]
    except KeyError as error:
        raise LocalModelNotFoundError("未知的本地模型包。") from error


def model_store_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LearnNest" / "models"
    return Path.home() / "AppData" / "Local" / "LearnNest" / "models"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for delay in (0.02, 0.05, 0.1, None):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if delay is None:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(delay)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _component_is_complete(directory: Path, component: ModelComponent) -> bool:
    return directory.is_dir() and all(
        (directory / name).is_file() and (directory / name).stat().st_size > 0
        for name in component.required_files
    )


def _package_is_complete(directory: Path, package: ModelPackage) -> bool:
    return all(
        _component_is_complete(directory / component.name, component)
        for component in package.components
    )


def _managed_install(root: Path, package: ModelPackage) -> Path | None:
    pointer = _read_json(root / "packages" / package.package_id / "current.json")
    install_id = pointer.get("install_id") if pointer else None
    if not isinstance(install_id, str) or not _INSTALL_ID.fullmatch(install_id):
        return None
    install = root / "packages" / package.package_id / "installs" / install_id
    return install if _package_is_complete(install, package) else None


def _huggingface_cache_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    runtime = load_runtime_environment(os.getcwd())
    if cache := os.environ.get("HUGGINGFACE_HUB_CACHE") or runtime.get(
        "HUGGINGFACE_HUB_CACHE"
    ):
        roots.append(Path(cache))
    if hf_home := os.environ.get("HF_HOME"):
        roots.append(Path(hf_home) / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return tuple(dict.fromkeys(roots))


def _external_asr_install(package: ModelPackage) -> Path | None:
    component = package.components[0]
    repo_directory = f"models--{component.repo_id.replace('/', '--')}"
    for cache in _huggingface_cache_roots():
        snapshot = cache / repo_directory / "snapshots" / component.revision
        if _component_is_complete(snapshot, component):
            return snapshot
    return None


def _external_ocr_install(package: ModelPackage) -> tuple[Path, Path] | None:
    cache = Path(os.environ.get("PADDLE_PDX_CACHE_HOME", Path.home() / ".paddlex"))
    roots = (cache / "official_models", cache)
    names = ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec")
    for root in roots:
        directories = tuple(root / name for name in names)
        if all(
            _component_is_complete(directory, component)
            and (
                directory
                / ".cache"
                / "huggingface"
                / "trees"
                / f"{component.revision}.json"
            ).is_file()
            for directory, component in zip(
                directories, package.components, strict=True
            )
        ):
            return directories  # type: ignore[return-value]
    return None


def resolve_asr_model(root: Path | None = None) -> Path:
    package = package_spec("faster-whisper-large-v3")
    managed = _managed_install(root or model_store_root(), package)
    if managed is not None:
        return managed / "model"
    external = _external_asr_install(package)
    if external is not None:
        return external
    raise LocalModelError("语音识别模型尚未安装。")


def resolve_ocr_models(root: Path | None = None) -> tuple[Path, Path]:
    package = package_spec("paddleocr-pp-ocrv6-medium")
    managed = _managed_install(root or model_store_root(), package)
    if managed is not None:
        return managed / "detection", managed / "recognition"
    external = _external_ocr_install(package)
    if external is not None:
        return external
    raise LocalModelError("画面文字识别模型尚未安装。")


def _directory_size(directory: Path) -> int:
    try:
        return sum(
            path.stat().st_size for path in directory.rglob("*") if path.is_file()
        )
    except OSError:
        return 0


class SubprocessModelInstaller:
    """Download pinned official snapshots in a cancellable child process."""

    def __call__(
        self,
        package: ModelPackage,
        staging: Path,
        progress: Callable[[str, int], None],
        cancel: threading.Event,
    ) -> None:
        environment = dict(os.environ)
        environment.pop("HF_HUB_OFFLINE", None)
        environment.pop("TRANSFORMERS_OFFLINE", None)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "learnnest.local_model_worker",
                package.package_id,
                str(staging),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        messages: queue.Queue[str | None] = queue.Queue()

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                messages.put(line)
            messages.put(None)

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        last_size = 0
        while process.poll() is None:
            if cancel.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise LocalModelCancelled
            try:
                message = messages.get(timeout=0.2)
            except queue.Empty:
                message = ""
            if message:
                try:
                    payload = json.loads(message)
                except ValueError:
                    payload = {}
                if payload.get("event") == "progress":
                    progress("downloading", int(payload.get("downloaded_bytes", 0)))
            size = _directory_size(staging)
            if size != last_size:
                last_size = size
                progress("downloading", size)
        reader.join(timeout=1)
        if cancel.is_set():
            raise LocalModelCancelled
        if process.returncode != 0:
            raise RuntimeError("model download worker failed")
        progress("downloading", _directory_size(staging))


class LocalModelService:
    """Own model-package state independently from learning task jobs."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        installer: LocalModelInstaller | None = None,
        discover_external: bool = True,
    ) -> None:
        self.root = Path(root or model_store_root()).resolve()
        self._installer = installer or SubprocessModelInstaller()
        self._discover_external = discover_external
        self._guard = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancellations: dict[str, threading.Event] = {}
        self._recover_interrupted_jobs()

    def list(self) -> list[dict[str, Any]]:
        return [self.get(package.package_id) for package in _PACKAGES]

    def snapshot(self) -> dict[str, object]:
        return {"model_home": str(self.root), "models": self.list()}

    def get(self, package_id: str) -> dict[str, Any]:
        with self._guard:
            package = package_spec(package_id)
            managed = _managed_install(self.root, package)
            if managed is not None:
                status = self._status(package)
                return self._public(
                    package,
                    {**status, "state": "ready", "install_source": "managed"},
                )
            external = self._external(package) if self._discover_external else None
            status = self._status(package)
            if external is not None and status.get("state") == "not_installed":
                return self._public(
                    package,
                    {
                        **status,
                        "state": "external_ready",
                        "install_source": "external_cache",
                    },
                )
            if status.get("state") == "ready":
                status = {
                    **status,
                    "state": "failed",
                    "error": "模型文件不完整，请重新下载。",
                    "install_source": None,
                }
            return self._public(package, status)

    def install(self, package_id: str) -> dict[str, Any]:
        package = package_spec(package_id)
        with self._guard:
            current = self.get(package_id)
            if current["state"] == "ready":
                raise LocalModelError("这个模型已经可以使用。")
            if current["state"] in _ACTIVE_STATES:
                raise LocalModelError("这个模型正在下载。")
            operation_lock = self._operation_lock(package)
            try:
                operation_lock.acquire(timeout=0)
            except Timeout:
                raise LocalModelError("另一个 LearnNest 进程正在下载这个模型。")
            previous = self._status(package)
            attempt = int(previous.get("attempt", 0)) + 1
            job_id = uuid.uuid4().hex
            cancel = threading.Event()
            self._cancellations[package_id] = cancel
            try:
                self._write_status(
                    package,
                    state="queued",
                    job_id=job_id,
                    attempt=attempt,
                    downloaded_bytes=0,
                    error=None,
                    install_source=None,
                )
                thread = threading.Thread(
                    target=self._run_install,
                    args=(package, job_id, attempt, cancel, operation_lock),
                    daemon=True,
                    name=f"learnnest-model-{package.capability}",
                )
                self._threads[package_id] = thread
                thread.start()
            except Exception:
                self._cancellations.pop(package_id, None)
                self._threads.pop(package_id, None)
                operation_lock.release()
                raise
            return self.get(package_id)

    def cancel(self, package_id: str) -> dict[str, Any]:
        package_spec(package_id)
        with self._guard:
            status = self.get(package_id)
            if status["state"] not in _ACTIVE_STATES:
                raise LocalModelError("当前没有可取消的模型下载。")
            cancel = self._cancellations.get(package_id)
            if cancel is not None:
                cancel.set()
            return self.get(package_id)

    def shutdown(self) -> None:
        with self._guard:
            active = list(self._cancellations.values())
            threads = list(self._threads.values())
        for cancel in active:
            cancel.set()
        for thread in threads:
            thread.join(timeout=6)

    def _run_install(
        self,
        package: ModelPackage,
        job_id: str,
        attempt: int,
        cancel: threading.Event,
        operation_lock: FileLock,
    ) -> None:
        staging = self.root / "staging" / job_id / "payload"
        downloaded_bytes = 0

        def cleanup_staging() -> None:
            shutil.rmtree(self.root / "staging" / job_id, ignore_errors=True)
            staging_root = self.root / "staging"
            try:
                staging_root.rmdir()
            except OSError:
                pass

        def progress(state: str, current_bytes: int) -> None:
            nonlocal downloaded_bytes
            downloaded_bytes = max(downloaded_bytes, current_bytes)
            self._write_status(
                package,
                state=state,
                job_id=job_id,
                attempt=attempt,
                downloaded_bytes=downloaded_bytes,
                error=None,
                install_source=None,
            )

        try:
            staging.mkdir(parents=True)
            progress("downloading", 0)
            self._installer(package, staging, progress, cancel)
            if cancel.is_set():
                raise LocalModelCancelled
            progress("verifying", downloaded_bytes)
            if not _package_is_complete(staging, package):
                raise LocalModelError("模型文件不完整，请重试下载。")
            receipt = self._receipt(package, staging)
            install_id = uuid.uuid4().hex
            installs = self.root / "packages" / package.package_id / "installs"
            destination = installs / install_id
            installs.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
            _atomic_json(destination / "receipt.json", receipt)
            cleanup_staging()
            _atomic_json(
                self.root / "packages" / package.package_id / "current.json",
                {
                    "schema_version": "1.0",
                    "package_id": package.package_id,
                    "install_id": install_id,
                },
            )
            self._write_status(
                package,
                state="ready",
                job_id=job_id,
                attempt=attempt,
                downloaded_bytes=downloaded_bytes,
                error=None,
                install_source="managed",
            )
        except LocalModelCancelled:
            cleanup_staging()
            self._write_status(
                package,
                state="cancelled",
                job_id=job_id,
                attempt=attempt,
                downloaded_bytes=downloaded_bytes,
                error=None,
                install_source=None,
            )
        except LocalModelError as error:
            cleanup_staging()
            self._write_status(
                package,
                state="failed",
                job_id=job_id,
                attempt=attempt,
                downloaded_bytes=downloaded_bytes,
                error=str(error),
                install_source=None,
            )
        except Exception:
            cleanup_staging()
            self._write_status(
                package,
                state="failed",
                job_id=job_id,
                attempt=attempt,
                downloaded_bytes=downloaded_bytes,
                error="模型下载失败，请检查网络后重试。",
                install_source=None,
            )
        finally:
            operation_lock.release()
            cleanup_staging()
            with self._guard:
                self._cancellations.pop(package.package_id, None)
                self._threads.pop(package.package_id, None)

    def _status(self, package: ModelPackage) -> dict[str, Any]:
        return _read_json(self._status_path(package)) or {
            "state": "not_installed",
            "attempt": 0,
            "downloaded_bytes": 0,
            "error": None,
            "install_source": None,
        }

    def _write_status(self, package: ModelPackage, **fields: Any) -> None:
        with self._guard:
            _atomic_json(
                self._status_path(package),
                {
                    "schema_version": "1.0",
                    "package_id": package.package_id,
                    **fields,
                },
            )

    def _status_path(self, package: ModelPackage) -> Path:
        return self.root / "jobs" / f"{package.package_id}.json"

    def _operation_lock(self, package: ModelPackage) -> FileLock:
        lock_path = self.root / "locks" / f"{package.package_id}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(lock_path, thread_local=False)

    def _external(self, package: ModelPackage) -> object | None:
        if package.capability == "asr":
            return _external_asr_install(package)
        return _external_ocr_install(package)

    def _public(self, package: ModelPackage, status: dict[str, Any]) -> dict[str, Any]:
        state = status.get("state", "not_installed")
        public_state = "downloading" if state in _ACTIVE_STATES else state
        if public_state == "cancelled":
            public_state = "interrupted"
        downloaded = max(0, int(status.get("downloaded_bytes", 0)))
        percent = (
            min(99, round(downloaded * 100 / package.expected_bytes))
            if state in _ACTIVE_STATES and package.expected_bytes
            else (100 if state in {"ready", "external_ready"} else None)
        )
        messages = {
            "not_installed": "尚未下载到语栖模型仓。",
            "queued": "正在准备下载。",
            "downloading": "正在下载模型文件。",
            "verifying": "下载完成，正在校验模型文件。",
            "ready": "模型由语栖管理，可以使用。",
            "external_ready": "发现其他程序下载的可用模型；语栖不会改动它。",
            "cancelled": "下载已取消，可以重新开始。",
            "interrupted": "上次下载被中断，可以重新开始。",
        }
        return {
            "asset_id": package.package_id,
            "package_id": package.package_id,
            "capability": package.capability,
            "name": "Faster Whisper" if package.capability == "asr" else "PaddleOCR",
            "version": "large-v3" if package.capability == "asr" else "PP-OCRv6 medium",
            "display_name": package.display_name,
            "size_label": "约 3.1 GB" if package.capability == "asr" else "约 140 MB",
            "components": [component.repo_id for component in package.components],
            "state": public_state,
            "expected_bytes": package.expected_bytes,
            "downloaded_bytes": downloaded,
            "progress_percent": percent,
            "attempt": int(status.get("attempt", 0)),
            "error": status.get("error"),
            "message": status.get("error") or messages.get(str(state)),
            "install_source": status.get("install_source"),
            "managed": state == "ready",
            "can_download": state
            in {"not_installed", "external_ready", *_RETRY_STATES},
            "can_install": state in {"not_installed", "external_ready", *_RETRY_STATES},
            "can_cancel": state in _ACTIVE_STATES,
            "can_retry": state in _RETRY_STATES,
        }

    def _recover_interrupted_jobs(self) -> None:
        for package in _PACKAGES:
            status = self._status(package)
            if status.get("state") in _ACTIVE_STATES:
                lock = self._operation_lock(package)
                try:
                    with lock.acquire(timeout=0):
                        self._write_status(
                            package,
                            state="interrupted",
                            job_id=status.get("job_id"),
                            attempt=int(status.get("attempt", 0)),
                            downloaded_bytes=int(status.get("downloaded_bytes", 0)),
                            error="上次模型下载被中断，可以重新下载。",
                            install_source=None,
                        )
                except Timeout:
                    continue

    @staticmethod
    def _receipt(package: ModelPackage, staging: Path) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        for component in package.components:
            for name in component.required_files:
                path = staging / component.name / name
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                files.append(
                    {
                        "component": component.name,
                        "name": name,
                        "size": path.stat().st_size,
                        "sha256": digest.hexdigest(),
                    }
                )
        return {
            "schema_version": "1.0",
            "package_id": package.package_id,
            "components": [
                {
                    "name": component.name,
                    "repo_id": component.repo_id,
                    "revision": component.revision,
                }
                for component in package.components
            ],
            "files": files,
        }
