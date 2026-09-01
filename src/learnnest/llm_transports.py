"""Native LLM wire transports for OpenAI Responses, Anthropic, and Gemini.

This module is deliberately self-contained: it owns the credential-scrubbing
diagnostic helper shared by every provider boundary and never imports the
note or podcast provider layers.  Each transport speaks exactly one official
protocol; there is no hidden protocol conversion between API families.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import SecretStr

NATIVE_LLM_TIMEOUT_SECONDS = 600.0
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MAX_TOKENS = 8192
DEFAULT_LLM_SAFE_INPUT_TOKENS = 64_000
NativeLlmFamily = Literal["openai_responses", "anthropic", "gemini"]

_API_KEY_PATTERN = re.compile(r"\b(?:sk|tp)-[A-Za-z0-9_-]{8,}\b")
Message = dict[str, str]
HttpClientFactory = Callable[..., Any]


class NativeLlmTransportError(RuntimeError):
    """Sanitized transport error used when no domain error type is supplied."""


@dataclass(frozen=True)
class NativeLlmConfig:
    """One explicit native-transport identity for a frozen LLM connection."""

    provider_name: str
    model: str
    base_url: str
    api_key: SecretStr
    api_family: NativeLlmFamily
    safe_input_tokens: int = DEFAULT_LLM_SAFE_INPUT_TOKENS


class _HttpStatusCarrier(RuntimeError):
    """Carry one HTTP status and parsed body into the shared diagnostic scrubber."""

    def __init__(self, status_code: int, body: Mapping[str, Any] | None) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}")


def safe_provider_diagnostic(error: Exception, api_key: SecretStr) -> str:
    secret = api_key.get_secret_value()
    status_code = getattr(error, "status_code", None)
    if not isinstance(status_code, int):
        return type(error).__name__

    parts = [f"HTTP {status_code}"]
    body = getattr(error, "body", None)
    response = getattr(error, "response", None)
    details: Mapping[str, Any] | None = None
    if isinstance(body, Mapping):
        nested = body.get("error")
        details = nested if isinstance(nested, Mapping) else body
    if details is None and response is not None:
        try:
            response_body = response.json()
        except (ValueError, TypeError):
            response_body = None
        if isinstance(response_body, Mapping):
            nested = response_body.get("error")
            details = nested if isinstance(nested, Mapping) else response_body
    if details is not None:
        for key in ("code", "type", "message"):
            value = details.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                parts.append(f"{key}={_scrub_diagnostic(str(value), secret)}")

    request_id = getattr(error, "request_id", None)
    if not isinstance(request_id, str) or not request_id.strip():
        headers = getattr(response, "headers", None)
        if headers is not None:
            request_id = headers.get("x-request-id")
    if isinstance(request_id, str) and request_id.strip():
        parts.append(f"request_id={_scrub_diagnostic(request_id, secret)}")
    return "; ".join(parts)


def _scrub_diagnostic(value: str, secret: str) -> str:
    scrubbed = value.replace(secret, "***") if secret else value
    scrubbed = _API_KEY_PATTERN.sub("***", scrubbed)
    return " ".join(scrubbed.split())[:240]


class _NativeLlmTransport:
    """Shared request/error skeleton for the native wire transports."""

    def __init__(
        self,
        config: NativeLlmConfig,
        *,
        error_type: type[Exception] = NativeLlmTransportError,
        http_client_factory: HttpClientFactory = httpx.Client,
    ) -> None:
        secret = config.api_key.get_secret_value()
        if (
            not config.provider_name.strip()
            or not config.model.strip()
            or not config.base_url.strip()
        ):
            raise error_type("native LLM provider snapshot is invalid")
        if not secret.strip():
            raise error_type("native LLM provider secret is unavailable")
        self.name = config.provider_name
        self.model = config.model
        self.safe_input_tokens = config.safe_input_tokens
        self._api_key = config.api_key
        self._secret = secret
        self._base_url = config.base_url.strip().rstrip("/")
        self._error_type = error_type
        self._http_client_factory = http_client_factory

    # -- public completion API ------------------------------------------ #

    def complete_text(self, messages: list[Message], *, operation: str) -> str:
        body = self._request(self._build_text_payload(messages), operation=operation)
        text = self._extract_text(body)
        if not text.strip():
            raise self._error_type(
                f"{self.name} {operation} returned no message content"
            )
        return text

    def complete_json(
        self,
        messages: list[Message],
        *,
        operation: str,
        schema: dict[str, object],
        schema_name: str,
    ) -> str:
        body = self._request(
            self._build_json_payload(messages, schema=schema, schema_name=schema_name),
            operation=operation,
        )
        try:
            text = self._extract_json(body, schema_name=schema_name)
        except Exception as error:
            raise self._error_type(
                f"{self.name} {operation} returned an unreadable structured response"
            ) from error
        if not text.strip():
            raise self._error_type(
                f"{self.name} {operation} returned no structured JSON"
            )
        return text

    # -- shared request plumbing ---------------------------------------- #

    def _request(self, payload: dict[str, object], *, operation: str) -> object:
        try:
            client = self._http_client_factory(timeout=NATIVE_LLM_TIMEOUT_SECONDS)
            with client:
                response = client.post(
                    self._request_url(),
                    headers=self._request_headers(),
                    json=payload,
                )
            body = self._response_body(response)
            status_code = getattr(response, "status_code", None)
            if isinstance(status_code, int) and status_code >= 400:
                raise _HttpStatusCarrier(
                    status_code,
                    body if isinstance(body, Mapping) else None,
                )
        except Exception as error:
            raise self._error_type(
                f"{self.name} {operation} failed: "
                f"{safe_provider_diagnostic(error, self._api_key)}"
            ) from error
        return body

    @staticmethod
    def _response_body(response: Any) -> object:
        try:
            return response.json()
        except Exception:
            return None

    def _request_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._secret}",
            "Content-Type": "application/json",
        }

    def _request_url(self) -> str:
        raise NotImplementedError

    def _build_text_payload(self, messages: list[Message]) -> dict[str, object]:
        raise NotImplementedError

    def _build_json_payload(
        self,
        messages: list[Message],
        *,
        schema: dict[str, object],
        schema_name: str,
    ) -> dict[str, object]:
        del schema, schema_name
        return self._build_text_payload(messages)

    def _extract_text(self, body: object) -> str:
        raise NotImplementedError

    def _extract_json(self, body: object, *, schema_name: str) -> str:
        del schema_name
        return self._extract_text(body)


class OpenAIResponsesTransport(_NativeLlmTransport):
    """OpenAI Responses API: POST {base}/responses with text.format JSON mode."""

    def _request_url(self) -> str:
        return f"{self._base_url}/responses"

    def _build_text_payload(self, messages: list[Message]) -> dict[str, object]:
        return {"model": self.model, "input": messages, "stream": False}

    def _build_json_payload(
        self,
        messages: list[Message],
        *,
        schema: dict[str, object],
        schema_name: str,
    ) -> dict[str, object]:
        del schema, schema_name
        return {
            "model": self.model,
            "input": messages,
            "stream": False,
            "text": {"format": {"type": "json_object"}},
        }

    def _extract_text(self, body: object) -> str:
        parts: list[str] = []
        if isinstance(body, Mapping):
            output = body.get("output")
            if isinstance(output, list):
                for item in output:
                    if not (
                        isinstance(item, Mapping) and item.get("type") == "message"
                    ):
                        continue
                    content = item.get("content")
                    if not isinstance(content, list):
                        continue
                    for part in content:
                        if (
                            isinstance(part, Mapping)
                            and part.get("type") == "output_text"
                            and isinstance(part.get("text"), str)
                        ):
                            parts.append(part["text"])
        return "".join(parts)


class AnthropicMessagesTransport(_NativeLlmTransport):
    """Anthropic Messages API: POST {base}/v1/messages with native tool use."""

    def _request_url(self) -> str:
        return f"{self._base_url}/v1/messages"

    def _request_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._secret,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    @staticmethod
    def _split_system(
        messages: list[Message],
    ) -> tuple[str, list[Message]]:
        system = "\n\n".join(
            message["content"]
            for message in messages
            if message.get("role") == "system"
        )
        rest = [
            {"role": message["role"], "content": message["content"]}
            for message in messages
            if message.get("role") != "system"
        ]
        return system, rest

    def _build_text_payload(self, messages: list[Message]) -> dict[str, object]:
        system, rest = self._split_system(messages)
        return {
            "model": self.model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": system,
            "messages": rest,
        }

    def _build_json_payload(
        self,
        messages: list[Message],
        *,
        schema: dict[str, object],
        schema_name: str,
    ) -> dict[str, object]:
        system, rest = self._split_system(messages)
        return {
            "model": self.model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "system": system,
            "messages": rest,
            "tools": [
                {
                    "name": schema_name,
                    "description": f"Return exactly one {schema_name} JSON object.",
                    "input_schema": schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": schema_name},
        }

    def _extract_text(self, body: object) -> str:
        parts: list[str] = []
        if isinstance(body, Mapping):
            content = body.get("content")
            if isinstance(content, list):
                for item in content:
                    if (
                        isinstance(item, Mapping)
                        and item.get("type") == "text"
                        and isinstance(item.get("text"), str)
                    ):
                        parts.append(item["text"])
        return "".join(parts)

    def _extract_json(self, body: object, *, schema_name: str) -> str:
        del schema_name
        if isinstance(body, Mapping):
            content = body.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, Mapping) and item.get("type") == "tool_use":
                        tool_input = item.get("input")
                        if isinstance(tool_input, Mapping):
                            return json.dumps(
                                tool_input,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
        return ""


class GeminiGenerateContentTransport(_NativeLlmTransport):
    """Gemini API: POST {base}/v1beta/models/{model}:generateContent."""

    def _request_url(self) -> str:
        return (
            f"{self._base_url}/v1beta/models/{quote(self.model, safe='')}"
            ":generateContent"
        )

    def _request_headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self._secret,
            "Content-Type": "application/json",
        }

    def _gemini_payload(
        self,
        messages: list[Message],
        *,
        generation_config: dict[str, object] | None = None,
    ) -> dict[str, object]:
        system_parts = [
            message["content"]
            for message in messages
            if message.get("role") == "system"
        ]
        contents: list[dict[str, object]] = []
        for message in messages:
            if message.get("role") == "system":
                continue
            role = "model" if message.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message["content"]}]})
        payload: dict[str, object] = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {
                "parts": [{"text": "\n\n".join(system_parts)}]
            }
        if generation_config is not None:
            payload["generationConfig"] = generation_config
        return payload

    def _build_text_payload(self, messages: list[Message]) -> dict[str, object]:
        return self._gemini_payload(messages)

    def _build_json_payload(
        self,
        messages: list[Message],
        *,
        schema: dict[str, object],
        schema_name: str,
    ) -> dict[str, object]:
        del schema, schema_name
        return self._gemini_payload(
            messages,
            generation_config={"responseMimeType": "application/json"},
        )

    def _extract_text(self, body: object) -> str:
        parts: list[str] = []
        if isinstance(body, Mapping):
            candidates = body.get("candidates")
            if isinstance(candidates, list) and candidates:
                candidate = candidates[0]
                if isinstance(candidate, Mapping):
                    content = candidate.get("content")
                    if isinstance(content, Mapping):
                        pieces = content.get("parts")
                        if isinstance(pieces, list):
                            for piece in pieces:
                                if isinstance(piece, Mapping) and isinstance(
                                    piece.get("text"), str
                                ):
                                    parts.append(piece["text"])
        return "".join(parts)


_NATIVE_TRANSPORTS: dict[str, type[_NativeLlmTransport]] = {
    "openai_responses": OpenAIResponsesTransport,
    "anthropic": AnthropicMessagesTransport,
    "gemini": GeminiGenerateContentTransport,
}


def build_native_llm_transport(
    config: NativeLlmConfig,
    *,
    error_type: type[Exception] = NativeLlmTransportError,
    http_client_factory: HttpClientFactory = httpx.Client,
) -> _NativeLlmTransport:
    """Construct exactly the transport declared by the connection's api_family."""
    try:
        transport_class = _NATIVE_TRANSPORTS[config.api_family]
    except KeyError as error:
        raise ValueError(
            "unsupported native LLM api family for this connection"
        ) from error
    return transport_class(
        config, error_type=error_type, http_client_factory=http_client_factory
    )
