from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from learnnest.automation_models import AutomationIntake
from learnnest.automation_store import create_intake, load_intake


def _intake(*, default_output: str = "complete_note_with_audio") -> AutomationIntake:
    return AutomationIntake(
        task_id="20260807-intake",
        source_kind="public_url",
        default_output=default_output,
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
    )


def test_intake_is_atomic_readable_after_restart_and_idempotent(tmp_path: Path) -> None:
    created = create_intake(tmp_path, _intake())

    reloaded = load_intake(tmp_path, created.task_id)
    repeated = create_intake(tmp_path, _intake())

    assert reloaded == created
    assert repeated == created
    assert not list((tmp_path / ".learnnest" / "automation" / "intake").glob("*.tmp"))


def test_intake_rejects_conflicts_and_tampered_facts(tmp_path: Path) -> None:
    created = create_intake(tmp_path, _intake())

    with pytest.raises(ValueError):
        create_intake(tmp_path, _intake(default_output="complete_note"))
    path = tmp_path / ".learnnest" / "automation" / "intake" / f"{created.task_id}.json"
    path.write_text('{"task_id":"other"}', encoding="utf-8")

    with pytest.raises(ValueError):
        load_intake(tmp_path, created.task_id)
