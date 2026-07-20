"""Provider boundary for structured podcast scripts."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from openai import OpenAI
from pydantic import SecretStr

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
Use only the supplied content_pack and validated active note. Make the script natural spoken
Chinese for one narrator. Describe visual information in words. Never put evidence IDs,
Markdown, URLs, paths, code fences, or Obsidian links inside segment text. Every segment must
cite real evidence_ids outside its text. Put non-video context only in ai_supplements."""


class PodcastProviderError(RuntimeError):
    """A provider failure whose message is safe for persisted metadata."""


class PodcastProvider(Protocol):
    name: str
    model: str

    def generate(
        self,
        source_context_json: str,
        validation_feedback: tuple[str, ...],
    ) -> str: ...


class MimoPodcastProvider:
    name = "xiaomi-mimo"
    model = _MIMO_MODEL

    def __init__(
        self,
        api_key: SecretStr,
        *,
        client_factory: Callable[..., Any] = OpenAI,
    ) -> None:
        secret = api_key.get_secret_value()
        if not secret.strip():
            raise PodcastProviderError("MIMO_API_KEY is missing")
        self._api_key = api_key
        self._client = client_factory(
            api_key=secret,
            base_url=_MIMO_BASE_URL,
            max_retries=0,
        )

    def generate(
        self,
        source_context_json: str,
        validation_feedback: tuple[str, ...],
    ) -> str:
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
                "MiMo podcast provider failed: "
                f"{safe_provider_diagnostic(error, self._api_key)}"
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise PodcastProviderError("MiMo returned no message content")
        return content
