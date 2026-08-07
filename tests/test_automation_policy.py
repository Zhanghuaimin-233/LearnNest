from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from learnnest.automation_models import AutomationBudget, AutomationPolicy
from learnnest.automation_runner import AutomationRunResult
from learnnest.automation_store import (
    authorize,
    disable,
    load_status,
    policy_sha256,
    save_policy,
    save_tick_result,
)
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.cli import app
from learnnest.download_queue import DownloadOutcome
from learnnest.provider_profiles import connect, update_limits
from learnnest.schedule_models import ScheduleRecord
from learnnest.schedule_store import write_schedule_atomic


runner = CliRunner()


def _snapshot() -> AssistedConnectionSnapshot:
    return AssistedConnectionSnapshot(
        connection_name="main",
        provider="example",
        endpoint_identity="https://example.test/v1",
        model="model-a",
        adapter_revision="1",
    )


def _policy() -> AutomationPolicy:
    return AutomationPolicy(
        schedule_id="douyin-favorites",
        writer=_snapshot(),
        reviewer=_snapshot(),
        paid_retry_limit=1,
        budget=AutomationBudget(writer_per_day=1, reviewer_per_day=2),
    )


def _schedule(root: Path) -> None:
    now = datetime(2026, 7, 28, tzinfo=UTC)
    write_schedule_atomic(
        root,
        ScheduleRecord(
            schedule_id="douyin-favorites",
            status="enabled",
            source={"kind": "douyin", "url": "https://douyin.example/favorites"},
            trigger={"kind": "manual"},
            profile="evidence",
            created_at=now,
            updated_at=now,
        ),
    )


def test_policy_is_disabled_until_explicit_authorization(tmp_path: Path) -> None:
    saved = save_policy(tmp_path, _policy())

    assert saved.policy.enabled is False
    assert saved.policy_sha256
    assert authorize(tmp_path, now=datetime(2026, 7, 28, tzinfo=UTC)).policy.enabled
    assert disable(tmp_path).policy.enabled is False


def test_policy_change_does_not_reuse_a_prior_tick_summary(tmp_path: Path) -> None:
    save_policy(tmp_path, _policy())
    save_tick_result(
        tmp_path,
        when=datetime(2026, 7, 28, tzinfo=UTC),
        summary="old policy tick",
    )

    updated = save_policy(
        tmp_path, _policy().model_copy(update={"paid_retry_limit": 0})
    )

    assert updated.last_tick_at is None
    assert updated.last_tick_summary is None


def test_policy_migrates_legacy_schedule_and_freezes_default_output() -> None:
    legacy = _policy().model_dump(mode="json")
    legacy["schema_version"] = "1.1"
    legacy.pop("default_output")

    migrated = AutomationPolicy.model_validate(legacy)
    note_only = AutomationPolicy(
        schedule_id=None,
        writer=_snapshot(),
        reviewer=_snapshot(),
        default_output="complete_note",
    )
    note_with_audio = note_only.model_copy(
        update={"default_output": "complete_note_with_audio"}
    )

    assert migrated.schedule_id == "douyin-favorites"
    assert migrated.default_output == "complete_note_with_audio"
    assert policy_sha256(note_only) != policy_sha256(note_with_audio)


def test_cli_configures_retry_limit_without_persisting_secret(tmp_path: Path) -> None:
    _schedule(tmp_path)
    connect(
        tmp_path,
        name="main",
        preset="mimo",
        secret_value="automation-test-key",
    )
    update_limits(
        tmp_path,
        retries_per_role=2,
        global_calls_per_day=80,
        budget_group_calls_per_day={
            "note": 20,
            "podcast": 20,
            "tts": 20,
            "asr": 20,
            "ocr": 20,
        },
    )

    result = runner.invoke(
        app,
        [
            "automation",
            "configure",
            "douyin-favorites",
            "--writer-connection",
            "main",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    status = load_status(tmp_path)
    assert status is not None
    assert status.policy.enabled is False
    assert status.policy.paid_retry_limit == 2
    persisted = (tmp_path / ".learnnest" / "automation" / "status.json").read_text(
        encoding="utf-8"
    )
    assert "automation-test-key" not in persisted


def test_cli_requires_explicit_paid_authorization(tmp_path: Path) -> None:
    save_policy(tmp_path, _policy())

    blocked = runner.invoke(
        app, ["automation", "authorize", "--output-root", str(tmp_path)]
    )
    allowed = runner.invoke(
        app,
        ["automation", "authorize", "--confirm-paid", "--output-root", str(tmp_path)],
    )

    assert blocked.exit_code == 1
    assert "--confirm-paid" in blocked.output
    assert allowed.exit_code == 0, allowed.output
    assert load_status(tmp_path).policy.enabled  # type: ignore[union-attr]


def test_automation_tick_applies_retry_setting_to_every_local_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import learnnest.cli as cli

    _schedule(tmp_path)
    update_limits(
        tmp_path,
        retries_per_role=0,
        global_calls_per_day=80,
        budget_group_calls_per_day={
            "note": 20,
            "podcast": 20,
            "tts": 20,
            "asr": 20,
            "ocr": 20,
        },
    )
    save_policy(
        tmp_path,
        _policy().model_copy(update={"retries_per_stage": 0}),
    )
    authorize(tmp_path, now=datetime(2026, 7, 28, tzinfo=UTC))
    observed: dict[str, int] = {}

    monkeypatch.setattr(
        cli,
        "run_schedule_once",
        lambda *args, **kwargs: SimpleNamespace(status="completed"),
    )

    def fake_downloads(root: Path, **kwargs: object) -> DownloadOutcome:
        del root
        observed["downloads"] = int(kwargs["max_stage_attempts"])
        return DownloadOutcome(None, 0, 0, 0)

    def fake_recoveries(root: Path, **kwargs: object) -> list[object]:
        del root
        observed["recoveries"] = int(kwargs["max_stage_attempts"])
        return []

    monkeypatch.setattr(cli, "run_pending_downloads", fake_downloads)
    monkeypatch.setattr(cli, "run_failure_queue", fake_recoveries)
    monkeypatch.setattr(cli, "_load_douyin_cookie", lambda _root=None: None)
    monkeypatch.setattr(cli, "_runtime_environment", lambda: {})
    monkeypatch.setattr(
        cli,
        "_assisted_note_provider",
        lambda *args, **kwargs: (object(), None),
    )
    monkeypatch.setattr(
        cli,
        "_mimo_api_key",
        lambda *args, **kwargs: SecretStr("fake-key"),
    )
    monkeypatch.setattr(cli, "_verify_provider_workers", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        cli,
        "run_automation_tasks",
        lambda *args, **kwargs: AutomationRunResult((), (), ()),
    )

    result = runner.invoke(
        app,
        ["automation", "tick", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert observed == {"downloads": 1, "recoveries": 1}
