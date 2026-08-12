"""Atomic local persistence for automatic-delivery configuration and facts."""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from learnnest.automation_models import (
    AutomationAttempt,
    AutomationIntake,
    AutomationPolicy,
    AutomationStatus,
    AutomationTaskState,
)
from learnnest.locks import automation_lock


class ProviderAdmissionError(ValueError):
    """A paid call was rejected before a Provider object could be constructed."""


def automation_directory(output_root: str | Path) -> Path:
    return Path(output_root).resolve() / ".learnnest" / "automation"


def policy_sha256(policy: AutomationPolicy) -> str:
    payload = policy.model_dump(
        mode="json", exclude={"enabled", "authorized_at", "paid_retry_limit"}
    )
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def load_status(output_root: str | Path) -> AutomationStatus | None:
    path = automation_directory(output_root) / "status.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        status = AutomationStatus.model_validate(payload)
        current_sha = policy_sha256(status.policy)
        stored_sha = payload.get("policy_sha256") if isinstance(payload, dict) else None
        if stored_sha != current_sha:
            if isinstance(stored_sha, str) and len(stored_sha) == 64:
                _migrate_task_states(
                    Path(output_root).resolve(), stored_sha, current_sha
                )
            status = status.model_copy(
                update={"schema_version": "1.2", "policy_sha256": current_sha}
            )
            _write_json(path, status.model_dump(mode="json"))
        return status
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        raise ValueError("automation status is missing or invalid") from error


def save_policy(output_root: str | Path, policy: AutomationPolicy) -> AutomationStatus:
    previous = load_status(output_root)
    current_sha = policy_sha256(policy)
    same_policy = previous is not None and previous.policy_sha256 == current_sha
    status = AutomationStatus(
        schema_version="1.2",
        policy=policy,
        policy_sha256=current_sha,
        last_tick_at=previous.last_tick_at if same_policy else None,
        last_tick_summary=previous.last_tick_summary if same_policy else None,
    )
    _write_json(
        automation_directory(output_root) / "status.json",
        status.model_dump(mode="json"),
    )
    return status


def authorize(
    output_root: str | Path, *, now: datetime | None = None
) -> AutomationStatus:
    status = _require_status(output_root)
    from learnnest.provider_profiles import load_settings, settings_sha256

    settings = load_settings(output_root)
    current_settings_sha = settings_sha256(settings)
    from learnnest.automation_models import AutomationBudget

    policy = status.policy.model_copy(
        update={
            "enabled": True,
            "authorized_at": (now or datetime.now(UTC)).astimezone(UTC),
            "provider_settings_sha256": current_settings_sha,
            "retries_per_stage": settings.retries_per_role,
            "budget": AutomationBudget(
                provider_calls_per_day=settings.global_calls_per_day,
                budget_group_calls_per_day=settings.budget_group_calls_per_day,
            ),
        }
    )
    return save_policy(output_root, policy)


def disable(output_root: str | Path) -> AutomationStatus:
    status = _require_status(output_root)
    return save_policy(output_root, status.policy.model_copy(update={"enabled": False}))


def save_tick_result(
    output_root: str | Path,
    *,
    when: datetime,
    summary: str,
) -> AutomationStatus:
    status = _require_status(output_root)
    updated = status.model_copy(
        update={"last_tick_at": when.astimezone(UTC), "last_tick_summary": summary}
    )
    _write_json(
        automation_directory(output_root) / "status.json",
        updated.model_dump(mode="json"),
    )
    return updated


def load_task_state(
    output_root: str | Path, task_id: str, policy_sha: str
) -> AutomationTaskState | None:
    path = _task_path(output_root, task_id, policy_sha)
    if not path.is_file():
        return None
    try:
        state = AutomationTaskState.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        raise ValueError("automation task state is missing or invalid") from error
    if state.task_id != task_id or state.policy_sha256 != policy_sha:
        raise ValueError("automation task state does not match its identity")
    return state


