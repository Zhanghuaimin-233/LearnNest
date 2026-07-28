from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from learnnest.automation_models import AutomationBudget, AutomationPolicy
from learnnest.automation_store import (
    authorize,
    disable,
    load_status,
    save_policy,
    save_tick_result,
)
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.cli import app
from learnnest.provider_profiles import connect
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


def test_cli_configures_retry_limit_without_persisting_secret(tmp_path: Path) -> None:
    _schedule(tmp_path)
    connect(
        tmp_path,
        name="main",
        preset="openai-compatible",
        endpoint="https://example.test/v1",
        model="model-a",
        secret_env="AUTOMATION_TEST_KEY",
    )

    result = runner.invoke(
        app,
        [
            "automation",
            "configure",
            "douyin-favorites",
            "--paid-retry-limit",
            "2",
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
    assert "AUTOMATION_TEST_KEY" not in persisted


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
