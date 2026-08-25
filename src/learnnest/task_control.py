"""Durable manual scheduling control kept beside one task record."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from learnnest.locks import task_control_lock


CONTROL_FILENAME = "task-control.json"
_WINDOWS_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1)


class TaskControl(BaseModel):
    """Orthogonal user control that must not rewrite execution facts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    manually_paused: bool = False


def load_task_control(task_dir: str | Path, task_id: str) -> TaskControl:
    path = Path(task_dir) / CONTROL_FILENAME
    if not path.is_file():
        return TaskControl(task_id=task_id)
    control = TaskControl.model_validate_json(path.read_bytes())
    if control.task_id != task_id:
        raise ValueError("task control identity is invalid")
    return control


def set_manual_pause(
    output_root: str | Path,
    task_dir: str | Path,
    task_id: str,
    *,
    paused: bool,
) -> TaskControl:
    """Atomically change only the manual scheduling control fact."""
    with task_control_lock(output_root, task_id, timeout=0):
        current = load_task_control(task_dir, task_id)
        updated = current.model_copy(update={"manually_paused": paused})
        _write_task_control_atomic(Path(task_dir), updated)
        return updated


def _write_task_control_atomic(task_dir: Path, control: TaskControl) -> Path:
    task_dir.mkdir(parents=True, exist_ok=True)
    destination = task_dir / CONTROL_FILENAME
    serialized = json.dumps(
        control.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=task_dir, suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(f"{serialized}\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        for delay in (*_WINDOWS_REPLACE_RETRY_DELAYS, None):
            try:
                os.replace(temporary_path, destination)
                break
            except OSError as error:
                if (
                    delay is None
                    or os.name != "nt"
                    or getattr(error, "winerror", error.errno) not in {5, 32}
                ):
                    raise
                time.sleep(delay)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return destination
