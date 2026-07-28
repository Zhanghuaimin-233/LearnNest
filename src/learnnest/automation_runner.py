"""Recoverable, opt-in orchestration for the automatic paid delivery route."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import uuid

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
    PodcastArtifact,
    ReviewedMarkdownSource,
    generate_model_reviewed_podcast,
    generate_model_reviewed_tts,
    load_reviewed_markdown_source,
)
from learnnest.automation_models import AutomationAttempt, AutomationTaskState
from learnnest.automation_store import (
    load_status,
    load_task_state,
    provider_call_usage,
    save_task_state,
    save_tick_result,
)
from learnnest.execution import RETRY_DELAYS, classify_failure
from learnnest.locks import automation_lock
from learnnest.podcast_providers import PodcastProvider
from learnnest.tts_generation import DEFAULT_TTS_STYLE
from learnnest.tts_providers import TtsProvider


_PAID_STAGES = ("writer", "reviewer", "podcast", "tts")


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
    """Deliver deterministic tasks under one immutable authorized policy.

    The automation lock covers budget admission and the running marker.  Each
    invocation advances a failed paid stage by at most one opportunity, so a
    retry uses its persisted bounded delay instead of looping inside one tick.
    """
    root = Path(output_root).resolve()
    selected_now = (now or datetime.now(UTC)).astimezone(UTC)
    status = load_status(root)
    if status is None:
        raise ValueError("automation is not configured")
    if not status.policy.enabled or status.policy.authorized_at is None:
        raise ValueError("automation paid delivery is not authorized")
    _assert_provider_snapshots(status.policy, providers)
    selected = tuple(dict.fromkeys(task_ids))[: status.policy.max_items_per_tick]
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
                # Only the safe per-task fact is retained; the caller may
                # surface a sanitized command error without raw provider data.
                failed.append(task_id)
        used, limit, _remaining = provider_call_usage(
            root,
            status.policy_sha256,
            now=selected_now,
            limit=status.policy.budget.provider_calls_per_day,
        )
        summary = (
            f"tasks={len(selected)} completed={len(completed)} failed={len(failed)} "
            f"provider_calls={used}/{limit}"
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
    if not status.policy.enabled or status.policy.authorized_at is None:
        return False
    policy = status.policy
    state = load_task_state(root, task_id, status.policy_sha256)
    if state is None:
        plan_path = create_assisted_plan(
            root,
            [task_id],
            writer=policy.writer,
            reviewer=policy.reviewer,
            max_role_calls=policy.retries_per_stage + 1,
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
        return _attention(
            root, state, "non_retryable_failure", "automation task has no plan"
        )
    plan_path = root / state.plan_path
    try:
        plan = load_assisted_plan(plan_path)
        _assert_plan_snapshots(plan, policy)
        # This path is deliberately local-only.  It can promote a persisted
        # response or candidate without consuming a new provider opportunity.
        recover_assisted_plan(plan_path, root)
    except Exception as error:
        return _attention(
            root,
            state,
            "non_retryable_failure",
            _safe_summary(error),
        )

    state = _recover_running_note_attempts(root, state, plan_path, now)
    if state.status == "needs_attention":
        return False

    assisted = load_assisted_state(plan_path).tasks[0]
    if assisted.writer.status != "completed":
        state = _run_assisted_role(
            root,
            state,
            "writer",
            now,
            plan_path,
            lambda retry: generate_assisted_plan(
                plan_path, root, providers.writer, retry_failed=retry
            ),
        )
        assisted = load_assisted_state(plan_path).tasks[0]
        if assisted.writer.status != "completed":
            return False

    if assisted.reviewer.status != "completed":
        state = _run_assisted_role(
            root,
            state,
            "reviewer",
            now,
            plan_path,
            lambda retry: review_assisted_plan(
                plan_path, root, providers.reviewer, retry_failed=retry
            ),
        )
        assisted = load_assisted_state(plan_path).tasks[0]
        if assisted.reviewer.status != "completed" or assisted.reviewed_path is None:
            return False

    if assisted.reviewed_path is None:
        return _attention(
            root, state, "non_retryable_failure", "reviewed Markdown is missing"
        )
    task_plan = plan.tasks[0]
    try:
        source = load_reviewed_markdown_source(
            root,
            task_id=task_id,
            reviewed_path=(root / task_plan.task_dir / assisted.reviewed_path),
            plan_id=plan.plan_id,
            dossier_sha256=task_plan.dossier_sha256,
        )
    except Exception as error:
        return _attention(root, state, "non_retryable_failure", _safe_summary(error))

    delivery_dir = source.task_dir / "automated-delivery" / status.policy_sha256[:16]
    podcast, state, podcast_ok = _run_paid_stage(
        root,
        state,
        "podcast",
        now,
        invoke=lambda: generate_model_reviewed_podcast(
            source, providers.podcast, delivery_dir=delivery_dir
        ),
        recover=lambda: _recover_podcast(
            source, providers.podcast, delivery_dir=delivery_dir
        ),
    )
    if not podcast_ok or podcast is None:
        return False

    _tts_path, state, tts_ok = _run_paid_stage(
        root,
        state,
        "tts",
        now,
        invoke=lambda: generate_model_reviewed_tts(
            source,
            podcast,
            providers.tts,
            output_root=root,
            delivery_dir=delivery_dir,
            style_instruction=DEFAULT_TTS_STYLE,
        ),
        recover=lambda: _recover_tts(
            source,
            podcast,
            providers.tts,
            root,
            delivery_dir,
        ),
    )
    if not tts_ok:
        return False
    save_task_state(
        root,
        state.model_copy(update={"status": "completed", "blocked_reason": None}),
    )
    return True


def _run_assisted_role(
    root: Path,
    state: AutomationTaskState,
    stage: str,
    now: datetime,
    plan_path: Path,
    invoke: Callable[[bool], object],
) -> AutomationTaskState:
    status = load_status(root)
    assert status is not None
    readiness = _stage_readiness(
        state,
        stage,
        now,
        max_attempts=status.policy.retries_per_stage + 1,
    )
    if readiness == "completed":
        return state
    if readiness != "ready":
        return _apply_readiness_block(root, state, stage, readiness)
    if not _budget_available(root, now, status.policy.budget, status.policy_sha256):
        return _apply_readiness_block(root, state, stage, "budget_exhausted")

    attempt = _stage_attempts(state, stage) + 1
    state = _begin_attempt(root, state, stage, attempt, now)
    try:
        invoke(attempt > 1)
    except Exception as error:
        return _finish_provider_failure(root, state, stage, attempt, now, error)

    plan_state = load_assisted_state(plan_path).tasks[0]
    role = getattr(plan_state, stage)
    if role.status == "completed":
        return _finish_attempt(
            root,
            state,
            stage,
            attempt,
            "completed",
            now=now,
            response_path=role.output_path,
        )
    error = RuntimeError(role.safe_summary or f"{stage} provider failed")
    return _finish_provider_failure(root, state, stage, attempt, now, error)


def _run_paid_stage(
    root: Path,
    state: AutomationTaskState,
    stage: str,
    now: datetime,
    *,
    invoke: Callable[[], object],
    recover: Callable[[], object],
) -> tuple[object | None, AutomationTaskState, bool]:
    status = load_status(root)
    assert status is not None
    readiness = _stage_readiness(
        state,
        stage,
        now,
        max_attempts=status.policy.retries_per_stage + 1,
    )
    if readiness == "completed":
        try:
            return recover(), state, True
        except Exception as error:
            state = _attention(
                root, state, "non_retryable_failure", _safe_summary(error)
            )
            return None, state, False
    running = _latest_attempt(state, stage, status="running")
    if running is not None:
        try:
            recovered = recover()
        except Exception:
            recovered = None
        if recovered is None:
            state = _finish_attempt(
                root,
                state,
                stage,
                running.attempt,
                "unknown",
                now=now,
                safe_summary="provider result could not be confirmed",
            )
            return None, _attention(root, state, "unknown_result"), False
        state = _finish_attempt(
            root,
            state,
            stage,
            running.attempt,
            "completed",
            now=now,
        )
        return recovered, state, True
    if readiness != "ready":
        return None, _apply_readiness_block(root, state, stage, readiness), False

    if not _budget_available(root, now, status.policy.budget, status.policy_sha256):
        return (
            None,
            _apply_readiness_block(root, state, stage, "budget_exhausted"),
            False,
        )
    attempt = _stage_attempts(state, stage) + 1
    state = _begin_attempt(root, state, stage, attempt, now)
    try:
        result = invoke()
    except Exception as error:
        state = _finish_provider_failure(root, state, stage, attempt, now, error)
        return None, state, False
    state = _finish_attempt(root, state, stage, attempt, "completed", now=now)
    return result, state, True


def _recover_running_note_attempts(
    root: Path,
    state: AutomationTaskState,
    plan_path: Path,
    now: datetime,
) -> AutomationTaskState:
    plan_state = load_assisted_state(plan_path).tasks[0]
    for stage in ("writer", "reviewer"):
        running = _latest_attempt(state, stage, status="running")
        if running is None:
            continue
        role = getattr(plan_state, stage)
        if role.status == "completed":
            state = _finish_attempt(
                root,
                state,
                stage,
                running.attempt,
                "completed",
                now=now,
                response_path=role.output_path,
            )
        else:
            state = _finish_attempt(
                root,
                state,
                stage,
                running.attempt,
                "unknown",
                now=now,
                safe_summary="provider result could not be confirmed",
            )
            state = _attention(root, state, "unknown_result")
            break
        plan_state = load_assisted_state(plan_path).tasks[0]
    return state


def _recover_podcast(
    source: ReviewedMarkdownSource,
    provider: PodcastProvider,
    *,
    delivery_dir: Path,
) -> PodcastArtifact:
    if not (delivery_dir / "podcast").is_dir():
        raise ValueError("persisted podcast artifact is missing")
    return generate_model_reviewed_podcast(source, provider, delivery_dir=delivery_dir)


def _recover_tts(
    source: ReviewedMarkdownSource,
    podcast: PodcastArtifact,
    provider: TtsProvider,
    root: Path,
    delivery_dir: Path,
) -> Path:
    if not (delivery_dir / "tts").is_dir():
        raise ValueError("persisted TTS artifact is missing")
    return generate_model_reviewed_tts(
        source,
        podcast,
        provider,
        output_root=root,
        delivery_dir=delivery_dir,
        style_instruction=DEFAULT_TTS_STYLE,
    )


def _finish_provider_failure(
    root: Path,
    state: AutomationTaskState,
    stage: str,
    attempt: int,
    now: datetime,
    error: Exception,
) -> AutomationTaskState:
    failure = classify_failure(error)
    if _is_unknown_failure(error):
        updated = _finish_attempt(
            root,
            state,
            stage,
            attempt,
            "unknown",
            now=now,
            safe_summary=failure.safe_summary,
        )
        return _attention(root, updated, "unknown_result")
    updated = _finish_attempt(
        root,
        state,
        stage,
        attempt,
        "failed",
        now=now,
        disposition=failure.disposition,
        safe_summary=failure.safe_summary,
    )
    if failure.disposition != "retryable":
        return _attention(root, updated, "non_retryable_failure")
    status = load_status(root)
    assert status is not None
    if attempt >= status.policy.retries_per_stage + 1:
        return _attention(root, updated, "stage_attempt_limit")
    updated = updated.model_copy(update={"blocked_reason": "retry_wait"})
    save_task_state(root, updated)
    return updated


def _begin_attempt(
    root: Path,
    state: AutomationTaskState,
    stage: str,
    attempt: int,
    now: datetime,
) -> AutomationTaskState:
    updated = state.model_copy(
        update={
            "blocked_reason": None,
            "attempts": [
                *state.attempts,
                AutomationAttempt(
                    call_id=uuid.uuid4().hex,
                    stage=stage,
                    attempt=attempt,
                    status="running",
                    started_at=now,
                ),
            ],
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
    *,
    now: datetime,
    disposition: str | None = None,
    safe_summary: str | None = None,
    response_path: str | None = None,
) -> AutomationTaskState:
    next_retry_at = None
    if outcome == "failed" and disposition == "retryable":
        retry_index = attempt - 1
        if retry_index < len(RETRY_DELAYS):
            next_retry_at = now + RETRY_DELAYS[retry_index]
    attempts = [
        item.model_copy(
            update={
                "status": outcome,
                "completed_at": now,
                "disposition": disposition,
                "safe_summary": safe_summary,
                "response_path": response_path,
                "next_retry_at": next_retry_at,
            }
        )
        if item.stage == stage and item.attempt == attempt and item.status == "running"
        else item
        for item in state.attempts
    ]
    updated = state.model_copy(
        update={
            "attempts": attempts,
            **(
                {"status": "pending", "blocked_reason": None}
                if outcome == "completed"
                else {}
            ),
        }
    )
    save_task_state(root, updated)
    return updated


def _stage_readiness(
    state: AutomationTaskState,
    stage: str,
    now: datetime,
    *,
    max_attempts: int = 4,
) -> str:
    history = [item for item in state.attempts if item.stage == stage]
    if any(item.status == "completed" for item in history):
        return "completed"
    if any(item.status == "unknown" for item in history):
        return "unknown"
    if len(history) >= max_attempts:
        return "attempt_limit"
    if not history:
        return "ready"
    latest = history[-1]
    if latest.status == "running":
        return "running"
    if latest.disposition != "retryable":
        return "non_retryable"
    if latest.next_retry_at is not None and latest.next_retry_at > now:
        return "retry_wait"
    return "ready"


def _apply_readiness_block(
    root: Path,
    state: AutomationTaskState,
    stage: str,
    readiness: str,
) -> AutomationTaskState:
    if readiness == "budget_exhausted":
        updated = state.model_copy(
            update={"blocked_reason": "provider_budget_exhausted"}
        )
    elif readiness == "retry_wait":
        updated = state.model_copy(update={"blocked_reason": "retry_wait"})
    elif readiness == "attempt_limit":
        updated = _attention(root, state, "stage_attempt_limit")
        return updated
    elif readiness == "unknown":
        updated = _attention(root, state, "unknown_result")
        return updated
    elif readiness == "non_retryable":
        updated = _attention(root, state, "non_retryable_failure")
        return updated
    elif readiness == "running":
        updated = _attention(root, state, "unknown_result")
        return updated
    else:
        return state
    save_task_state(root, updated)
    return updated


def _attention(
    root: Path,
    state: AutomationTaskState,
    reason: str,
    summary: str | None = None,
) -> AutomationTaskState:
    updated = state.model_copy(
        update={
            "status": "needs_attention",
            "blocked_reason": reason,
            "attempts": [
                item.model_copy(update={"safe_summary": summary})
                if summary is not None
                and item.status in {"failed", "unknown"}
                and item.safe_summary is None
                else item
                for item in state.attempts
            ],
        }
    )
    save_task_state(root, updated)
    return updated


def _stage_attempts(state: AutomationTaskState, stage: str) -> int:
    return sum(1 for item in state.attempts if item.stage == stage)


def _latest_attempt(
    state: AutomationTaskState, stage: str, *, status: str | None = None
) -> AutomationAttempt | None:
    matches = [
        item
        for item in state.attempts
        if item.stage == stage and (status is None or item.status == status)
    ]
    return matches[-1] if matches else None


def _budget_available(
    root: Path,
    now: datetime,
    budget: object,
    policy_sha: str,
) -> bool:
    allowed = int(getattr(budget, "provider_calls_per_day"))
    used, _limit, _remaining = provider_call_usage(
        root, policy_sha, now=now, limit=allowed
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


def _assert_plan_snapshots(plan: object, policy: object) -> None:
    if plan.writer != policy.writer or plan.reviewer != policy.reviewer:
        raise ValueError("automation plan provider snapshot no longer matches policy")


def _is_unknown_failure(error: Exception) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return "timeout" in text or "timed out" in text


def _safe_summary(error: Exception) -> str:
    return classify_failure(error).safe_summary
