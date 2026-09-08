"""Foreground-only automatic delivery for one local WebUI process."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from learnnest.automation_models import AutomationIntake
from learnnest.automation_runner import (
    AutomationProviders,
    AutomationRunResult,
    run_automation_tasks,
)
from learnnest.automation_store import (
    create_intake,
    find_intake,
    list_intakes,
    load_status,
    load_task_state,
    save_intake,
    task_has_execution_facts,
    task_is_zero_attempt_budget_blocked,
)
from learnnest.learning_state import (
    automation_readiness,
    required_automation_roles,
    task_has_paid_automation_trace,
)
from learnnest.locks import LockUnavailable, task_control_lock, task_lock
from learnnest.models import TaskRecord
from learnnest.provider_profiles import (
    freeze_role_bindings,
    load_settings,
    settings_sha256,
)
from learnnest.provider_service import (
    assisted_provider_from_snapshot,
    podcast_provider_from_snapshot,
    tts_provider_from_snapshot,
)
from learnnest.task_store import find_task_by_id, load_task, write_task_atomic
from learnnest.task_control import load_task_control

RunTasks = Callable[..., AutomationRunResult]
Clock = Callable[[], datetime]
ProviderFactory = Callable[[Path, str], AutomationProviders]
FavoritesSync = Callable[[], None]


class AutomationCoordinator:
    """Run one authorized intake queue only while its FastAPI app is alive."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        run_tasks: RunTasks | None = None,
        clock: Clock | None = None,
        provider_factory: ProviderFactory | None = None,
        favorites_sync: FavoritesSync | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._provider_factory = provider_factory or automation_providers_for_task
        self._run_tasks = run_tasks or self._run_authorized_tasks
        self._favorites_sync = favorites_sync
        # An explicit wall-clock interval override for tests and deployments;
        # the configured minutes remain the default source of truth.
        self._interval_seconds = interval_seconds
        self._tick_lock = threading.Lock()
        self._wake_lock = threading.Lock()
        self._wake_generation = 0
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None

    def set_favorites_sync(self, sync: FavoritesSync) -> None:
        """Bind the app's one favorites sync entry after service construction."""
        self._favorites_sync = sync

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        with self._wake_lock:
            self._wake_generation = 0
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run_forever())

    async def shutdown(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            await task
        self._task = None
        self._loop = None

    def wake(self) -> bool:
        """Request one prompt background tick without running it on this thread."""
        loop = self._loop
        if not self.running or loop is None or loop.is_closed():
            return False
        with self._wake_lock:
            self._wake_generation += 1
        loop.call_soon_threadsafe(self._wake.set)
        return True

    async def _run_forever(self) -> None:
        seen_wake_generation = 0
        startup_sync_pending = True
        while not self._stop.is_set():
            if startup_sync_pending:
                startup_sync_pending = False
                await asyncio.to_thread(self._run_background_favorites_sync)
            result = await asyncio.to_thread(self.tick_once)
            if (
                result is not None
                and result.task_ids
                and await asyncio.to_thread(self._has_pending_intake)
            ):
                continue
            try:
                seen_wake_generation = await asyncio.wait_for(
                    self._wait_for_wake_or_stop(seen_wake_generation),
                    timeout=(
                        self._interval_seconds
                        if self._interval_seconds is not None
                        else self._interval_minutes() * 60
                    ),
                )
            except TimeoutError:
                # Only a periodic timeout (and the single startup sync above)
                # refreshes Douyin favorites; a plain wake never does.
                await asyncio.to_thread(self._run_background_favorites_sync)
                continue

    def _run_background_favorites_sync(self) -> None:
        """Run the app's favorites sync without ever failing this loop."""
        if self._favorites_sync is None:
            return
        try:
            self._favorites_sync()
        except Exception:
            # The service records observable failure states itself; an
            # unexpected fault must not kill the intake loop.
            return

    def _has_pending_intake(self) -> bool:
        """Drain an existing backlog without waiting for another wake signal."""
        try:
            return any(
                intake.status == "pending"
                and not _task_is_manually_paused(self.output_root, intake.task_id)
                for intake in list_intakes(self.output_root)
            )
        except ValueError:
            return False

    async def _wait_for_wake_or_stop(self, seen_generation: int) -> int:
        with self._wake_lock:
            current_generation = self._wake_generation
        if current_generation != seen_generation:
            return current_generation
        # Clearing is guarded by a generation check: a cross-thread wake that
        # races with this reset is observed immediately instead of being lost.
        self._wake.clear()
        with self._wake_lock:
            current_generation = self._wake_generation
        if current_generation != seen_generation:
            self._wake.set()
            return current_generation
        stop = asyncio.create_task(self._stop.wait())
        wake = asyncio.create_task(self._wake.wait())
        done, pending = await asyncio.wait(
            {stop, wake}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if stop in done:
            return seen_generation
        with self._wake_lock:
            return self._wake_generation

    def tick_once(self) -> AutomationRunResult | None:
        """Claim durable intakes once; a concurrent wake-up is intentionally ignored."""
        if not self._tick_lock.acquire(blocking=False):
            return None
        try:
            status = load_status(self.output_root)
            if status is None or not _authorization_is_current(self.output_root):
                return None
            intakes = list_intakes(self.output_root)
            selected = tuple(
                item
                for item in intakes
                if item.status in {"pending", "claimed"}
                and not _task_is_manually_paused(self.output_root, item.task_id)
            )[: status.policy.max_items_per_tick]
            if not selected:
                return AutomationRunResult((), (), ())
            prepared: list[AutomationIntake] = []
            for intake in selected:
                try:
                    execution = load_task_state(
                        self.output_root, intake.task_id, status.policy_sha256
                    )
                    if (
                        execution is not None
                        and execution.blocked_reason == "retry_wait"
                    ):
                        # Only the explicit Web retry admission clears this
                        # persisted gate; an intake status alone cannot do so.
                        continue
                    _prepare_intake_task(self.output_root, intake)
                except _ImmutableTaskIdentity:
                    save_intake(
                        self.output_root,
                        intake.model_copy(update={"status": "needs_attention"}),
                    )
                    continue
                except (LockUnavailable, OSError, ValueError):
                    # A missing role, stale configuration, corrupt task fact, or
                    # failed atomic write stays safely pending for later repair.
                    continue
                prepared.append(intake)
            if not prepared:
                return None
            task_ids = tuple(item.task_id for item in prepared)
            outputs = {item.task_id: item.default_output for item in prepared}
            result = self._run_tasks(
                self.output_root,
                task_ids,
                default_outputs=outputs,
                now=self._clock(),
            )
            for intake in prepared:
                if intake.task_id in result.completed_task_ids:
                    save_intake(
                        self.output_root,
                        intake.model_copy(update={"status": "completed"}),
                    )
                elif intake.task_id in result.failed_task_ids:
                    save_intake(
                        self.output_root,
                        intake.model_copy(update={"status": "needs_attention"}),
                    )
            return result
        except ValueError:
            # A corrupt intake or invalid authorization is fail-closed.  The
            # runner and therefore every Provider factory remain untouched.
            return None
        finally:
            self._tick_lock.release()

    def _interval_minutes(self) -> float:
        status = load_status(self.output_root)
        if status is None:
            return 30
        return status.policy.check_interval_minutes

    def _run_authorized_tasks(
        self,
        root: Path,
        task_ids: tuple[str, ...],
        *,
        default_outputs: dict[str, str],
        now: datetime,
    ) -> AutomationRunResult:
        return run_automation_tasks(
            root,
            task_ids,
            provider_factory=lambda task_id: self._provider_factory(root, task_id),
            default_outputs=default_outputs,
            now=now,
        )


def create_intake_for_task(
    output_root: str | Path,
    *,
    task_id: str,
    source_kind: Literal["local_video", "public_url", "douyin_favorite"],
    default_output: Literal["complete_note", "complete_note_with_audio"],
    now: datetime | None = None,
) -> AutomationIntake:
    """Persist a user request before a foreground tick can ever claim it."""
    existing = find_intake(output_root, task_id)
    if existing is not None:
        if existing.source_kind != source_kind:
            raise ValueError("automation intake conflicts with its frozen identity")
        return existing
    return create_intake(
        output_root,
        AutomationIntake(
            task_id=task_id,
            source_kind=source_kind,
            default_output=default_output,
            created_at=(now or datetime.now(UTC)).astimezone(UTC),
        ),
    )


def _authorization_is_current(output_root: Path) -> bool:
    status = load_status(output_root)
    if status is None:
        return False
    policy = status.policy
    return (
        policy.enabled
        and policy.authorized_at is not None
        and policy.provider_settings_sha256
        == settings_sha256(load_settings(output_root))
    )


class _ImmutableTaskIdentity(ValueError):
    """A task already has paid/automation facts and cannot be rebound."""


def _prepare_intake_task(output_root: Path, intake: AutomationIntake) -> None:
    """Freeze identity and claim only while the task remains schedulable."""
    if automation_readiness(output_root, intake) != "ready":
        raise ValueError("automation setup is not ready")
    current = freeze_role_bindings(output_root)
    required = required_automation_roles(intake.default_output)
    if any(role not in current for role in required):
        raise ValueError("automation roles are incomplete")
    with (
        task_lock(output_root, intake.task_id, timeout=0),
        task_control_lock(output_root, intake.task_id, timeout=0),
    ):
        found = find_task_by_id(output_root, intake.task_id)
        if found is None:
            raise ValueError("automation task is missing")
        task_dir, task = found
        if load_task_control(task_dir, task.task_id).manually_paused:
            raise ValueError("automation task is manually paused")
        if not _task_bindings_match(task, current, required):
            has_execution_facts = task_has_execution_facts(output_root, intake.task_id)
            safe_budget_restart = (
                has_execution_facts
                and task_is_zero_attempt_budget_blocked(output_root, intake.task_id)
                and _task_bindings_match_except_settings_sha(task, current, required)
            )
            if (
                has_execution_facts and not safe_budget_restart
            ) or _has_paid_task_trace(task):
                raise _ImmutableTaskIdentity("automation task binding is immutable")
            updated = TaskRecord.model_validate(
                {
                    **task.model_dump(mode="python"),
                    "provider_bindings": {
                        role: binding.model_dump(mode="python")
                        for role, binding in current.items()
                    },
                    "provider_settings_sha256": settings_sha256(
                        load_settings(output_root)
                    ),
                }
            )
            write_task_atomic(task_dir, updated)
        if intake.status == "pending":
            save_intake(
                output_root,
                intake.model_copy(update={"status": "claimed"}),
            )


def _task_is_manually_paused(output_root: Path, task_id: str) -> bool:
    try:
        found = find_task_by_id(output_root, task_id)
        return (
            found is not None
            and load_task_control(found[0], found[1].task_id).manually_paused
        )
    except (OSError, ValueError):
        return True


def _task_bindings_match(
    task: TaskRecord, current: dict[str, object], required: tuple[str, ...]
) -> bool:
    for role in required:
        frozen = task.provider_bindings.get(role)
        configured = current.get(role)
        if frozen is None or configured is None:
            return False
        if frozen.model_dump(mode="json") != configured.model_dump(mode="json"):
            return False
    return True


def _task_bindings_match_except_settings_sha(
    task: TaskRecord, current: dict[str, object], required: tuple[str, ...]
) -> bool:
    for role in required:
        frozen = task.provider_bindings.get(role)
        configured = current.get(role)
        if frozen is None or configured is None:
            return False
        if frozen.model_dump(mode="json", exclude={"settings_sha256"}) != (
            configured.model_dump(  # type: ignore[union-attr]
                mode="json", exclude={"settings_sha256"}
            )
        ):
            return False
    return True


def _has_paid_task_trace(task: TaskRecord) -> bool:
    return task_has_paid_automation_trace(task)


def automation_providers_for_task(root: Path, task_id: str) -> AutomationProviders:
    """Build only the task's immutable providers after runner admission."""
    found = find_task_by_id(root, task_id)
    if found is None:
        raise ValueError("automation task is missing")
    task_dir, _task = found
    task = load_task(task_dir)
    try:
        writer = task.provider_bindings["note_writer"]
        reviewer = task.provider_bindings["note_reviewer"]
    except KeyError as error:
        raise ValueError(
            "automation task is missing frozen note role bindings"
        ) from error
    podcast = task.provider_bindings.get("podcast")
    tts = task.provider_bindings.get("tts")
    return AutomationProviders(
        writer=assisted_provider_from_snapshot(str(root), writer),
        reviewer=assisted_provider_from_snapshot(str(root), reviewer),
        podcast=(
            podcast_provider_from_snapshot(str(root), podcast)
            if podcast is not None
            else _UnavailableAutomationProvider()
        ),
        tts=(
            _UnavailableAutomationProvider()
            if tts is None
            else tts_provider_from_snapshot(str(root), tts)
            if tts.provider == "windows-tts"
            else _LazyFrozenTtsProvider(root, tts)
        ),
    )


class _UnavailableAutomationProvider:
    """A role omitted by a note-only task; invocation remains fail-closed."""

    billing = "paid"
    name = "unavailable"
    model = "unavailable"
    endpoint_identity = ""

    def __getattr__(self, _name: str) -> object:
        raise ValueError("automation task is missing frozen audio role bindings")


class _LazyFrozenTtsProvider:
    """Keep cloud TTS client construction inside its admitted synthesis call."""

    billing = "paid"
    voice = "冰糖"

    def __init__(self, root: Path, binding: object) -> None:
        self._root = root
        self._binding = binding
        self.name = str(getattr(binding, "provider"))
        self.model = str(getattr(binding, "model"))

    def synthesize(self, speech_text: str, style_instruction: str) -> bytes:
        provider = tts_provider_from_snapshot(str(self._root), self._binding)  # type: ignore[arg-type]
        self.voice = provider.voice
        return provider.synthesize(speech_text, style_instruction)
