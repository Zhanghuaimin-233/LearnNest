"""Bounded explicit Provider connection checks without payload persistence."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from openai import OpenAI
from pydantic import SecretStr

from learnnest import providers
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.automation_models import AutomationAttempt, AutomationTaskState
from learnnest.automation_store import (
    ProviderAdmissionError,
    admit_provider_call,
    finish_provider_call,
    save_task_state,
)
from learnnest.models import ProviderBindingSnapshot
from learnnest.note_providers import (
    OpenAICompatibleAssistedNoteProvider,
    OpenAICompatibleChatConfig,
)
from learnnest.podcast_providers import OpenAICompatiblePodcastProvider
from learnnest.provider_profiles import (
    Capability,
    ProviderConnection,
    ProviderRoleSnapshot,
    load_settings,
    settings_sha256,
)
from learnnest.provider_secrets import ProviderSecretStore, SecretStoreError
from learnnest.tts_providers import (
    OpenAICompatibleTtsProvider,
    WindowsTtsProvider,
    windows_tts_voice_from_endpoint,
)
from learnnest.tts_generation import validate_wav_bytes


class ProviderConnectionCheckError(ValueError):
    """Safe error for a check that did not invoke or confirm a Provider."""


@dataclass(frozen=True)
class ProviderConnectionCheckResult:
    """Secret-free outcome of one user-confirmed functional check."""

    status: Literal["completed", "failed", "unknown"]
    capability: Capability
    billable: bool


FrozenBinding = ProviderRoleSnapshot | ProviderBindingSnapshot


class AdmittedAssistedProvider:
    """Lazy Writer/Reviewer adapter that admits each planned task separately."""

    def __init__(
        self,
        output_root: str,
        task_id_by_dossier_sha256: dict[str, str],
        stage: Literal["writer", "reviewer"],
        *,
        name: str,
        model: str,
        endpoint_identity: str,
        provider_factory: Callable[[], Any],
    ) -> None:
        self.name = name
        self.model = model
        self.endpoint_identity = endpoint_identity
        self._output_root = output_root
        self._task_id_by_dossier_sha256 = dict(task_id_by_dossier_sha256)
        self._consumed_dossier_sha256: set[str] = set()
        self._stage = stage
        self._provider_factory = provider_factory

    def write_markdown(self, dossier_json: str) -> str:
        if self._stage != "writer":
            raise ValueError("admitted provider has the wrong role")
        return self._invoke("write_markdown", dossier_json)

    def review_markdown(self, dossier_json: str, candidate_markdown: str) -> str:
        if self._stage != "reviewer":
            raise ValueError("admitted provider has the wrong role")
        return self._invoke("review_markdown", dossier_json, candidate_markdown)

    def _invoke(self, method: str, *args: str) -> str:
        dossier_sha256 = hashlib.sha256(args[0].encode("utf-8")).hexdigest()
        try:
            task_id = self._task_id_by_dossier_sha256[dossier_sha256]
        except KeyError as error:
            raise ValueError("assisted plan received an unknown dossier") from error
        if dossier_sha256 in self._consumed_dossier_sha256:
            raise ValueError("assisted plan attempted to consume a dossier twice")
        self._consumed_dossier_sha256.add(dossier_sha256)
        return execute_direct_provider_call(
            self._output_root,
            task_id,
            self._stage,
            lambda: getattr(self._provider_factory(), method)(*args),
        )


def execute_direct_provider_call(
    output_root: str,
    task_id: str,
    stage: Literal["writer", "reviewer", "podcast", "tts"],
    invoke: Callable[[], Any],
    *,
    now: datetime | None = None,
) -> Any:
    """Run one explicit paid operation through the shared admission ledger.

    The callable is deliberately invoked only after the running fact has been
    atomically persisted; callers therefore place Provider construction inside
    it so cap=0 never constructs a client.
    """
    selected_now = (now or datetime.now(UTC)).astimezone(UTC)
    settings = load_settings(output_root)
    policy_sha = settings_sha256(settings)
    pending = AutomationTaskState(
        task_id=f"manual-{stage}-{task_id}", policy_sha256=policy_sha
    )
    try:
        running, admitted = admit_provider_call(
            output_root,
            pending,
            stage=stage,
            now=selected_now,
            retries_per_stage=settings.retries_per_role,
            global_calls_per_day=settings.global_calls_per_day,
            budget_group_calls_per_day=settings.budget_group_calls_per_day,
        )
    except ProviderAdmissionError as error:
        raise ValueError("provider call limit is exhausted") from error
    try:
        result = invoke()
    except Exception as error:
        finish_provider_call(
            output_root,
            running,
            admitted,
            outcome="unknown" if "timeout" in str(error).lower() else "failed",
            now=selected_now,
        )
        raise
    finish_provider_call(
        output_root, running, admitted, outcome="completed", now=selected_now
    )
    return result


def assisted_snapshot_from_binding(
    binding: FrozenBinding,
) -> AssistedConnectionSnapshot:
    """Project one task role binding into the existing assisted-note contract."""
    _require_llm_binding(binding)
    endpoint = _require_endpoint(binding)
    return AssistedConnectionSnapshot(
        connection_name=binding.connection_id,
        connection_id=binding.connection_id,
        secret_id=binding.secret_id,
        provider=binding.provider,
        endpoint_identity=endpoint,
        model=binding.model,
        adapter_revision=binding.adapter_revision,
        settings_sha256=binding.settings_sha256,
    )


def assisted_provider_from_snapshot(
    output_root: str,
    binding: FrozenBinding | AssistedConnectionSnapshot,
    *,
    client_factory: Callable[..., Any] = OpenAI,
) -> OpenAICompatibleAssistedNoteProvider:
    """Construct one Writer/Reviewer transport from an immutable secret reference."""
    if isinstance(binding, AssistedConnectionSnapshot):
        endpoint = binding.endpoint_identity
        secret_id = binding.secret_id
        provider = binding.provider
        model = binding.model
    else:
        _require_llm_binding(binding)
        endpoint = _require_endpoint(binding)
        secret_id = binding.secret_id
        provider = binding.provider
        model = binding.model
    secret = _read_secret(output_root, secret_id)
    return OpenAICompatibleAssistedNoteProvider(
        OpenAICompatibleChatConfig(
            provider_name=provider,
            model=model,
            base_url=endpoint,
            api_key=secret,
        ),
        client_factory=client_factory,
    )


def podcast_provider_from_snapshot(
    output_root: str,
    binding: FrozenBinding,
    *,
    client_factory: Callable[..., Any] = OpenAI,
) -> OpenAICompatiblePodcastProvider:
    """Construct one MiMo or DeepSeek podcast role without provider fallback."""
    _require_llm_binding(binding)
    return OpenAICompatiblePodcastProvider(
        _read_secret(output_root, binding.secret_id),
        provider_name=binding.provider,
        model=binding.model,
        base_url=_require_endpoint(binding),
        client_factory=client_factory,
    )


def tts_provider_from_snapshot(
    output_root: str,
    binding: FrozenBinding,
    *,
    client_factory: Callable[..., Any] = OpenAI,
) -> OpenAICompatibleTtsProvider | WindowsTtsProvider:
    """Construct exactly the frozen MiMo or local Windows TTS adapter."""
    if binding.capability != "tts":
        raise ValueError("frozen binding cannot perform TTS")
    if binding.provider == "windows-tts":
        if binding.model != "system-speech":
            raise ValueError("frozen binding has an unsupported Windows TTS model")
        voice = windows_tts_voice_from_endpoint(binding.endpoint)
        if voice is None:
            raise ValueError("frozen binding has no Windows TTS voice")
        return WindowsTtsProvider(voice)
    if binding.provider != "xiaomi-mimo-tts":
        raise ValueError("frozen binding cannot perform MiMo TTS")
    if binding.model != "mimo-v2.5-tts":
        raise ValueError("frozen binding has an unsupported MiMo TTS model")
    return OpenAICompatibleTtsProvider(
        _read_secret(output_root, binding.secret_id),
        provider_name=binding.provider,
        model=binding.model,
        base_url=_require_endpoint(binding),
        client_factory=client_factory,
    )


def _require_llm_binding(binding: FrozenBinding) -> None:
    if binding.capability != "llm" or binding.provider not in {
        "xiaomi-mimo",
        "deepseek",
    }:
        raise ValueError("frozen binding cannot perform an LLM role")


def _require_endpoint(binding: FrozenBinding) -> str:
    endpoint = binding.endpoint
    if endpoint is None or not endpoint.startswith("https://"):
        raise ValueError("frozen provider endpoint is unavailable")
    return endpoint.rstrip("/")


def _read_secret(output_root: str, secret_id: str | None):
    if secret_id is None:
        raise ValueError("provider secret is unavailable")
    try:
        return ProviderSecretStore(output_root).read(secret_id)
    except SecretStoreError as error:
        raise ValueError("provider secret is unavailable") from error


def run_provider_connection_check(
    output_root: str,
    connection_name: str,
    *,
    now: datetime | None = None,
    client_factory: Callable[..., Any] = OpenAI,
) -> ProviderConnectionCheckResult:
    """Run one real, response-free functional check for the selected connection.

    Local adapters execute their existing isolated smoke probe. Cloud adapters
    persist a running paid-call fact before constructing a client, send exactly
    one minimal request with transport retries disabled, and never save the
    response body. The diagnostic fact does not consume or depend on task daily
    call limits. A later explicit user confirmation may start another check.
    """
    selected_now = (now or datetime.now(UTC)).astimezone(UTC)
    settings = load_settings(output_root)
    connection = settings.connections.get(connection_name)
    if connection is None:
        raise ProviderConnectionCheckError("provider connection does not exist")
    if connection.api_family == "local":
        return _run_local_connection_check(connection)
    if connection.api_family != "openai_chat" or connection.capability not in {
        "llm",
        "tts",
    }:
        raise ProviderConnectionCheckError("connection cannot perform a real check")
    if connection.secret_id is None:
        raise ProviderConnectionCheckError("provider secret is unavailable")
    try:
        secret = ProviderSecretStore(output_root).read(connection.secret_id)
    except SecretStoreError as error:
        raise ProviderConnectionCheckError("provider secret is unavailable") from error

    settings_sha = settings_sha256(settings)
    connection_digest = hashlib.sha256(
        connection.connection_id.encode("utf-8")
    ).hexdigest()[:4]
    task_id = f"pcheck-{connection_digest}-{uuid.uuid4().hex[:12]}"
    pending = AutomationTaskState(
        task_id=task_id,
        policy_sha256=settings_sha,
    )
    stage: Literal["writer", "tts"] = (
        "tts" if connection.capability == "tts" else "writer"
    )
    try:
        running, admitted = admit_provider_call(
            output_root,
            pending,
            stage=stage,
            now=selected_now,
            retries_per_stage=0,
            global_calls_per_day=settings.global_calls_per_day,
            budget_group_calls_per_day=settings.budget_group_calls_per_day,
            counts_toward_limit=False,
        )
    except ProviderAdmissionError as error:
        raise ProviderConnectionCheckError(
            "provider connection check could not be admitted"
        ) from error
    try:
        if connection.capability == "llm":
            _check_llm_connection(connection, secret.get_secret_value(), client_factory)
        else:
            _check_tts_connection(connection, secret, client_factory)
    except Exception as error:
        status: Literal["failed", "unknown"] = (
            "unknown" if "timeout" in str(error).lower() else "failed"
        )
        _save_terminal(output_root, running, admitted, status, selected_now)
        return ProviderConnectionCheckResult(
            status=status,
            capability=connection.capability,
            billable=True,
        )
    _save_terminal(output_root, running, admitted, "completed", selected_now)
    return ProviderConnectionCheckResult(
        status="completed",
        capability=connection.capability,
        billable=True,
    )


def _check_llm_connection(
    connection: ProviderConnection,
    secret: str,
    client_factory: Callable[..., Any],
) -> None:
    client = client_factory(
        api_key=secret,
        base_url=connection.endpoint,
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=connection.model,
        messages=[
            {"role": "system", "content": "Reply with OK only."},
            {"role": "user", "content": "Connection check."},
        ],
        stream=False,
        max_tokens=8,
    )
    if not getattr(response, "choices", None):
        raise RuntimeError("empty provider response")


def _check_tts_connection(
    connection: ProviderConnection,
    secret: SecretStr,
    client_factory: Callable[..., Any],
) -> None:
    provider = OpenAICompatibleTtsProvider(
        secret,
        provider_name=connection.provider,
        model=connection.model,
        base_url=connection.endpoint,
        client_factory=client_factory,
    )
    validate_wav_bytes(provider.synthesize("测试", "自然、清晰地朗读。"))


def _run_local_connection_check(
    connection: ProviderConnection,
) -> ProviderConnectionCheckResult:
    try:
        if connection.provider in {"local-asr", "local-ocr"}:
            command = (
                "doctor-asr" if connection.provider == "local-asr" else "doctor-ocr"
            )
            expected_provider = (
                "faster-whisper" if connection.provider == "local-asr" else "paddleocr"
            )
            payload = json.loads(providers.run_worker([command]).stdout)
            if (
                not isinstance(payload, dict)
                or payload.get("ok") is not True
                or payload.get("provider") != expected_provider
            ):
                raise RuntimeError("local provider probe returned an invalid result")
        elif connection.provider == "windows-tts":
            voice = windows_tts_voice_from_endpoint(connection.endpoint)
            if voice is None:
                raise RuntimeError("Windows TTS voice is unavailable")
            validate_wav_bytes(
                WindowsTtsProvider(voice).synthesize("测试", "自然、清晰地朗读。")
            )
        else:
            raise ProviderConnectionCheckError(
                "connection cannot perform a local check"
            )
    except ProviderConnectionCheckError:
        raise
    except Exception:
        return ProviderConnectionCheckResult(
            status="failed", capability=connection.capability, billable=False
        )
    return ProviderConnectionCheckResult(
        status="completed", capability=connection.capability, billable=False
    )


def _save_terminal(
    output_root: str,
    state: AutomationTaskState,
    admitted: AutomationAttempt,
    status: Literal["completed", "failed", "unknown"],
    now: datetime,
) -> None:
    updated = finish_provider_call(
        output_root, state, admitted, outcome=status, now=now
    ).model_copy(
        update={
            "status": "needs_attention" if status != "completed" else "completed",
            "blocked_reason": "unknown_result" if status == "unknown" else None,
        }
    )
    save_task_state(output_root, updated)
