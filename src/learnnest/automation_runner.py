"""Recoverable, opt-in orchestration for the automatic paid delivery route."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from learnnest.assisted_note_generation import (
    create_assisted_plan,
    generate_assisted_plan,
    load_assisted_plan,
    load_assisted_state,
    recover_assisted_plan,
    review_assisted_plan,
)
from learnnest.assisted_note_generation import AssistedNoteProvider
from learnnest.automation_delivery import (
    generate_model_reviewed_podcast,
    generate_model_reviewed_tts,
    load_reviewed_markdown_source,
)
from learnnest.automation_models import AutomationAttempt, AutomationTaskState
from learnnest.automation_store import (
    load_status,
    load_task_state,
    save_task_state,
    save_tick_result,
)
from learnnest.locks import automation_lock
from learnnest.podcast_providers import PodcastProvider
from learnnest.tts_generation import DEFAULT_TTS_STYLE
from learnnest.tts_providers import TtsProvider


@dataclass(frozen=True)
class AutomationProviders:
    writer: AssistedNoteProvider
    reviewer: AssistedNoteProvider
    podcast: PodcastProvider
    tts: TtsProvider


@dataclass(frozen=True)
class AutomationRunResult:
    task_ids: tuple[str, ...]
    completed_task_ids: tuple[str, ...]
    failed_task_ids: tuple[str, ...]


def run_automation_tasks(
    output_root: str | Path,
    task_ids: Iterable[str],
    providers: AutomationProviders,
    *,
    now: datetime | None = None,
) -> AutomationRunResult:
    """Deliver already deterministic tasks under one immutable authorized policy."""
    root = Path(output_root).resolve()
    selected_now = (now or datetime.now(UTC)).astimezone(UTC)
    status = load_status(root)
    if status is None:
        raise ValueError("automation is not configured")
    if not status.policy.enabled or status.policy.authorized_at is None:
        raise ValueError("automation paid delivery is not authorized")
    _assert_provider_snapshots(status.policy, providers)
    selected = tuple(task_ids)
    if len(selected) > status.policy.max_items_per_tick:
        selected = selected[: status.policy.max_items_per_tick]
    completed: list[str] = []
    failed: list[str] = []
    with automation_lock(root, timeout=0):
        for task_id in selected:
            try:
                if _run_task(root, task_id, providers, selected_now):
                    completed.append(task_id)
                else:
                    failed.append(task_id)
            except Exception:
                failed.append(task_id)
        summary = (
            f"tasks={len(selected)} completed={len(completed)} failed={len(failed)}"
        )
        save_tick_result(root, when=selected_now, summary=summary)
    return AutomationRunResult(selected, tuple(completed), tuple(failed))


def _run_task(
    root: Path,
    task_id: str,
    providers: AutomationProviders,
    now: datetime,
) -> bool:
    status = load_status(root)
    assert status is not None
    policy = status.policy
    state = load_task_state(root, task_id, status.policy_sha256)
    if state is None:
        plan_path = create_assisted_plan(
            root,
            [task_id],
            writer=policy.writer,
            reviewer=policy.reviewer,
            max_role_calls=policy.paid_retry_limit + 1,
            now=now,
        )
        state = AutomationTaskState(
            task_id=task_id,
            policy_sha256=status.policy_sha256,
            plan_path=plan_path.resolve().relative_to(root).as_posix(),
        )
        save_task_state(root, state)
    if state.status == "completed":
        return True
    if state.plan_path is None:
        raise ValueError("automation task has no assisted plan")
    plan_path = root / state.plan_path
    plan = load_assisted_plan(plan_path)
    recover_assisted_plan(plan_path, root)
    assisted = load_assisted_state(plan_path).tasks[0]
    if assisted.writer.status != "completed":
        state = _call_assisted_role(
            root,
            state,
            "writer",
            now,
            lambda retry: generate_assisted_plan(
                plan_path, root, providers.writer, retry_failed=retry
            ),
        )
        assisted = load_assisted_state(plan_path).tasks[0]
        if assisted.writer.status != "completed":
            _save_failure(root, state, "writer_failed")
            return False
    if assisted.reviewer.status != "completed":
        state = _call_assisted_role(
            root,
            state,
            "reviewer",
            now,
            lambda retry: review_assisted_plan(
                plan_path, root, providers.reviewer, retry_failed=retry
            ),
        )
        assisted = load_assisted_state(plan_path).tasks[0]
        if assisted.reviewer.status != "completed" or assisted.reviewed_path is None:
            _save_failure(root, state, "review_failed")
            return False
    assert assisted.reviewed_path is not None
    task_plan = plan.tasks[0]
    source = load_reviewed_markdown_source(
        root,
        task_id=task_id,
        reviewed_path=(root / task_plan.task_dir / assisted.reviewed_path),
        plan_id=plan.plan_id,
        dossier_sha256=task_plan.dossier_sha256,
    )
    delivery_dir = source.task_dir / "automated-delivery" / status.policy_sha256[:16]
    podcast_ok, state = _run_paid_stage(
        root,
        state,
        "podcast",
        now,
        lambda: generate_model_reviewed_podcast(
            source, providers.podcast, delivery_dir=delivery_dir
        ),
    )
    if not podcast_ok:
        _save_failure(root, state, "podcast_failed")
        return False
    podcast = generate_model_reviewed_podcast(
        source, providers.podcast, delivery_dir=delivery_dir
    )
    tts_ok, state = _run_paid_stage(
        root,
        state,
        "tts",
        now,
        lambda: generate_model_reviewed_tts(
            source,
            podcast,
            providers.tts,
            output_root=root,
            delivery_dir=delivery_dir,
            style_instruction=DEFAULT_TTS_STYLE,
        ),
    )
    if not tts_ok:
        _save_failure(root, state, "tts_failed")
        return False
    save_task_state(root, state.model_copy(update={"status": "completed"}))
    return True


def _call_assisted_role(
    root: Path,
    state: AutomationTaskState,
    stage: str,
    now: datetime,
    invoke: Callable[[bool], object],
) -> AutomationTaskState:
    status = load_status(root)
    assert status is not None
    limit = status.policy.paid_retry_limit + 1
    for attempt in range(_stage_attempts(state, stage) + 1, limit + 1):
        if not _budget_available(root, stage, now, status.policy.budget):
            return
        state = _begin_attempt(root, state, stage, attempt, now)
        try:
            invoke(attempt > 1)
        except Exception as error:
            state = _finish_attempt(root, state, stage, attempt, "failed", error)
            continue
        plan_state = load_assisted_state(root / str(state.plan_path)).tasks[0]
        role = getattr(plan_state, stage)
        state = _finish_attempt(
            root,
            state,
            stage,
            attempt,
            "completed" if role.status == "completed" else "failed",
            None
            if role.status == "completed"
            else RuntimeError(role.safe_summary or "provider failed"),
            response_path=role.output_path,
        )
        if role.status == "completed":
            return state
    return state


def _run_paid_stage(
    root: Path,
    state: AutomationTaskState,
    stage: str,
    now: datetime,
    invoke: Callable[[], object],
) -> tuple[bool, AutomationTaskState]:
    if any(
        item.stage == stage and item.status == "completed" for item in state.attempts
    ):
        return True, state
    status = load_status(root)
    assert status is not None
    limit = status.policy.paid_retry_limit + 1
    for attempt in range(_stage_attempts(state, stage) + 1, limit + 1):
        if not _budget_available(root, stage, now, status.policy.budget):
            return False, state
        state = _begin_attempt(root, state, stage, attempt, now)
        try:
            invoke()
        except Exception as error:
            state = _finish_attempt(root, state, stage, attempt, "failed", error)
            continue
        state = _finish_attempt(root, state, stage, attempt, "completed", None)
        return True, state
    return False, state


def _begin_attempt(
    root: Path, state: AutomationTaskState, stage: str, attempt: int, now: datetime
) -> AutomationTaskState:
    updated = state.model_copy(
        update={
            "attempts": [
                *state.attempts,
                AutomationAttempt(
                    stage=stage, attempt=attempt, status="running", started_at=now
                ),
            ]
        }
    )
    save_task_state(root, updated)
    return updated


def _finish_attempt(
    root: Path,
    state: AutomationTaskState,
    stage: str,
    attempt: int,
    outcome: str,
    error: Exception | None,
    *,
    response_path: str | None = None,
) -> AutomationTaskState:
    attempts = [
        item.model_copy(
            update={
                "status": outcome,
                "completed_at": datetime.now(UTC),
                "safe_summary": None if error is None else type(error).__name__,
                "response_path": response_path,
            }
        )
        if item.stage == stage and item.attempt == attempt and item.status == "running"
        else item
        for item in state.attempts
    ]
    updated = state.model_copy(update={"attempts": attempts})
    save_task_state(root, updated)
    return updated


def _save_failure(root: Path, state: AutomationTaskState, failure: str) -> None:
    save_task_state(root, state.model_copy(update={"status": failure}))


def _stage_attempts(state: AutomationTaskState, stage: str) -> int:
    return sum(1 for item in state.attempts if item.stage == stage)


def _budget_available(root: Path, stage: str, now: datetime, budget: object) -> bool:
    field = f"{stage}_per_day"
    allowed = int(getattr(budget, field))
    used = 0
    for path in (root / ".learnnest" / "automation" / "tasks").glob("*/*.json"):
        try:
            state = AutomationTaskState.model_validate_json(path.read_bytes())
        except Exception:
            continue
        used += sum(
            1
            for item in state.attempts
            if item.stage == stage
            and item.started_at.astimezone(UTC).date() == now.date()
        )
    return used < allowed


def _assert_provider_snapshots(policy: object, providers: AutomationProviders) -> None:
    for role, provider, snapshot in (
        ("writer", providers.writer, policy.writer),
        ("reviewer", providers.reviewer, policy.reviewer),
    ):
        if (
            provider.name != snapshot.provider
            or provider.model != snapshot.model
            or provider.endpoint_identity != snapshot.endpoint_identity
        ):
            raise ValueError(f"automation {role} provider no longer matches policy")
