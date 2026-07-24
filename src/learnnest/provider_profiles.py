"""Local, secret-free provider connections and Writer capability profiles.

This module deliberately persists only connection identity and probe outcomes.
Credentials and provider payloads stay at the runtime boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from learnnest.note_models import ReaderDraft

WriterStrategy = Literal[
    "native_json_schema", "tool_call", "json_object", "prompted_json"
]
Extractor = Literal["message_content", "tool_call_arguments"]
AuthorizationMode = Literal["local_only", "automatic"]

_STRATEGIES: tuple[WriterStrategy, ...] = (
    "native_json_schema",
    "tool_call",
    "json_object",
    "prompted_json",
)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProviderPreset(_Model):
    preset: str
    provider: str
    api_family: Literal["openai_chat", "anthropic_messages", "gemini_generate_content"]
    endpoint: str
    default_model: str
    secret_env: str
    adapter_revision: str = "1"


PRESETS: dict[str, ProviderPreset] = {
    "mimo": ProviderPreset(
        preset="mimo",
        provider="xiaomi-mimo",
        api_family="openai_chat",
        endpoint="https://api.xiaomimimo.com/v1",
        default_model="mimo-v2.5",
        secret_env="MIMO_API_KEY",
    ),
    "openai": ProviderPreset(
        preset="openai",
        provider="openai",
        api_family="openai_chat",
        endpoint="https://api.openai.com/v1",
        default_model="gpt-5.2",
        secret_env="OPENAI_API_KEY",
    ),
    "anthropic": ProviderPreset(
        preset="anthropic",
        provider="anthropic",
        api_family="anthropic_messages",
        endpoint="https://api.anthropic.com/v1",
        default_model="claude-sonnet-4-5",
        secret_env="ANTHROPIC_API_KEY",
    ),
    "gemini": ProviderPreset(
        preset="gemini",
        provider="google-gemini",
        api_family="gemini_generate_content",
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        default_model="gemini-3.1-pro-preview",
        secret_env="GEMINI_API_KEY",
    ),
    "deepseek": ProviderPreset(
        preset="deepseek",
        provider="deepseek",
        api_family="openai_chat",
        endpoint="https://api.deepseek.com/v1",
        default_model="deepseek-v4-pro",
        secret_env="DEEPSEEK_API_KEY",
    ),
}


class ProviderConnection(_Model):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    preset: str
    provider: str
    api_family: Literal["openai_chat", "anthropic_messages", "gemini_generate_content"]
    endpoint: str
    model: str
    secret_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    adapter_revision: str = Field(min_length=1, max_length=32)
    updated_at: datetime


class ProviderSettings(_Model):
    schema_version: Literal["1.0"] = "1.0"
    authorization: AuthorizationMode = "local_only"
    default_connection: str | None = None
    connections: dict[str, ProviderConnection] = Field(default_factory=dict)


class CapabilityAttempt(_Model):
    strategy: WriterStrategy
    extractor: Extractor
    outcome: Literal["success", "failed"]
    failure_class: Literal["transport", "unsupported", "invalid_contract"] | None = None


class WriterCapabilityProfile(_Model):
    schema_version: Literal["1.0"] = "1.0"
    profile_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider: str
    endpoint_identity: str
    model: str
    adapter_revision: str
    status: Literal["verified", "failed", "invalidated"]
    checked_at: datetime
    attempts: list[CapabilityAttempt] = Field(min_length=1, max_length=4)
    strategy: WriterStrategy | None = None
    extractor: Extractor | None = None


def provider_directory(output_root: str | Path) -> Path:
    return Path(output_root).resolve() / ".learnnest" / "providers"


def load_settings(output_root: str | Path) -> ProviderSettings:
    path = provider_directory(output_root) / "settings.json"
    if not path.is_file():
        return ProviderSettings()
    try:
        return ProviderSettings.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        raise ValueError("provider settings are missing or invalid") from error


def save_settings(output_root: str | Path, settings: ProviderSettings) -> None:
    _write_json(
        provider_directory(output_root) / "settings.json",
        settings.model_dump(mode="json"),
    )


def connect(
    output_root: str | Path,
    *,
    name: str,
    preset: str,
    endpoint: str | None = None,
    model: str | None = None,
    secret_env: str | None = None,
    provider_name: str | None = None,
    now: datetime | None = None,
) -> ProviderConnection:
    if not _NAME.fullmatch(name):
        raise ValueError("provider connection name is invalid")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if preset == "openai-compatible":
        if not endpoint or not model or not secret_env:
            raise ValueError(
                "openai-compatible connection requires endpoint, model, and secret env"
            )
        base = ProviderPreset(
            preset=preset,
            provider=provider_name or "openai-compatible",
            api_family="openai_chat",
            endpoint=endpoint,
            default_model=model,
            secret_env=secret_env,
        )
    else:
        try:
            base = PRESETS[preset]
        except KeyError as error:
            raise ValueError("unknown provider preset") from error
    connection = ProviderConnection(
        name=name,
        preset=base.preset,
        provider=base.provider,
        api_family=base.api_family,
        endpoint=(endpoint or base.endpoint),
        model=(model or base.default_model),
        secret_env=(secret_env or base.secret_env),
        adapter_revision=base.adapter_revision,
        updated_at=now,
    )
    settings = load_settings(output_root)
    previous = settings.connections.get(name)
    settings.connections[name] = connection
    if settings.default_connection is None:
        settings.default_connection = name
    save_settings(output_root, settings)
    if previous is not None and _identity(previous) != _identity(connection):
        invalidate_profiles(output_root, previous)
    return connection


def set_authorization(
    output_root: str | Path, authorization: AuthorizationMode
) -> ProviderSettings:
    settings = load_settings(output_root)
    settings.authorization = authorization
    save_settings(output_root, settings)
    return settings


def get_connection(
    output_root: str | Path, name: str | None = None
) -> ProviderConnection:
    settings = load_settings(output_root)
    selected = name or settings.default_connection
    if not selected or selected not in settings.connections:
        raise ValueError("no provider connection is configured")
    return settings.connections[selected]


def profile_for_connection(
    output_root: str | Path, connection: ProviderConnection
) -> WriterCapabilityProfile | None:
    path = _profile_path(output_root, connection)
    if not path.is_file():
        return None
    try:
        profile = WriterCapabilityProfile.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        raise ValueError("provider capability profile is invalid") from error
    return profile if _profile_identity(profile) == _identity(connection) else None


def check_capability(
    output_root: str | Path,
    connection: ProviderConnection,
    invoke: Callable[[WriterStrategy], str],
    *,
    now: datetime | None = None,
) -> WriterCapabilityProfile:
    """Run the finite Writer contract ladder; never persist request/response bodies."""
    attempts: list[CapabilityAttempt] = []
    chosen: tuple[WriterStrategy, Extractor] | None = None
    for strategy in _STRATEGIES:
        extractor: Extractor = (
            "tool_call_arguments" if strategy == "tool_call" else "message_content"
        )
        try:
            ReaderDraft.model_validate_json(invoke(strategy))
        except Exception as error:
            attempts.append(
                CapabilityAttempt(
                    strategy=strategy,
                    extractor=extractor,
                    outcome="failed",
                    failure_class=_failure_class(error),
                )
            )
            continue
        attempts.append(
            CapabilityAttempt(strategy=strategy, extractor=extractor, outcome="success")
        )
        chosen = (strategy, extractor)
        break
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    identity = _identity(connection)
    profile_id = hashlib.sha256("\0".join(identity).encode("utf-8")).hexdigest()
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "profile_id": profile_id,
        "provider": identity[0],
        "endpoint_identity": identity[1],
        "model": identity[2],
        "adapter_revision": identity[3],
        "status": "verified" if chosen else "failed",
        "checked_at": checked_at.isoformat(),
        "attempts": [item.model_dump(mode="json") for item in attempts],
        "strategy": chosen[0] if chosen else None,
        "extractor": chosen[1] if chosen else None,
    }
    profile_sha = _sha(payload)
    profile = WriterCapabilityProfile(profile_sha256=profile_sha, **payload)
    _write_json(_profile_path(output_root, connection), profile.model_dump(mode="json"))
    return profile


def invalidate_profiles(
    output_root: str | Path, connection: ProviderConnection
) -> None:
    profile = profile_for_connection(output_root, connection)
    if profile is None:
        return
    invalid = profile.model_copy(update={"status": "invalidated"})
    _write_json(_profile_path(output_root, connection), invalid.model_dump(mode="json"))


def _identity(connection: ProviderConnection) -> tuple[str, str, str, str]:
    return (
        connection.provider,
        _normalize_endpoint(connection.endpoint),
        connection.model,
        connection.adapter_revision,
    )


def _profile_identity(profile: WriterCapabilityProfile) -> tuple[str, str, str, str]:
    return (
        profile.provider,
        profile.endpoint_identity,
        profile.model,
        profile.adapter_revision,
    )


def _normalize_endpoint(value: str) -> str:
    return value.strip().rstrip("/").lower()


def _profile_path(output_root: str | Path, connection: ProviderConnection) -> Path:
    identity = "\0".join(_identity(connection))
    return (
        provider_directory(output_root)
        / "profiles"
        / (hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".json")
    )


def _failure_class(
    error: Exception,
) -> Literal["transport", "unsupported", "invalid_contract"]:
    message = str(error).lower()
    if any(
        token in message
        for token in ("unsupported", "response_format", "tool", "schema")
    ):
        return "unsupported"
    if "json" in message or "validation" in message:
        return "invalid_contract"
    return "transport"


def _sha(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp"
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)
