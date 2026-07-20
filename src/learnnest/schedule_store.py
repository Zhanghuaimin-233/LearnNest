"""Atomic storage for foreground schedule facts."""

from __future__ import annotations

import json
import re
from pathlib import Path

from learnnest.publication import atomic_replace_bytes
from learnnest.schedule_models import ScheduleRecord


def schedule_path(output_root: str | Path, schedule_id: str) -> Path:
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}[a-z0-9]", schedule_id) is None:
        raise ValueError("invalid schedule_id")
    return (
        Path(output_root).resolve()
        / "视频学习批次"
        / "schedules"
        / f"{schedule_id}.json"
    )


def write_schedule_atomic(output_root: str | Path, schedule: ScheduleRecord) -> Path:
    validated = ScheduleRecord.model_validate(schedule.model_dump(mode="python"))
    destination = schedule_path(output_root, validated.schedule_id)
    payload = json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    atomic_replace_bytes(destination, f"{payload}\n".encode("utf-8"))
    return destination


def parse_schedule_bytes(data: bytes) -> ScheduleRecord:
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("schedule JSON root must be an object")
    return ScheduleRecord.model_validate(payload)


def load_schedule(path: str | Path) -> ScheduleRecord:
    candidate = Path(path)
    if candidate.is_dir():
        files = sorted(candidate.glob("*.json"))
        if len(files) != 1:
            raise ValueError("schedule directory must contain exactly one JSON fact")
        candidate = files[0]
    return parse_schedule_bytes(candidate.read_bytes())


def list_schedules(output_root: str | Path) -> list[ScheduleRecord]:
    directory = Path(output_root).resolve() / "视频学习批次" / "schedules"
    if not directory.is_dir():
        return []
    return [load_schedule(path) for path in sorted(directory.glob("*.json"))]