def save_task_state(output_root: str | Path, state: AutomationTaskState) -> None:
    _write_json(
        _task_path(output_root, state.task_id, state.policy_sha256),
        state.model_dump(mode="json"),
    )


def task_has_execution_facts(output_root: str | Path, task_id: str) -> bool:
    """Return whether any durable automation state already fixes task identity."""
    directory = automation_directory(output_root) / "tasks" / task_id
    if not directory.is_dir():
        return False
    for path in sorted(directory.glob("*.json")):
        try:
            state = AutomationTaskState.model_validate_json(path.read_bytes())
        except (OSError, UnicodeError, ValidationError, ValueError) as error:
            raise ValueError("automation task state is missing or invalid") from error
        if state.task_id != task_id or path.stem != state.policy_sha256:
            raise ValueError("automation task state does not match its identity")
        return True
    return False


def create_intake(
    output_root: str | Path, intake: AutomationIntake
) -> AutomationIntake:
    """Atomically preserve the first user-selected output for one task identity."""
    path = _intake_path(output_root, intake.task_id)
    if path.is_file():
        existing = load_intake(output_root, intake.task_id)
        if (
            existing.source_kind != intake.source_kind
            or existing.default_output != intake.default_output
        ):
            raise ValueError("automation intake conflicts with its frozen identity")
        return existing
    _write_json(path, intake.model_dump(mode="json"))
    return intake


def load_intake(output_root: str | Path, task_id: str) -> AutomationIntake:
    path = _intake_path(output_root, task_id)
    try:
        intake = AutomationIntake.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        raise ValueError("automation intake is missing or invalid") from error
    if intake.task_id != task_id:
        raise ValueError("automation intake does not match its identity")
    return intake


def find_intake(output_root: str | Path, task_id: str) -> AutomationIntake | None:
    path = _intake_path(output_root, task_id)
    if not path.is_file():
        return None
    return load_intake(output_root, task_id)


def list_intakes(output_root: str | Path) -> tuple[AutomationIntake, ...]:
    directory = automation_directory(output_root) / "intake"
    if not directory.is_dir():
        return ()
    items: list[AutomationIntake] = []
    for path in sorted(directory.glob("*.json")):
        try:
            intake = AutomationIntake.model_validate_json(path.read_bytes())
        except (OSError, UnicodeError, ValidationError, ValueError) as error:
            raise ValueError("automation intake is missing or invalid") from error
        if path.stem != intake.task_id:
            raise ValueError("automation intake does not match its identity")
        items.append(intake)
    return tuple(items)


def save_intake(output_root: str | Path, intake: AutomationIntake) -> None:
    existing = load_intake(output_root, intake.task_id)
    if (
        existing.source_kind != intake.source_kind
        or existing.default_output != intake.default_output
        or existing.created_at != intake.created_at
    ):
        raise ValueError("automation intake cannot change its frozen identity")
    _write_json(
        _intake_path(output_root, intake.task_id), intake.model_dump(mode="json")
    )


def admit_provider_call(
    output_root: str | Path,
    state: AutomationTaskState,
    *,
    stage: str,
    now: datetime,
    retries_per_stage: int,
    global_calls_per_day: int,
    budget_group_calls_per_day: dict[str, int],
    lock_held: bool = False,
) -> tuple[AutomationTaskState, AutomationAttempt]:
    """Persist one unique running paid-call fact before construction or invocation.

    Every caller uses the same UTC ledger.  ``running`` is deliberately charged
    so a process loss cannot reopen a paid opportunity.
    """
    if stage not in {"writer", "reviewer", "podcast", "tts"}:
        raise ProviderAdmissionError("unknown paid provider stage")
    _require_aware(now)
    group = "note" if stage in {"writer", "reviewer"} else stage
    try:
        group_cap = int(budget_group_calls_per_day[group])
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderAdmissionError("provider budget policy is invalid") from error
    context = nullcontext() if lock_held else automation_lock(output_root, timeout=1)
    with context:
        current = load_task_state(output_root, state.task_id, state.policy_sha256)
        current = current or state
        history = [item for item in current.attempts if item.stage == stage]
        if any(item.status in {"running", "unknown"} for item in history):
            raise ProviderAdmissionError("prior provider result is not recoverable")
        if len(history) >= retries_per_stage + 1:
            raise ProviderAdmissionError("provider retry limit is exhausted")
        used, _limit, _remaining = provider_call_usage(
            output_root,
            current.policy_sha256,
            now=now,
            limit=global_calls_per_day,
        )
        group_used = provider_budget_group_call_usage(output_root, group, now=now)
        if used >= global_calls_per_day or group_used >= group_cap:
            raise ProviderAdmissionError("provider call limit is exhausted")
        attempt = AutomationAttempt(
            call_id=uuid.uuid4().hex,
            stage=stage,
            attempt=len(history) + 1,
            status="running",
            started_at=now,
        )
        updated = current.model_copy(
            update={"blocked_reason": None, "attempts": [*current.attempts, attempt]}
        )
        save_task_state(output_root, updated)
        return updated, attempt


