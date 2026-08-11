from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.automation_coordinator import AutomationCoordinator
from learnnest.automation_models import AutomationIntake, AutomationPolicy
from learnnest.automation_runner import AutomationRunResult
from learnnest.automation_store import (
    authorize,
    create_intake,
    load_intake,
    save_policy,
)
from learnnest.provider_profiles import connect, set_role_binding


def _snapshot() -> AssistedConnectionSnapshot:
    return AssistedConnectionSnapshot(
        connection_name="note",
        connection_id="note",
        provider="xiaomi-mimo",
        endpoint_identity="https://api.example.test/v1",
        model="mimo-v2",
        adapter_revision="1",
    )


def _authorized_root(tmp_path: Path) -> Path:
    connect(tmp_path, name="note", preset="mimo", secret_value="fake-key")
    set_role_binding(tmp_path, role="note_writer", connection_name="note")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="note")
    save_policy(
        tmp_path,
        AutomationPolicy(
            writer=_snapshot(),
            reviewer=_snapshot(),
            default_output="complete_note",
            check_interval_seconds=30,
        ),
    )
    authorize(tmp_path, now=datetime(2026, 8, 7, tzinfo=UTC))
    return tmp_path


def test_coordinator_runs_startup_tick_with_the_intake_frozen_output(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    create_intake(
        root,
        AutomationIntake(
            task_id="20260807-intake",
            source_kind="public_url",
            default_output="complete_note",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        ),
    )
    observed: list[dict[str, str]] = []

    def run_tasks(
        _root: Path,
        task_ids: tuple[str, ...],
        *,
        default_outputs: dict[str, str],
        **_kwargs: object,
    ) -> AutomationRunResult:
        observed.append(default_outputs)
        return AutomationRunResult(task_ids, task_ids, ())

    async def exercise() -> None:
        coordinator = AutomationCoordinator(root, run_tasks=run_tasks)
        await coordinator.start()
        for _ in range(20):
            if observed:
                break
            await asyncio.sleep(0.01)
        await coordinator.shutdown()
        assert coordinator.running is False

    asyncio.run(exercise())

    assert observed == [{"20260807-intake": "complete_note"}]


def test_coordinator_ignores_disabled_intake_without_constructing_a_runner(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []
    create_intake(
        tmp_path,
        AutomationIntake(
            task_id="20260807-disabled",
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        ),
    )
    coordinator = AutomationCoordinator(
        tmp_path,
        run_tasks=lambda _root, task_ids, **_kwargs: (
            calls.append(task_ids),
            AutomationRunResult(task_ids, task_ids, ()),
        )[1],
    )

    assert coordinator.tick_once() is None
    assert calls == []
    assert load_intake(tmp_path, "20260807-disabled").status == "pending"


def test_coordinator_serializes_wakeups_and_keeps_claimed_intake_on_restart(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    create_intake(
        root,
        AutomationIntake(
            task_id="20260807-restart",
            source_kind="public_url",
            default_output="complete_note",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        ),
    )
    entered = Event()
    release = Event()
    calls: list[tuple[str, ...]] = []

    def hold_runner(
        _root: Path, task_ids: tuple[str, ...], **_kwargs: object
    ) -> AutomationRunResult:
        calls.append(task_ids)
        entered.set()
        release.wait(timeout=1)
        return AutomationRunResult(task_ids, (), task_ids)

    coordinator = AutomationCoordinator(root, run_tasks=hold_runner)
    active = Thread(target=coordinator.tick_once)
    active.start()
    assert entered.wait(timeout=1)
    assert coordinator.tick_once() is None
    release.set()
    active.join(timeout=1)

    assert calls == [("20260807-restart",)]
    assert load_intake(root, "20260807-restart").status == "needs_attention"


def test_coordinator_shutdown_waits_for_the_active_tick_and_stops_future_ticks(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    create_intake(
        root,
        AutomationIntake(
            task_id="20260807-shutdown",
            source_kind="public_url",
            default_output="complete_note",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        ),
    )
    entered = Event()
    release = Event()
    calls: list[tuple[str, ...]] = []

    def hold_runner(
        _root: Path, task_ids: tuple[str, ...], **_kwargs: object
    ) -> AutomationRunResult:
        calls.append(task_ids)
        entered.set()
        release.wait(timeout=1)
        return AutomationRunResult(task_ids, task_ids, ())

    async def exercise() -> None:
        coordinator = AutomationCoordinator(root, run_tasks=hold_runner)
        await coordinator.start()
        for _ in range(20):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        stopping = asyncio.create_task(coordinator.shutdown())
        await asyncio.sleep(0.02)
        assert stopping.done() is False
        release.set()
        await stopping
        assert coordinator.running is False

    asyncio.run(exercise())
    assert calls == [("20260807-shutdown",)]


def test_coordinator_keeps_the_intake_output_after_policy_changes(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    create_intake(
        root,
        AutomationIntake(
            task_id="20260807-frozen-output",
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        ),
    )
    save_policy(
        root,
        AutomationPolicy(
            writer=_snapshot(),
            reviewer=_snapshot(),
            default_output="complete_note_with_audio",
            check_interval_seconds=30,
        ),
    )
    authorize(root, now=datetime(2026, 8, 7, tzinfo=UTC))
    observed: list[dict[str, str]] = []

    coordinator = AutomationCoordinator(
        root,
        run_tasks=lambda _root, task_ids, *, default_outputs, **_kwargs: (
            observed.append(default_outputs),
            AutomationRunResult(task_ids, task_ids, ()),
        )[1],
    )

    coordinator.tick_once()

    assert observed == [{"20260807-frozen-output": "complete_note"}]
