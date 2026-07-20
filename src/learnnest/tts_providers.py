"""MiMo V2.5 TTS provider boundary."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from typing import Any, Protocol

from openai import OpenAI
from pydantic import SecretStr

from learnnest.note_providers import safe_provider_diagnostic

_MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
_MIMO_TTS_MODEL = "mimo-v2.5-tts"
_DEFAULT_VOICE = "冰糖"


class TtsProviderError(RuntimeError):
    """A safe synthesis failure without request text or credentials."""


class TtsProvider(Protocol):
    name: str
    model: str
    voice: str

    def synthesize(self, speech_text: str, style_instruction: str) -> bytes: ...


class MimoTtsProvider:
    name = "xiaomi-mimo"
    model = _MIMO_TTS_MODEL
    voice = _DEFAULT_VOICE

    def __init__(
        self,
        api_key: SecretStr,
        *,
        client_factory: Callable[..., Any] = OpenAI,
    ) -> None:
        secret = api_key.get_secret_value()
        if not secret.strip():
            raise TtsProviderError("MIMO_API_KEY is missing")
        self._api_key = api_key
        self._client = client_factory(
            api_key=secret,
            base_url=_MIMO_BASE_URL,
            max_retries=0,
        )

    def synthesize(self, speech_text: str, style_instruction: str) -> bytes:
        if not speech_text.strip():
            raise TtsProviderError("speech text is empty")
        if not style_instruction.strip():
            raise TtsProviderError("style instruction is empty")
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "user", "content": style_instruction},
                    {"role": "assistant", "content": speech_text},
                ],
                audio={"format": "wav", "voice": self.voice},
                stream=False,
            )
            audio = getattr(response.choices[0].message, "audio", None)
            data = (
                audio.get("data")
                if isinstance(audio, dict)
                else getattr(audio, "data", None)
            )
        except Exception as error:
            raise TtsProviderError(
                "MiMo TTS provider failed: "
                f"{safe_provider_diagnostic(error, self._api_key)}"
            ) from error
        if not isinstance(data, str) or not data.strip():
            raise TtsProviderError("MiMo returned no audio data")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as error:
            raise TtsProviderError("MiMo returned invalid Base64 audio") from error
        if not decoded:
            raise TtsProviderError("MiMo returned empty audio data")
        return decoded