def finish_provider_call(
    output_root: str | Path,
    state: AutomationTaskState,
    attempt: AutomationAttempt,
    *,
    outcome: str,
    now: datetime,
    safe_summary: str | None = None,
) -> AutomationTaskState:
    """Complete a previously admitted fact without exposing provider payloads."""
    if outcome not in {"completed", "failed", "unknown"}:
        raise ValueError("provider call outcome is invalid")
    _require_aware(now)
    attempts = [
        item.model_copy(
            update={
                "status": outcome,
                "completed_at": now,
                "safe_summary": safe_summary,
            }
        )
        if item.call_id == attempt.call_id and item.status == "running"
        else item
        for item in state.attempts
    ]
    updated = state.model_copy(update={"attempts": attempts})
    save_task_state(output_root, updated)
    return updated


def list_incomplete_task_ids(
    output_root: str | Path, policy_sha: str
) -> tuple[str, ...]:
    directory = automation_directory(output_root) / "tasks"
    if not directory.is_dir():
        return ()
    task_ids: list[str] = []
    for path in directory.glob(f"*/{policy_sha}.json"):
        try:
            state = AutomationTaskState.model_validate_json(path.read_bytes())
        except (OSError, UnicodeError, ValidationError, ValueError):
            continue
        if state.status not in {"completed", "needs_attention"}:
            task_ids.append(state.task_id)
    return tuple(sorted(set(task_ids)))


def list_task_states(
    output_root: str | Path, policy_sha: str
) -> tuple[AutomationTaskState, ...]:
    """Load current-policy task facts for read-only status projection."""
    directory = automation_directory(output_root) / "tasks"
    if not directory.is_dir():
        return ()
    states: list[AutomationTaskState] = []
    for path in directory.glob(f"*/{policy_sha}.json"):
        try:
            state = AutomationTaskState.model_validate_json(path.read_bytes())
        except (OSError, UnicodeError, ValidationError, ValueError):
            continue
        states.append(state)
    return tuple(sorted(states, key=lambda item: item.task_id))


def provider_call_usage(
    output_root: str | Path,
    _policy_sha: str,
    *,
    now: datetime,
    limit: int,
) -> tuple[int, int, int]:
    """Return global UTC-day used, limit, and remaining calls from task facts."""
    _require_aware(now)
    calls: set[str | tuple[str, str, int, str, int]] = set()
    directory = automation_directory(output_root) / "tasks"
    if directory.is_dir():
        for path in directory.glob("*/*.json"):
            try:
                state = AutomationTaskState.model_validate_json(path.read_bytes())
            except (OSError, UnicodeError, ValidationError, ValueError):
                continue
            calls.update(
                attempt.call_id
                or (
                    state.task_id,
                    attempt.stage,
                    attempt.attempt,
                    attempt.started_at.astimezone(UTC).isoformat(),
                    position,
                )
                for position, attempt in enumerate(state.attempts)
                if attempt.stage in {"writer", "reviewer", "podcast", "tts"}
                and attempt.billing == "paid"
                and attempt.started_at.astimezone(UTC).date()
                == now.astimezone(UTC).date()
            )
    used = len(calls)
    return used, limit, max(0, limit - used)


