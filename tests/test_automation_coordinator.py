from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.automation_coordinator import AutomationCoordinator
from learnnest.automation_models import (
    AutomationIntake,
    AutomationPolicy,
    AutomationTaskState,
)
from learnnest.automation_runner import AutomationRunResult
from learnnest.automation_store import (
    authorize,
    create_intake,
    load_intake,
    load_status,
    save_policy,
    save_task_state,
)
from learnnest.models import ProviderBindingSnapshot, StageStatus
from learnnest.provider_profiles import (
    connect,
    freeze_role_bindings,
    load_settings,
    set_role_binding,
    settings_sha256,
)
from learnnest.task_store import create_task, load_task, write_task_atomic


def _snapshot() -> AssistedConnectionSnapshot:
    return AssistedConnectionSnapshot(
        connection_name="note",
        connection_id="note",
        provider="xiaomi-mimo",
        endpoint_identity="https://api.xiaomimimo.com/v1",
        model="mimo-v2.5",
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


def _material_task(
    root: Path,
    task_id: str,
    *,
    bindings: dict[str, ProviderBindingSnapshot] | None = None,
) -> Path:
    task_dir = root / "视频学习素材" / task_id
    task = create_task(
        task_id=task_id,
        source_path="C:/private/lesson.mp4",
        source_fingerprint=task_id,
        title="自动化课程",
        provider_bindings=bindings,
    ).model_copy(
        update={
            "stages": {"content_pack": StageStatus.COMPLETED},
            "artifacts": {"content_pack": ["content_pack.json"]},
        }
    )
    write_task_atomic(task_dir, task)
    return task_dir


def _stale_bindings(root: Path) -> dict[str, ProviderBindingSnapshot]:
    return {
        role: ProviderBindingSnapshot.model_validate(
            binding.model_copy(update={"settings_sha256": "a" * 64}).model_dump(
                mode="python"
            )
        )
        for role, binding in freeze_role_bindings(root).items()
    }


def test_coordinator_runs_startup_tick_with_the_intake_frozen_output(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    _material_task(root, "20260807-intake")
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
    _material_task(root, "20260807-restart")
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
    _material_task(root, "20260807-shutdown")
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


def test_coordinator_coalesces_threadsafe_wakes_without_concurrent_runner(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    _material_task(root, "20260807-wake")
    create_intake(
        root,
        AutomationIntake(
            task_id="20260807-wake",
            source_kind="local_video",
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
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        threads = [Thread(target=coordinator.wake) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)
        release.set()
        await asyncio.sleep(0.05)
        await coordinator.shutdown()
        assert coordinator.wake() is False

    asyncio.run(exercise())
    assert calls == [("20260807-wake",)]


def test_coordinator_drains_pending_backlog_after_wakes_are_coalesced(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    task_ids = tuple(f"20260807-backlog-{index}" for index in range(3))
    for task_id in task_ids:
        _material_task(root, task_id)
        create_intake(
            root,
            AutomationIntake(
                task_id=task_id,
                source_kind="local_video",
                default_output="complete_note",
                created_at=datetime(2026, 8, 7, tzinfo=UTC),
            ),
        )
    calls: list[tuple[str, ...]] = []

    def run_tasks(
        _root: Path, selected: tuple[str, ...], **_kwargs: object
    ) -> AutomationRunResult:
        calls.append(selected)
        return AutomationRunResult(selected, selected, ())

    async def exercise() -> None:
        coordinator = AutomationCoordinator(root, run_tasks=run_tasks)
        await coordinator.start()
        for _ in range(100):
            if len(calls) == len(task_ids):
                break
            await asyncio.sleep(0.01)
        await coordinator.shutdown()

    asyncio.run(exercise())
    assert calls == [(task_id,) for task_id in task_ids]
    assert all(load_intake(root, task_id).status == "completed" for task_id in task_ids)


def test_coordinator_keeps_the_intake_output_after_policy_changes(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    _material_task(root, "20260807-frozen-output")
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


def test_coordinator_waits_for_missing_audio_roles_without_running_or_factory(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    save_policy(
        root,
        AutomationPolicy(
            writer=_snapshot(),
            reviewer=_snapshot(),
            default_output="complete_note_with_audio",
        ),
    )
    authorize(root, now=datetime(2026, 8, 7, tzinfo=UTC))
    task_id = "20260807-missing-audio"
    task_dir = _material_task(root, task_id)
    create_intake(
        root,
        AutomationIntake(
            task_id=task_id,
            source_kind="local_video",
            default_output="complete_note_with_audio",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        ),
    )
    runs: list[tuple[str, ...]] = []
    factory_calls: list[str] = []
    coordinator = AutomationCoordinator(
        root,
        run_tasks=lambda _root, task_ids, **_kwargs: (
            runs.append(task_ids),
            AutomationRunResult(task_ids, task_ids, ()),
        )[1],
        provider_factory=lambda _root, task: (
            factory_calls.append(task),
            AssertionError("factory must not run"),
        )[1],
    )

    assert coordinator.tick_once() is None
    assert runs == []
    assert factory_calls == []
    assert load_intake(root, task_id).status == "pending"
    assert load_task(task_dir).provider_bindings == {}


def test_coordinator_keeps_stale_authorization_pending_without_running_or_factory(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    status = load_status(root)
    assert status is not None
    save_policy(
        root,
        status.policy.model_copy(update={"provider_settings_sha256": "a" * 64}),
    )
    task_id = "20260807-stale-authorization"
    task_dir = _material_task(root, task_id)
    create_intake(
        root,
        AutomationIntake(
            task_id=task_id,
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        ),
    )
    original = load_task(task_dir)
    runs: list[tuple[str, ...]] = []
    factory_calls: list[str] = []
    coordinator = AutomationCoordinator(
        root,
        run_tasks=lambda _root, task_ids, **_kwargs: (
            runs.append(task_ids),
            AutomationRunResult(task_ids, task_ids, ()),
        )[1],
        provider_factory=lambda _root, task: (
            factory_calls.append(task),
            AssertionError("factory must not run"),
        )[1],
    )

    assert coordinator.tick_once() is None
    assert runs == []
    assert factory_calls == []
    assert load_intake(root, task_id).status == "pending"
    assert load_task(task_dir) == original


def test_coordinator_rebinds_only_an_unexecuted_stale_task_before_claiming(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    task_id = "20260807-rebind-once"
    task_dir = _material_task(root, task_id, bindings=_stale_bindings(root))
    create_intake(
        root,
        AutomationIntake(
            task_id=task_id,
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        ),
    )
    runs: list[tuple[str, ...]] = []
    coordinator = AutomationCoordinator(
        root,
        run_tasks=lambda _root, task_ids, **_kwargs: (
            runs.append(task_ids),
            AutomationRunResult(task_ids, task_ids, ()),
        )[1],
    )

    coordinator.tick_once()
    frozen = load_task(task_dir)
    coordinator.tick_once()

    assert runs == [(task_id,)]
    assert {
        role: binding.model_dump(mode="json")
        for role, binding in frozen.provider_bindings.items()
    } == {
        role: binding.model_dump(mode="json")
        for role, binding in freeze_role_bindings(root).items()
    }
    assert frozen.provider_settings_sha256 == settings_sha256(load_settings(root))
    assert load_task(task_dir) == frozen


def test_coordinator_never_rebinds_an_executed_stale_task_or_enters_runner(
    tmp_path: Path,
) -> None:
    root = _authorized_root(tmp_path)
    task_id = "20260807-executed-stale"
    task_dir = _material_task(root, task_id, bindings=_stale_bindings(root))
    status = load_status(root)
    assert status is not None
    save_task_state(
        root,
        AutomationTaskState(task_id=task_id, policy_sha256=status.policy_sha256),
    )
    create_intake(
        root,
        AutomationIntake(
            task_id=task_id,
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime(2026, 8, 7, tzinfo=UTC),
        ),
    )
    original = load_task(task_dir)
    runs: list[tuple[str, ...]] = []
    factory_calls: list[str] = []
    coordinator = AutomationCoordinator(
        root,
        run_tasks=lambda _root, task_ids, **_kwargs: (
            runs.append(task_ids),
            AutomationRunResult(task_ids, task_ids, ()),
        )[1],
        provider_factory=lambda _root, task: (
            factory_calls.append(task),
            AssertionError("factory must not run"),
        )[1],
    )

    assert coordinator.tick_once() is None
    assert runs == []
    assert factory_calls == []
    assert load_task(task_dir) == original
    assert load_intake(root, task_id).status == "needs_attention"
