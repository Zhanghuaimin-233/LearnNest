"""Shared, read-only decisions for the public learning-workspace state."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from learnnest.automation_models import AutomationIntake
from learnnest.automation_store import load_status, load_task_state
from learnnest.provider_profiles import (
    freeze_role_bindings,
    load_settings,
    settings_sha256,
)

AutomationReadiness = Literal["ready", "waiting_setup", "waiting_authorization"]


def automation_task_state(output_root: str | Path, task_id: str) -> str | None:
    """Read the current-policy execution fact without creating or repairing it."""
    try:
        status = load_status(output_root)
        if status is None:
            return None
        state = load_task_state(output_root, task_id, status.policy_sha256)
    except ValueError:
        return "invalid"
    return None if state is None else state.status


def required_automation_roles(
    default_output: str,
) -> tuple[str, ...]:
    """Return the minimum immutable roles for one requested delivery."""
    if default_output == "complete_note":
        return ("note_writer", "note_reviewer")
    if default_output == "complete_note_with_audio":
        return ("note_writer", "note_reviewer", "podcast", "tts")
    raise ValueError("automation output is invalid")


def automation_readiness(
    output_root: str | Path, intake: AutomationIntake
) -> AutomationReadiness:
    """Project setup and authorization without exposing configuration details."""
    root = Path(output_root).resolve()
    try:
        settings = load_settings(root)
        bindings = freeze_role_bindings(root)
        status = load_status(root)
    except ValueError:
        return "waiting_setup"
    required = required_automation_roles(intake.default_output)
    if status is None or any(role not in bindings for role in required):
        return "waiting_setup"
    current_sha = settings_sha256(settings)
    if (
        not status.policy.enabled
        or status.policy.authorized_at is None
        or status.policy.provider_settings_sha256 != current_sha
    ):
        return "waiting_authorization"
    if not _policy_matches_current_note_roles(status.policy, bindings, current_sha):
        return "waiting_setup"
    return "ready"


def _policy_matches_current_note_roles(
    policy: object, bindings: dict[str, object], current_sha: str
) -> bool:
    for role, policy_name in (("note_writer", "writer"), ("note_reviewer", "reviewer")):
        binding = bindings.get(role)
        snapshot = getattr(policy, policy_name, None)
        if binding is None or snapshot is None:
            return False
        if (
            getattr(binding, "connection_id", None)
            != getattr(snapshot, "connection_id", None)
            or getattr(binding, "provider", None) != getattr(snapshot, "provider", None)
            or getattr(binding, "endpoint", None)
            != getattr(snapshot, "endpoint_identity", None)
            or getattr(binding, "model", None) != getattr(snapshot, "model", None)
            or getattr(binding, "adapter_revision", None)
            != getattr(snapshot, "adapter_revision", None)
            or getattr(binding, "settings_sha256", None) != current_sha
            or getattr(snapshot, "settings_sha256", None) not in {None, current_sha}
        ):
            return False
    return True
