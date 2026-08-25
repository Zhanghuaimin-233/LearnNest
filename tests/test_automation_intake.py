from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from learnnest.automation_models import AutomationIntake
from learnnest.automation_store import create_intake, load_intake


def _intake(
    *,
    task_id: str = "20260807-intake",
    default_output: str = "complete_note_with_audio",
) -> AutomationIntake:
    return AutomationIntake(
        task_id=task_id,
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


@pytest.mark.parametrize("winerror", [5, 32])
def test_automation_facts_retry_transient_windows_replacement_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    calls = 0
    real_replace = Path.replace

    def flaky_replace(source: Path, destination: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(winerror, "sharing violation", str(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    created = create_intake(tmp_path, _intake())

    assert calls == 3
    assert load_intake(tmp_path, created.task_id) == created


def test_automation_facts_clean_temporary_file_when_replacement_conflict_exhausts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake_dir = tmp_path / ".learnnest" / "automation" / "intake"

    def always_conflicted(_source: Path, destination: Path) -> Path:
        raise PermissionError(5, "sharing violation", str(destination))

    monkeypatch.setattr(Path, "replace", always_conflicted)

    with pytest.raises(PermissionError):
        create_intake(tmp_path, _intake(task_id="20260807-conflicted"))

    assert list(intake_dir.iterdir()) == []
