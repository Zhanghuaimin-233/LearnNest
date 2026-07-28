"""Atomic local persistence for automatic-delivery configuration and facts."""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from learnnest.automation_models import (
    AutomationPolicy,
    AutomationStatus,
    AutomationTaskState,
)


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
                update={"schema_version": "1.1", "policy_sha256": current_sha}
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
        schema_version="1.1",
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
    policy = status.policy.model_copy(
        update={
            "enabled": True,
            "authorized_at": (now or datetime.now(UTC)).astimezone(UTC),
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
                and attempt.started_at.astimezone(UTC).date()
                == now.astimezone(UTC).date()
            )
    used = len(calls)
    return used, limit, max(0, limit - used)


def _require_status(output_root: str | Path) -> AutomationStatus:
    status = load_status(output_root)
    if status is None:
        raise ValueError("automation is not configured")
    return status


def _task_path(output_root: str | Path, task_id: str, policy_sha: str) -> Path:
    return automation_directory(output_root) / "tasks" / task_id / f"{policy_sha}.json"


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
