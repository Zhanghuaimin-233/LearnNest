"""Secret-free product Provider settings, connections, and role snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from learnnest.provider_secrets import ProviderSecretStore, SecretStoreError

Capability = Literal["asr", "ocr", "llm", "tts"]
WriterStrategy = Literal[
    "native_json_schema", "tool_call", "json_object", "prompted_json"
]
Extractor = Literal["message_content", "tool_call_arguments"]
ProviderRole = Literal["note_writer", "note_reviewer", "podcast", "tts", "asr", "ocr"]
ProviderBudgetGroup = Literal["note", "podcast", "tts", "asr", "ocr"]
AuthorizationMode = Literal["local_only", "automatic"]
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WINDOWS_TTS_PREFIX = "local://windows-tts/"
_ROLE_CAPABILITIES: dict[ProviderRole, Capability] = {
    "note_writer": "llm",
    "note_reviewer": "llm",
    "podcast": "llm",
    "tts": "tts",
    "asr": "asr",
    "ocr": "ocr",
}
_ROLE_BUDGET_GROUPS: dict[ProviderRole, ProviderBudgetGroup] = {
    "note_writer": "note",
    "note_reviewer": "note",
    "podcast": "podcast",
    "tts": "tts",
    "asr": "asr",
    "ocr": "ocr",
}


class ProviderConnectionNotFoundError(ValueError):
    """The requested connection does not exist."""


class ProviderConnectionBoundError(ValueError):
    """A connection cannot be deleted while a workflow role uses it."""

    def __init__(self, roles: tuple[ProviderRole, ...]) -> None:
        self.roles = roles
        super().__init__("provider connection is still bound")


class ProviderRoleNotBoundError(ValueError):
    """The requested workflow role has no configured connection."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProviderPreset(_Model):
    preset: str
    capability: Capability
    provider: str
    api_family: Literal["openai_chat", "local"]
    endpoint: str
    default_model: str
    adapter_revision: str = "1"
    requires_secret: bool = True


# OpenAI-compatible is a transport implementation detail shared by only the
# MiMo and DeepSeek adapters; it is intentionally absent as a product choice.
PRESETS: dict[str, ProviderPreset] = {
    "mimo": ProviderPreset(
        preset="mimo",
        capability="llm",
        provider="xiaomi-mimo",
        api_family="openai_chat",
        endpoint="https://api.xiaomimimo.com/v1",
        default_model="mimo-v2.5",
    ),
    "deepseek": ProviderPreset(
        preset="deepseek",
        capability="llm",
        provider="deepseek",
        api_family="openai_chat",
        endpoint="https://api.deepseek.com/v1",
        default_model="deepseek-v4-pro",
    ),
    "mimo-tts": ProviderPreset(
        preset="mimo-tts",
        capability="tts",
        provider="xiaomi-mimo-tts",
        api_family="openai_chat",
        endpoint="https://api.xiaomimimo.com/v1",
        default_model="mimo-v2.5-tts",
    ),
    "local-asr": ProviderPreset(
        preset="local-asr",
        capability="asr",
        provider="local-asr",
        api_family="local",
        endpoint="local://asr",
        default_model="local-installed",
        requires_secret=False,
    ),
    "local-ocr": ProviderPreset(
        preset="local-ocr",
        capability="ocr",
        provider="local-ocr",
        api_family="local",
        endpoint="local://ocr",
        default_model="local-installed",
        requires_secret=False,
    ),
}

# ``PRESETS`` remains the pre-existing public product registry consumed by
# legacy CLI compatibility. Windows voice selection is a local connection-only
# choice exposed by the settings service, not a cloud Provider product option.
_CONNECTION_PRESETS: dict[str, ProviderPreset] = {
    **PRESETS,
    "windows-tts": ProviderPreset(
        preset="windows-tts",
        capability="tts",
        provider="windows-tts",
        api_family="local",
        endpoint="local://windows-tts",
        default_model="system-speech",
        requires_secret=False,
    ),
}


