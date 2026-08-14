"""Shared, read-only decisions for the public learning-workspace state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from learnnest.automation_models import AutomationIntake, AutomationTaskState

from learnnest.automation_store import find_intake, load_status, load_task_state
from learnnest.provider_profiles import (
    freeze_role_bindings,
    load_settings,
    role_binding_is_compatible,
    settings_sha256,
)
from learnnest.provider_secrets import ProviderSecretStore, SecretStoreError
from learnnest.task_store import find_task_by_id

AutomationReadiness = Literal["ready", "waiting_setup", "waiting_authorization"]

_ROLE_LABELS = {
    "note_writer": "笔记 Writer",
    "note_reviewer": "笔记 Reviewer",
    "podcast": "播客",
    "tts": "TTS",
}
_ROLE_CAPABILITIES = {
    "note_writer": "llm",
    "note_reviewer": "llm",
    "podcast": "llm",
    "tts": "tts",
}
_ROLE_SETUP_HINTS = {
    "note_writer": "请先添加可用的 MiMo/DeepSeek 连接。",
    "note_reviewer": "请先添加可用的 MiMo/DeepSeek 连接。",
    "podcast": "播客需要单独的 MiMo/DeepSeek 连接，不能复用笔记连接。",
    "tts": "请先添加可用的 Windows 系统语音或 MiMo TTS 连接。",
}
_OUTPUT_LABELS = {
    "complete_note": "完整笔记",
    "complete_note_with_audio": "完整笔记和播客音频",
}
_PROVIDER_LABELS = {
    "xiaomi-mimo": "MiMo",
    "deepseek": "DeepSeek",
    "xiaomi-mimo-tts": "MiMo TTS",
    "windows-tts": "Windows 系统语音",
}


@dataclass(frozen=True)
class AutomationRetryAdmission:
    """Current immutable facts that permit one explicit automation retry."""

    intake: AutomationIntake
    state: AutomationTaskState


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


def automation_retry_is_due(output_root: str | Path, task_id: str) -> bool:
    """Whether durable, current-policy facts permit one explicit retry."""
    return automation_retry_admission(output_root, task_id) is not None


def automation_retry_admission(
    output_root: str | Path, task_id: str
) -> AutomationRetryAdmission | None:
    """Read every retry identity fact together without writing or repairing it."""
    root = Path(output_root).resolve()
    try:
        status = load_status(root)
        if status is None:
            return None
        if (
            not status.policy.enabled
            or status.policy.authorized_at is None
            or status.policy.provider_settings_sha256
            != settings_sha256(load_settings(root))
        ):
            return None
        found = find_task_by_id(root, task_id)
        if found is None:
            return None
        _task_dir, task = found
        if task.provider_settings_sha256 != status.policy.provider_settings_sha256:
            return None
        intake = find_intake(root, task_id)
        if intake is None or intake.status != "needs_attention":
            return None
        state = load_task_state(root, task_id, status.policy_sha256)
        if (
            state is None
            or state.task_id != intake.task_id
            or state.default_output != intake.default_output
            or state.status != "pending"
            or state.blocked_reason != "retry_wait"
        ):
            return None
        current = freeze_role_bindings(root)
        required = required_automation_roles(intake.default_output)
        if any(
            task.provider_bindings.get(role) is None
            or current.get(role) is None
            or task.provider_bindings[role].model_dump(mode="json")
            != current[role].model_dump(mode="json")
            for role in required
        ):
            return None
        if automation_readiness(root, intake) != "ready":
            return None
        if state.blocked_reason in {
            "provider_budget_exhausted",
            "unknown_result",
            "non_retryable_failure",
            "stage_attempt_limit",
        }:
            return None
        if not state.attempts:
            return None
        latest = state.attempts[-1]
        if not (
            latest.status == "failed"
            and latest.disposition == "retryable"
            and latest.next_retry_at is not None
            and latest.next_retry_at <= datetime.now(UTC)
        ):
            return None
        return AutomationRetryAdmission(intake=intake, state=state)
    except ValueError:
        return None


def required_automation_roles(
    default_output: str,
) -> tuple[str, ...]:
    """Return the minimum immutable roles for one requested delivery."""
    if default_output == "complete_note":
        return ("note_writer", "note_reviewer")
    if default_output == "complete_note_with_audio":
        return ("note_writer", "note_reviewer", "podcast", "tts")
    raise ValueError("automation output is invalid")


def public_setup_readiness(
    output_root: str | Path, default_output: str | None = None
) -> dict[str, object]:
    """Project the selected delivery's setup facts in plain user language.

    This reads existing settings, policy, and stored secret readability only.
    It intentionally neither creates a binding nor constructs a Provider, so
    rendering the settings page cannot become a paid-call entry point.
    """
    root = Path(output_root).resolve()
    try:
        settings = load_settings(root)
        status = load_status(root)
    except ValueError:
        return {
            "default_output": _OUTPUT_LABELS["complete_note_with_audio"],
            "required_roles": [],
            "state": "需要检查设置",
            "message": "设置暂时无法读取，请重新保存需要的连接。",
            "authorization": {
                "state": "等待设置",
                "message": "完成连接设置后才能确认自动整理。",
            },
        }
    selected = default_output or (
        status.policy.default_output
        if status is not None
        else "complete_note_with_audio"
    )
    required = required_automation_roles(selected)
    roles: list[dict[str, object]] = []
    for role in required:
        binding = settings.role_bindings.get(role)
        connection = (
            settings.connections.get(binding.connection_id)
            if binding is not None
            else None
        )
        connection_state = public_connection_readability(root, connection)
        options = [
            {
                "name": item.name,
                "provider": public_provider_label(item.provider),
            }
            for item in sorted(
                settings.connections.values(), key=lambda item: item.name
            )
            if item.capability == _ROLE_CAPABILITIES[role]
            and role_binding_is_compatible(
                settings, role=role, connection_name=item.name
            )
        ]
        roles.append(
            {
                "name": _ROLE_LABELS[role],
                "connection": connection.name if connection is not None else None,
                "state": (
                    connection_state if connection is not None else "尚未绑定连接"
                ),
                "options": options,
                "hint": (
                    _ROLE_SETUP_HINTS[role]
                    if connection is None and not options
                    else None
                ),
            }
        )
    configured = all(
        role["state"] in {"连接配置可读取", "本地配置可读取"} for role in roles
    )
    current_sha = settings_sha256(settings)
    if not configured:
        authorization = {
            "state": "等待设置",
            "message": "先为所选结果补齐需要的连接。",
        }
        state = "等待设置"
        message = "补齐每一项需要的连接后，再保存自动整理设置。"
    elif status is None or not _policy_matches_current_note_roles(
        status.policy, freeze_role_bindings(root), current_sha
    ):
        authorization = {
            "state": "等待设置",
            "message": "连接已就绪，请保存自动整理设置。",
        }
        state = "等待设置"
        message = "连接已就绪，请保存自动整理设置。"
    elif (
        not status.policy.enabled
        or status.policy.authorized_at is None
        or status.policy.provider_settings_sha256 != current_sha
    ):
        authorization = {
            "state": "等待授权",
            "message": "设置已保存，确认可能付费的自动整理后才会开始。",
        }
        state = "等待授权"
        message = "所选结果已准备好，等待你的付费确认。"
    else:
        authorization = {"state": "已授权", "message": "自动整理可以开始。"}
        state = "可以自动整理"
        message = "所选结果已就绪。"
    return {
        "default_output": _OUTPUT_LABELS[selected],
        "required_roles": roles,
        "state": state,
        "message": message,
        "authorization": authorization,
    }


def public_provider_label(provider: str) -> str:
    """Return a stable human label for one persisted Provider identifier."""
    return _PROVIDER_LABELS.get(provider, "已保存的连接")


def public_connection_readability(root: Path, connection: object | None) -> str:
    """Project an existing connection without constructing its Provider client."""
    if connection is None:
        return "连接密钥不可用"
    secret_id = getattr(connection, "secret_id", None)
    if secret_id is None:
        return "本地配置可读取"
    try:
        ProviderSecretStore(root).read(secret_id)
    except SecretStoreError:
        return "连接密钥不可用"
    return "连接配置可读取"


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
