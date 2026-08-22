"""Secret-free Windows launcher configuration for the local WebUI."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class LauncherCancelled(RuntimeError):
    """The user closed the first-run directory picker without choosing a root."""


class LauncherConfigError(ValueError):
    """The launcher configuration cannot safely select one output root."""


@dataclass(frozen=True)
class LauncherConfig:
    output_root: str


DirectorySelector = Callable[[], Path | None]


def launcher_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise LauncherConfigError(
            "Windows local application data directory is unavailable"
        )
    return Path(local_app_data) / "LearnNest" / "launcher.json"


def load_launcher_config(path: Path | None = None) -> LauncherConfig:
    config_path = path or launcher_config_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LauncherConfigError("launcher configuration is unavailable") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "1.0"
        or not isinstance(payload.get("output_root"), str)
    ):
        raise LauncherConfigError("launcher configuration is invalid")
    root = _validated_output_root(Path(payload["output_root"]))
    return LauncherConfig(output_root=str(root))


def resolve_output_root(
    config_path: Path | None = None,
    *,
    select_directory: DirectorySelector | None = None,
) -> Path:
    """Load a prior root or perform one safe, explicit first-run selection."""
    path = config_path or launcher_config_path()
    if path.is_file():
        try:
            return Path(load_launcher_config(path).output_root)
        except LauncherConfigError:
            pass
    selector = select_directory or select_output_root_directory
    selected = selector()
    if selected is None:
        raise LauncherCancelled("未选择学习内容保存位置。")
    root = _validated_output_root(selected)
    _write_config(path, LauncherConfig(output_root=str(root)))
    return root


def save_launcher_output_root(
    output_root: str | Path,
    config_path: Path | None = None,
) -> Path:
    """Validate and save the output root used by the next normal WebUI launch."""
    root = _validated_output_root(Path(output_root))
    _write_config(
        config_path or launcher_config_path(),
        LauncherConfig(output_root=str(root)),
    )
    return root


def select_output_root_directory() -> Path | None:
    """Use Windows' native folder picker without collecting any file contents."""
    try:
        from tkinter import Tk, filedialog

        window = Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="选择语栖学习内容的保存位置")
        window.destroy()
    except Exception as error:
        raise LauncherConfigError("无法打开保存位置选择窗口") from error
    return Path(selected) if selected else None


def launch_web_app(*, port: int = 8765) -> None:
    """Resolve exactly one root, then run the loopback WebUI for this process."""
    root = resolve_output_root()
    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.4, lambda: webbrowser.open(url, new=1)).start()
    from learnnest.web_app import serve_web_app

    serve_web_app(root, port=port)


def _validated_output_root(value: Path) -> Path:
    if not value.is_absolute():
        raise LauncherConfigError("保存位置必须是绝对路径")
    try:
        root = value.resolve()
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(delete=False, dir=root) as stream:
            probe = Path(stream.name)
        probe.unlink(missing_ok=True)
    except OSError as error:
        raise LauncherConfigError("保存位置不可写") from error
    return root


def _write_config(path: Path, config: LauncherConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "output_root": config.output_root,
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=path.parent
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)
