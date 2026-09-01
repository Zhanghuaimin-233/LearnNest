"""Provider boundary for structured podcast scripts."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

import httpx
from openai import OpenAI
from pydantic import SecretStr

from learnnest.llm_transports import (
    NativeLlmConfig,
    build_native_llm_transport,
)
from learnnest.note_providers import safe_provider_diagnostic
from learnnest.podcast_models import PodcastScript

_MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
_MIMO_MODEL = "mimo-v2.5"
_PODCAST_SCHEMA = json.dumps(
    PodcastScript.model_json_schema(),
    ensure_ascii=False,
    separators=(",", ":"),
)
_SYSTEM_PROMPT = f"""Return exactly one JSON object matching this PodcastScript schema 1.0:
{_PODCAST_SCHEMA}
Use only the supplied content_pack and source note. The source note may be a validated active
note or a route-labeled model-reviewed Markdown note; never upgrade its stated review status.
Make the script natural spoken
Chinese for one narrator. Describe visual information in words. Never put evidence IDs,
Markdown, URLs, paths, code fences, or Obsidian links inside segment text. Every segment must
cite real evidence_ids outside its text. Put non-video context only in ai_supplements."""


class PodcastProviderError(RuntimeError):
    """A provider failure whose message is safe for persisted metadata."""


def _podcast_messages(
    source_context_json: str, validation_feedback: tuple[str, ...]
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": source_context_json},
    ]
    if validation_feedback:
        feedback = "\n".join(f"- {error}" for error in validation_feedback)
        messages.append(
            {
                "role": "user",
                "content": (
                    "The previous JSON was invalid. Correct only these validator "
                    f"errors without adding evidence:\n{feedback}"
                ),
            }
        )
    return messages


class PodcastProvider(Protocol):
    name: str
    model: str

    def generate(
        self,
        source_context_json: str,
        validation_feedback: tuple[str, ...],
    ) -> str: ...


class OpenAICompatiblePodcastProvider:
    """One explicit OpenAI-compatible transport for a frozen podcast role."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        provider_name: str,
        model: str,
        base_url: str,
        client_factory: Callable[..., Any] = OpenAI,
    ) -> None:
        secret = api_key.get_secret_value()
        if not secret.strip():
            raise PodcastProviderError("podcast provider secret is unavailable")
        if not provider_name.strip() or not model.strip() or not base_url.strip():
            raise PodcastProviderError("podcast provider snapshot is invalid")
        self.name = provider_name
        self.model = model
        self._api_key = api_key
        self._client = client_factory(
            api_key=secret,
            base_url=base_url,
            max_retries=0,
        )

    def generate(
        self,
        source_context_json: str,
        validation_feedback: tuple[str, ...],
    ) -> str:
        messages = _podcast_messages(source_context_json, validation_feedback)
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
        except Exception as error:
            raise PodcastProviderError(
                "podcast provider failed: "
                f"{safe_provider_diagnostic(error, self._api_key)}"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise PodcastProviderError("MiMo returned no message content")
        return content


class NativeLlmPodcastProvider:
    """One native Responses/Anthropic/Gemini transport for a frozen podcast role."""

    def __init__(
        self,
        config: NativeLlmConfig,
        *,
        http_client_factory: Callable[..., Any] = httpx.Client,
    ) -> None:
        self._transport = build_native_llm_transport(
            config,
            error_type=PodcastProviderError,
            http_client_factory=http_client_factory,
        )
        self.name = self._transport.name
        self.model = self._transport.model

    def generate(
        self,
        source_context_json: str,
        validation_feedback: tuple[str, ...],
    ) -> str:
        return self._transport.complete_json(
            _podcast_messages(source_context_json, validation_feedback),
            operation="podcast provider",
            schema=PodcastScript.model_json_schema(),
            schema_name="podcast_script_v1",
        )


class MimoPodcastProvider(OpenAICompatiblePodcastProvider):
    """Compatibility wrapper for the explicit legacy MiMo CLI entrypoint."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        client_factory: Callable[..., Any] = OpenAI,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise PodcastProviderError("MIMO_API_KEY is missing")
        super().__init__(
            api_key,
            provider_name="xiaomi-mimo",
            model=_MIMO_MODEL,
            base_url=_MIMO_BASE_URL,
            client_factory=client_factory,
        )