class ProviderConnection(_Model):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    connection_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    preset: str
    capability: Capability
    provider: str
    api_family: Literal["openai_chat", "local"]
    endpoint: str
    model: str
    secret_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    adapter_revision: str = Field(min_length=1, max_length=32)
    updated_at: datetime


class ProviderRoleBinding(_Model):
    role: ProviderRole
    connection_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ProviderRoleSnapshot(_Model):
    capability: Capability
    connection_id: str
    secret_id: str | None = None
    provider: str
    endpoint: str | None = None
    model: str
    adapter_revision: str
    settings_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class WriterCapabilityProfile(_Model):
    """Retained internal capability evidence for the shared LLM transport."""

    schema_version: Literal["1.0"] = "1.0"
    profile_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider: str
    endpoint_identity: str
    model: str
    adapter_revision: str
    status: Literal["verified", "failed", "invalidated"]
    checked_at: datetime
    attempts: list[object] = Field(default_factory=list)
    strategy: WriterStrategy | None = None
    extractor: Extractor | None = None


class ProviderSettings(_Model):
    schema_version: Literal["2.0"] = "2.0"
    connections: dict[str, ProviderConnection] = Field(default_factory=dict)
    role_bindings: dict[ProviderRole, ProviderRoleBinding] = Field(default_factory=dict)
    retries_per_role: int = Field(default=1, ge=0, le=3)
    global_calls_per_day: int = Field(default=80, ge=0, le=800)
    budget_group_calls_per_day: dict[ProviderBudgetGroup, int] = Field(
        default_factory=lambda: {
            "note": 20,
            "podcast": 20,
            "tts": 20,
            "asr": 20,
            "ocr": 20,
        }
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_role_caps(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy = data.pop("role_calls_per_day", None)
        if "budget_group_calls_per_day" not in data and isinstance(legacy, dict):
            try:
                data["budget_group_calls_per_day"] = {
                    "note": min(
                        int(legacy["note_writer"]), int(legacy["note_reviewer"])
                    ),
                    "podcast": int(legacy["podcast"]),
                    "tts": int(legacy["tts"]),
                    "asr": int(legacy["asr"]),
                    "ocr": int(legacy["ocr"]),
                }
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("legacy provider role caps are invalid") from error
        return data

    @model_validator(mode="after")
    def valid_connections_and_caps(self) -> ProviderSettings:
        for name, connection in self.connections.items():
            if name != connection.name or name != connection.connection_id:
                raise ValueError("provider connection identity is invalid")
            if connection.preset not in _CONNECTION_PRESETS:
                raise ValueError("unsupported provider")
        if set(self.budget_group_calls_per_day) != set(_ROLE_BUDGET_GROUPS.values()):
            raise ValueError("provider budget group caps must be complete")
        for role, binding in self.role_bindings.items():
            if binding.role != role or binding.connection_id not in self.connections:
                raise ValueError("provider role binding is invalid")
            if (
                self.connections[binding.connection_id].capability
                != _ROLE_CAPABILITIES[role]
            ):
                raise ValueError("provider role capability does not match connection")
        self._validate_domain_isolation()
        return self

    def _validate_domain_isolation(self) -> None:
        note_connections = {
            binding.connection_id
            for role, binding in self.role_bindings.items()
            if role in {"note_writer", "note_reviewer"}
        }
        note_secret_ids = {
            self.connections[connection_id].secret_id
            for connection_id in note_connections
            if self.connections[connection_id].secret_id is not None
        }
        podcast = self.role_bindings.get("podcast")
        if podcast is not None:
            connection = self.connections[podcast.connection_id]
            if (
                podcast.connection_id in note_connections
                or connection.secret_id in note_secret_ids
            ):
                raise ValueError(
                    "podcast connection must be isolated from the note domain"
                )
        tts = self.role_bindings.get("tts")
        if tts is not None:
            connection = self.connections[tts.connection_id]
            used_connections = note_connections | (
                {podcast.connection_id} if podcast else set()
            )
            used_secret_ids = note_secret_ids | (
                {self.connections[podcast.connection_id].secret_id}
                if podcast is not None
                and self.connections[podcast.connection_id].secret_id is not None
                else set()
            )
            if (
                tts.connection_id in used_connections
                or connection.secret_id in used_secret_ids
            ):
                raise ValueError("tts connection must be isolated from LLM domains")

    def call_allowed(
        self, role: ProviderRole, *, used_global: int, used_group: int
    ) -> bool:
        return (
            used_global < self.global_calls_per_day
            and used_group < self.budget_group_calls_per_day[_ROLE_BUDGET_GROUPS[role]]
        )

    def budget_group_for(self, role: ProviderRole) -> ProviderBudgetGroup:
        return _ROLE_BUDGET_GROUPS[role]


def provider_directory(output_root: str | Path) -> Path:
    return Path(output_root).resolve() / ".learnnest" / "providers"


def connection_presets() -> dict[str, ProviderPreset]:
    """Return the settings connection choices, including local Windows TTS."""
    return dict(_CONNECTION_PRESETS)


def default_windows_tts_voice() -> str:
    # Kept lazy because note providers import this settings module while the
    # TTS transport imports the shared diagnostic helper from note providers.
    from learnnest.tts_providers import default_windows_tts_voice as select_voice

    return select_voice()


def _windows_tts_endpoint(voice: str) -> str:
    if not voice.strip():
        raise ValueError("Windows TTS voice is unavailable")
    return _WINDOWS_TTS_PREFIX + quote(voice, safe="")


def _windows_tts_voice_from_endpoint(endpoint: str | None) -> str | None:
    if not isinstance(endpoint, str) or not endpoint.startswith(_WINDOWS_TTS_PREFIX):
        return None
    voice = unquote(endpoint.removeprefix(_WINDOWS_TTS_PREFIX))
    return voice if voice.strip() else None


def load_settings(output_root: str | Path) -> ProviderSettings:
    path = provider_directory(output_root) / "settings.json"
    if not path.is_file():
        return ProviderSettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("not an object")
        if payload.get("schema_version") == "1.0":
            payload = _migrate_v1(payload)
        return ProviderSettings.model_validate(payload)
    except (OSError, UnicodeError, ValidationError, ValueError, TypeError) as error:
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
    secret_value: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    now: datetime | None = None,
) -> ProviderConnection:
    if not _NAME.fullmatch(name):
        raise ValueError("provider connection name is invalid")
    try:
        base = _CONNECTION_PRESETS[preset]
    except KeyError as error:
        raise ValueError("unsupported provider") from error
    selected_endpoint = endpoint or base.endpoint
    selected_model = model or base.default_model
    if selected_model != base.default_model:
        raise ValueError("unsupported provider model")
    if base.api_family == "openai_chat" and not selected_endpoint.startswith(
        "https://"
    ):
        raise ValueError("provider endpoint is invalid")
    if base.requires_secret and not (secret_value and secret_value.strip()):
        raise ValueError("provider secret is required")
    if base.preset == "windows-tts":
        if endpoint is not None or model is not None:
            raise ValueError("Windows TTS connection is fixed to System.Speech")
        selected_endpoint = _windows_tts_endpoint(voice or default_windows_tts_voice())
    elif voice is not None:
        raise ValueError("voice selection is only available for Windows TTS")
    settings = load_settings(output_root)
    old_connection = settings.connections.get(name)
    if old_connection is not None and old_connection.capability != base.capability:
        if any(
            binding.connection_id == name for binding in settings.role_bindings.values()
        ):
            raise ValueError(
                "provider connection update is incompatible with its binding"
            )
    secret_id = uuid.uuid4().hex if base.requires_secret else None
    connection = ProviderConnection(
        name=name,
        connection_id=name,
        preset=base.preset,
        capability=base.capability,
        provider=base.provider,
        api_family=base.api_family,
        endpoint=selected_endpoint.rstrip("/"),
        model=selected_model,
        secret_id=secret_id,
        adapter_revision=base.adapter_revision,
        updated_at=(now or datetime.now(UTC)).astimezone(UTC),
    )
    candidate = settings.model_copy(
        update={"connections": {**settings.connections, name: connection}}
    )
    try:
        validated = ProviderSettings.model_validate(candidate.model_dump(mode="python"))
    except ValidationError as error:
        raise ValueError(
            "provider connection update is incompatible with its binding"
        ) from error
    if secret_id is not None:
        try:
            ProviderSecretStore(output_root).put(
                name, secret_value or "", secret_id=secret_id
            )
        except SecretStoreError as error:
            raise ValueError("provider secret is unavailable") from error
    try:
        save_settings(output_root, validated)
    except Exception:
        if secret_id is not None:
            try:
                ProviderSecretStore(output_root).path_for(secret_id).unlink(
                    missing_ok=True
                )
            except OSError:
                pass
        raise
    return connection


def delete_connection(output_root: str | Path, *, name: str) -> ProviderSettings:
    """Remove one unbound connection and its ciphertext, if any.

    Ciphertext is moved aside before settings are changed, so every expected
    failure can restore the original usable connection without handling the
    plaintext secret.
    """
    settings = load_settings(output_root)
    try:
        connection = settings.connections[name]
    except KeyError as error:
        raise ProviderConnectionNotFoundError(
            "provider connection is not configured"
        ) from error
    roles = tuple(
        role
        for role, binding in settings.role_bindings.items()
        if binding.connection_id == name
    )
    if roles:
        raise ProviderConnectionBoundError(roles)
    candidate = settings.model_copy(
        update={
            "connections": {
                connection_name: item
                for connection_name, item in settings.connections.items()
                if connection_name != name
            }
        }
    )
    validated = ProviderSettings.model_validate(candidate.model_dump(mode="python"))
    store = ProviderSecretStore(output_root)
    staged = None
    try:
        if connection.secret_id is not None:
            staged = store.stage_for_deletion(connection.secret_id)
        save_settings(output_root, validated)
    except (OSError, SecretStoreError, ValueError) as error:
        if staged is not None:
            try:
                store.restore_staged_deletion(staged)
            except SecretStoreError as rollback_error:
                raise ValueError(
                    "provider connection deletion could not be rolled back"
                ) from rollback_error
        raise ValueError("provider connection cannot be deleted") from error
    if staged is not None:
        try:
            store.discard_staged_deletion(staged)
        except SecretStoreError as error:
            try:
                save_settings(output_root, settings)
                store.restore_staged_deletion(staged)
            except (OSError, SecretStoreError, ValueError) as rollback_error:
                raise ValueError(
                    "provider connection deletion could not be rolled back"
                ) from rollback_error
            raise ValueError("provider connection cannot be deleted") from error
    return validated


def get_connection(
    output_root: str | Path, name: str | None = None
) -> ProviderConnection:
    settings = load_settings(output_root)
    if name is None:
        candidates = [
            connection
            for connection in settings.connections.values()
            if connection.capability == "llm"
        ]
        if len(candidates) != 1:
            raise ValueError("no provider connection is configured")
        return candidates[0]
    try:
        return settings.connections[name]
    except KeyError as error:
        raise ValueError("provider connection is not configured") from error


def profile_for_connection(
    _output_root: str | Path, _connection: ProviderConnection
) -> WriterCapabilityProfile | None:
    """Compatibility reader for retired v1 capability files.

    V2 no longer trusts an inferred profile for an arbitrary model; formal
    capability checks are bound to the product registry and are added through
    the explicit connection-check service.
    """
    return None


def set_role_binding(
    output_root: str | Path, *, role: ProviderRole, connection_name: str
) -> ProviderSettings:
    settings = load_settings(output_root)
    connection = get_connection(output_root, connection_name)
    if connection.capability != _ROLE_CAPABILITIES[role]:
        raise ValueError("provider role capability does not match connection")
    candidate = settings.model_copy(
        update={
            "role_bindings": {
                **settings.role_bindings,
                role: ProviderRoleBinding(role=role, connection_id=connection_name),
            }
        }
    )
    try:
        validated = ProviderSettings.model_validate(candidate.model_dump(mode="python"))
    except ValidationError as error:
        raise ValueError("provider role binding is incompatible") from error
    save_settings(output_root, validated)
    return validated


def clear_role_binding(
    output_root: str | Path, *, role: ProviderRole
) -> ProviderSettings:
    """Remove one role binding without deleting its connection or secret."""
    settings = load_settings(output_root)
    if role not in settings.role_bindings:
        raise ProviderRoleNotBoundError("provider role is not bound")
    candidate = settings.model_copy(
        update={
            "role_bindings": {
                bound_role: binding
                for bound_role, binding in settings.role_bindings.items()
                if bound_role != role
            }
        }
    )
    validated = ProviderSettings.model_validate(candidate.model_dump(mode="python"))
    save_settings(output_root, validated)
    return validated


def update_limits(
    output_root: str | Path,
    *,
    retries_per_role: int,
    global_calls_per_day: int,
    budget_group_calls_per_day: dict[ProviderBudgetGroup, int],
) -> ProviderSettings:
    """Persist the bounded automatic-call policy without authorizing it."""
    settings = load_settings(output_root).model_copy(
        update={
            "retries_per_role": retries_per_role,
            "global_calls_per_day": global_calls_per_day,
            "budget_group_calls_per_day": budget_group_calls_per_day,
        }
    )
    validated = ProviderSettings.model_validate(settings.model_dump(mode="python"))
    save_settings(output_root, validated)
    return validated


def freeze_role_bindings(output_root: str | Path) -> dict[str, ProviderRoleSnapshot]:
    settings = load_settings(output_root)
    digest = settings_sha256(settings)
    snapshots: dict[str, ProviderRoleSnapshot] = {}
    for role, binding in settings.role_bindings.items():
        connection = settings.connections[binding.connection_id]
        snapshots[role] = ProviderRoleSnapshot(
            capability=connection.capability,
            connection_id=connection.connection_id,
            secret_id=connection.secret_id,
            provider=connection.provider,
            endpoint=connection.endpoint,
            model=connection.model,
            adapter_revision=connection.adapter_revision,
            settings_sha256=digest,
        )
    return snapshots


def settings_sha256(settings: ProviderSettings) -> str:
    payload = settings.model_dump(mode="json")
    for connection in payload["connections"].values():
        connection.pop("updated_at", None)
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def public_settings(output_root: str | Path) -> dict[str, object]:
    settings = load_settings(output_root)
    return {
        "schema_version": settings.schema_version,
        "connections": [
            {
                "name": item.name,
                "capability": item.capability,
                "provider": item.provider,
                "model": item.model,
                "configured": item.secret_id is not None or item.api_family == "local",
                "secret_status": (
                    "configured"
                    if item.secret_id is not None
                    else "local_configured"
                    if item.api_family == "local"
                    else "unconfigured"
                ),
                "voice": _windows_tts_voice_from_endpoint(item.endpoint)
                if item.provider == "windows-tts"
                else None,
            }
            for item in sorted(
                settings.connections.values(), key=lambda item: item.name
            )
        ],
        "role_bindings": {
            role: binding.connection_id
            for role, binding in settings.role_bindings.items()
        },
        "retries_per_role": settings.retries_per_role,
        "global_calls_per_day": settings.global_calls_per_day,
        "budget_group_calls_per_day": settings.budget_group_calls_per_day,
        "settings_sha256": settings_sha256(settings),
    }


def _migrate_v1(payload: dict[str, object]) -> dict[str, object]:
    # V1 stored only environment-variable references.  They cannot become a
    # v2 secret object implicitly, so retain no usable connection and force an
    # explicit WebUI/CLI reconfiguration.
    del payload
    return ProviderSettings().model_dump(mode="json")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp"
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)
