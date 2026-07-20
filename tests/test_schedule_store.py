from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from learnnest.schedule_models import ScheduleRecord
from learnnest.schedule_store import (
    list_schedules,
    load_schedule,
    write_schedule_atomic,
)


def _schedule() -> ScheduleRecord:
    created = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    return ScheduleRecord(
        schedule_id="folder-a1b2c3d4",
        source={"kind": "folder", "path": "E:/Learning", "recursive": True},
        trigger={"kind": "interval", "every_seconds": 1800},
        profile="evidence",
        status="enabled",
        created_at=created,
        updated_at=created,
        next_tick_at=created + timedelta(minutes=30),
    )


def test_schedule_round_trip_is_a_versioned_fact(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    schedule = _schedule()

    path = write_schedule_atomic(root, schedule)

    assert path == root / "视频学习批次" / "schedules" / "folder-a1b2c3d4.json"
    assert load_schedule(path) == schedule
    assert list_schedules(root) == [schedule]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"


def test_schedule_contract_rejects_cookie_material() -> None:
    payload = _schedule().model_dump(mode="python")
    payload["cookie"] = "sensitive-session-value"

    with pytest.raises(ValidationError, match="cookie"):
        ScheduleRecord.model_validate(payload)


def test_schedule_requires_timezone_aware_times() -> None:
    payload = _schedule().model_dump(mode="python")
    payload["updated_at"] = datetime(2026, 7, 12, 12, 0)

    with pytest.raises(ValidationError, match="timezone-aware"):
        ScheduleRecord.model_validate(payload)
