"""Narrow Windows Task Scheduler integration for one automatic-delivery policy."""

from __future__ import annotations

import json
import os
import hashlib
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from learnnest.automation_store import automation_directory


class SchedulerInstallation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = "1.0"
    task_name: str = Field(pattern=r"^LearnNest-Automation-[a-f0-9]{12}$")
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    interval_minutes: int = Field(ge=1, le=1440)
    executable: str = Field(min_length=1)
    output_root: str = Field(min_length=1)
    installed_at: datetime


@dataclass(frozen=True)
class SchedulerCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str]], SchedulerCommandResult]


def task_name(output_root: str | Path) -> str:
    """Keep one stable scheduler task for an output root across policy changes."""
    identity = str(Path(output_root).resolve()).casefold().encode("utf-8")
    return f"LearnNest-Automation-{hashlib.sha256(identity).hexdigest()[:12]}"


def install(
    output_root: str | Path,
    *,
    policy_sha256: str,
    interval_minutes: int,
    executable: str | Path | None = None,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> SchedulerInstallation:
    """Create or update only this policy's current-user wake-up task."""
    if os.name != "nt":
        raise ValueError("automation install is supported only on Windows")
    if not 1 <= interval_minutes <= 1440:
        raise ValueError("automation interval must be between 1 and 1440 minutes")
    if len(policy_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in policy_sha256
    ):
        raise ValueError("automation policy SHA is invalid")
    root = Path(output_root).resolve()
    selected_executable = Path(executable or _default_executable()).resolve()
    if not selected_executable.is_file() or not selected_executable.is_absolute():
        raise ValueError("automation executable is missing or not absolute")
    installation = SchedulerInstallation(
        task_name=task_name(root),
        policy_sha256=policy_sha256,
        interval_minutes=interval_minutes,
        executable=str(selected_executable),
        output_root=str(root),
        installed_at=(now or datetime.now(UTC)).astimezone(UTC),
    )
    action = _task_action(installation)
    result = (runner or _run)(
        [
            "schtasks.exe",
            "/Create",
            "/TN",
            installation.task_name,
            "/TR",
            action,
            "/SC",
            "MINUTE",
            "/MO",
            str(installation.interval_minutes),
            "/RL",
            "LIMITED",
            "/F",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError("Windows Task Scheduler rejected automation installation")
    _write_json(_installation_path(root), installation.model_dump(mode="json"))
    return installation


def status(
    output_root: str | Path,
    *,
    runner: CommandRunner | None = None,
) -> tuple[SchedulerInstallation | None, bool]:
    root = Path(output_root).resolve()
    installation = _load_installation(root)
    if installation is None:
        return None, False
    if os.name != "nt":
        return installation, False
    result = (runner or _run)(["schtasks.exe", "/Query", "/TN", installation.task_name])
    return installation, result.returncode == 0


def uninstall(output_root: str | Path, *, runner: CommandRunner | None = None) -> bool:
    """Delete only the recorded task after verifying its deterministic ownership name."""
    if os.name != "nt":
        raise ValueError("automation uninstall is supported only on Windows")
    root = Path(output_root).resolve()
    installation = _load_installation(root)
    if installation is None:
        return False
    if not installation.task_name.startswith("LearnNest-Automation-"):
        raise ValueError("stored automation task ownership is invalid")
    result = (runner or _run)(
        ["schtasks.exe", "/Delete", "/TN", installation.task_name, "/F"]
    )
    if result.returncode != 0:
        raise RuntimeError("Windows Task Scheduler rejected automation removal")
    _installation_path(root).unlink(missing_ok=True)
    return True


def _default_executable() -> Path:
    candidate = Path(sys.executable).resolve().parent / "learnnest.exe"
    if candidate.is_file():
        return candidate
    raise ValueError("installed learnnest.exe is unavailable for scheduled restart")


def _task_action(installation: SchedulerInstallation) -> str:
    command = subprocess.list2cmdline(
        [
            installation.executable,
            "automation",
            "tick",
            "--output-root",
            installation.output_root,
        ]
    )
    return subprocess.list2cmdline(
        [
            "cmd.exe",
            "/d",
            "/s",
            "/c",
            f'cd /d "{installation.output_root}" && {command}',
        ]
    )


def _installation_path(root: Path) -> Path:
    return automation_directory(root) / "scheduler.json"


def _load_installation(root: Path) -> SchedulerInstallation | None:
    path = _installation_path(root)
    if not path.is_file():
        return None
    try:
        return SchedulerInstallation.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        raise ValueError("automation scheduler installation is invalid") from error


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=path.parent
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def _run(command: list[str]) -> SchedulerCommandResult:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return SchedulerCommandResult(
        completed.returncode, completed.stdout, completed.stderr
    )
