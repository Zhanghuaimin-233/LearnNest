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
    list_intakes,
    load_status,
    save_intake,
    task_has_execution_facts,
)
from learnnest.learning_state import automation_readiness, required_automation_roles
from learnnest.locks import LockUnavailable, task_lock
from learnnest.models import StageStatus, TaskRecord
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

RunTasks = Callable[..., AutomationRunResult]
Clock = Callable[[], datetime]
ProviderFactory = Callable[[Path, str], AutomationProviders]


class AutomationCoordinator:
    """Run one authorized intake queue only while its FastAPI app is alive."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        run_tasks: RunTasks | None = None,
        clock: Clock | None = None,
        provider_factory: ProviderFactory | None = None,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._provider_factory = provider_factory or automation_providers_for_task
        self._run_tasks = run_tasks or self._run_authorized_tasks
        self._tick_lock = threading.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run_forever())

    async def shutdown(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            await task
        self._task = None

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            await asyncio.to_thread(self.tick_once)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds()
                )
            except TimeoutError:
                continue

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
                item for item in intakes if item.status in {"pending", "claimed"}
            )[: status.policy.max_items_per_tick]
            if not selected:
                return AutomationRunResult((), (), ())
            prepared: list[AutomationIntake] = []
            for intake in selected:
                try:
                    _freeze_intake_task_binding(self.output_root, intake)
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
                if intake.status == "pending":
                    save_intake(
                        self.output_root,
                        intake.model_copy(update={"status": "claimed"}),
                    )
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

    def _interval_seconds(self) -> int:
        status = load_status(self.output_root)
        if status is None:
            return 300
        return status.policy.check_interval_seconds

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


def _freeze_intake_task_binding(output_root: Path, intake: AutomationIntake) -> None:
    """Freeze the authorized roles before changing pending intake ownership."""
    if automation_readiness(output_root, intake) != "ready":
        raise ValueError("automation setup is not ready")
    current = freeze_role_bindings(output_root)
    required = required_automation_roles(intake.default_output)
    if any(role not in current for role in required):
        raise ValueError("automation roles are incomplete")
    with task_lock(output_root, intake.task_id, timeout=0):
        found = find_task_by_id(output_root, intake.task_id)
        if found is None:
            raise ValueError("automation task is missing")
        task_dir, task = found
        if _task_bindings_match(task, current, required):
            return
        if task_has_execution_facts(
            output_root, intake.task_id
        ) or _has_paid_task_trace(task):
            raise _ImmutableTaskIdentity("automation task binding is immutable")
        updated = TaskRecord.model_validate(
            {
                **task.model_dump(mode="python"),
                "provider_bindings": {
                    role: binding.model_dump(mode="python")
                    for role, binding in current.items()
                },
                "provider_settings_sha256": settings_sha256(load_settings(output_root)),
            }
        )
        write_task_atomic(task_dir, updated)


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


def _has_paid_task_trace(task: TaskRecord) -> bool:
    return any(
        task.stages.get(stage) not in {None, StageStatus.PENDING}
        for stage in ("note", "publish", "podcast_script", "tts")
    )


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