def provider_role_call_usage(
    output_root: str | Path,
    role: str,
    *,
    now: datetime,
) -> int:
    """Count all started paid calls for one product role on the UTC day."""
    _require_aware(now)
    stage = {"note_writer": "writer", "note_reviewer": "reviewer"}.get(role, role)
    if stage not in {"writer", "reviewer", "podcast", "tts", "asr", "ocr"}:
        raise ValueError("unknown provider role")
    calls: set[str | tuple[str, str, int, str, int]] = set()
    directory = automation_directory(output_root) / "tasks"
    if directory.is_dir():
        for path in directory.glob("*/*.json"):
            try:
                state = AutomationTaskState.model_validate_json(path.read_bytes())
            except (OSError, UnicodeError, ValidationError, ValueError):
                continue
            calls.update(
                attempt.call_id
                or (
                    state.task_id,
                    attempt.stage,
                    attempt.attempt,
                    attempt.started_at.astimezone(UTC).isoformat(),
                    position,
                )
                for position, attempt in enumerate(state.attempts)
                if attempt.stage == stage
                and attempt.billing == "paid"
                and attempt.started_at.astimezone(UTC).date()
                == now.astimezone(UTC).date()
            )
    return len(calls)


def provider_budget_group_call_usage(
    output_root: str | Path,
    group: str,
    *,
    now: datetime,
) -> int:
    """Count started paid calls for one frozen product budget group."""
    _require_aware(now)
    stages_by_group = {
        "note": {"writer", "reviewer"},
        "podcast": {"podcast"},
        "tts": {"tts"},
        "asr": set(),
        "ocr": set(),
    }
    try:
        stages = stages_by_group[group]
    except KeyError as error:
        raise ValueError("unknown provider budget group") from error
    calls: set[str | tuple[str, str, int, str, int]] = set()
    directory = automation_directory(output_root) / "tasks"
    if directory.is_dir():
        for path in directory.glob("*/*.json"):
            try:
                state = AutomationTaskState.model_validate_json(path.read_bytes())
            except (OSError, UnicodeError, ValidationError, ValueError):
                continue
            calls.update(
                attempt.call_id
                or (
                    state.task_id,
                    attempt.stage,
                    attempt.attempt,
                    attempt.started_at.astimezone(UTC).isoformat(),
                    position,
                )
                for position, attempt in enumerate(state.attempts)
                if attempt.stage in stages
                and attempt.billing == "paid"
                and attempt.started_at.astimezone(UTC).date()
                == now.astimezone(UTC).date()
            )
    return len(calls)


def _require_status(output_root: str | Path) -> AutomationStatus:
    status = load_status(output_root)
    if status is None:
        raise ValueError("automation is not configured")
    return status


def _task_path(output_root: str | Path, task_id: str, policy_sha: str) -> Path:
    return automation_directory(output_root) / "tasks" / task_id / f"{policy_sha}.json"


def _intake_path(output_root: str | Path, task_id: str) -> Path:
    if not task_id or Path(task_id).name != task_id:
        raise ValueError("automation intake identity is invalid")
    return automation_directory(output_root) / "intake" / f"{task_id}.json"


def _migrate_task_states(root: Path, old_sha: str, new_sha: str) -> None:
    directory = automation_directory(root) / "tasks"
    if not directory.is_dir() or old_sha == new_sha:
        return
    for path in directory.glob(f"*/{old_sha}.json"):
        try:
            state = AutomationTaskState.model_validate_json(path.read_bytes())
        except (OSError, UnicodeError, ValidationError, ValueError):
            continue
        target = _task_path(root, state.task_id, new_sha)
        if target.is_file():
            continue
        _write_json(
            target,
            state.model_copy(update={"policy_sha256": new_sha}).model_dump(mode="json"),
        )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=path.parent
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("automation time must be timezone-aware")
