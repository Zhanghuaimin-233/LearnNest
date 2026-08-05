"""MiMo V2.5 TTS provider boundary."""

from __future__ import annotations

import base64
import binascii
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote

from openai import OpenAI
from pydantic import SecretStr

from learnnest.note_providers import safe_provider_diagnostic

_MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
_MIMO_TTS_MODEL = "mimo-v2.5-tts"
_DEFAULT_VOICE = "冰糖"
_WINDOWS_TTS_PREFIX = "local://windows-tts/"
_WINDOWS_TTS_SCRIPT = Path(__file__).with_name("windows_tts.ps1")


class TtsProviderError(RuntimeError):
    """A safe synthesis failure without request text or credentials."""


class TtsProvider(Protocol):
    name: str
    model: str
    voice: str
    billing: str

    def synthesize(self, speech_text: str, style_instruction: str) -> bytes: ...


class OpenAICompatibleTtsProvider:
    """One explicit OpenAI-compatible transport for a frozen TTS role."""

    voice = _DEFAULT_VOICE
    billing = "paid"

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
            raise TtsProviderError("TTS provider secret is unavailable")
        if not provider_name.strip() or not model.strip() or not base_url.strip():
            raise TtsProviderError("TTS provider snapshot is invalid")
        self.name = provider_name
        self.model = model
        self._api_key = api_key
        self._client = client_factory(
            api_key=secret,
            base_url=base_url,
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
                f"TTS provider failed: {safe_provider_diagnostic(error, self._api_key)}"
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


class MimoTtsProvider(OpenAICompatibleTtsProvider):
    """Compatibility wrapper for the explicit legacy MiMo TTS CLI entrypoint."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        client_factory: Callable[..., Any] = OpenAI,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise TtsProviderError("MIMO_API_KEY is missing")
        super().__init__(
            api_key,
            provider_name="xiaomi-mimo-tts",
            model=_MIMO_TTS_MODEL,
            base_url=_MIMO_BASE_URL,
            client_factory=client_factory,
        )
        self.name = "xiaomi-mimo"


@dataclass(frozen=True)
class WindowsTtsVoice:
    name: str
    culture: str


def list_windows_tts_voices() -> tuple[WindowsTtsVoice, ...]:
    """Return enabled System.Speech voices without exposing host diagnostics."""
    result = _run_windows_tts("voices")
    try:
        payload = json.loads(result.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        voices = tuple(
            WindowsTtsVoice(name=row["name"], culture=row["culture"])
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("name"), str)
            and row["name"].strip()
            and isinstance(row.get("culture"), str)
            and row["culture"].strip()
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise TtsProviderError("Windows TTS voice list is unavailable") from error
    if not voices:
        raise TtsProviderError("Windows TTS voice list is unavailable")
    return tuple(sorted(voices, key=lambda item: (item.culture, item.name)))


def default_windows_tts_voice() -> str:
    candidates = sorted(
        voice.name
        for voice in list_windows_tts_voices()
        if voice.culture.casefold() == "zh-cn"
    )
    if not candidates:
        raise TtsProviderError("Windows TTS has no enabled zh-CN voice")
    return candidates[0]


def windows_tts_endpoint(voice: str) -> str:
    if not voice.strip():
        raise ValueError("Windows TTS voice is unavailable")
    return _WINDOWS_TTS_PREFIX + quote(voice, safe="")


def windows_tts_voice_from_endpoint(endpoint: str | None) -> str | None:
    if not isinstance(endpoint, str) or not endpoint.startswith(_WINDOWS_TTS_PREFIX):
        return None
    voice = unquote(endpoint.removeprefix(_WINDOWS_TTS_PREFIX))
    return voice if voice.strip() else None


class WindowsTtsProvider:
    """PowerShell 7 System.Speech adapter that writes a WAV file, never speakers."""

    name = "windows-tts"
    model = "system-speech"
    billing = "local"

    def __init__(self, voice: str) -> None:
        if not voice.strip():
            raise TtsProviderError("Windows TTS voice is unavailable")
        self.voice = voice

    def synthesize(self, speech_text: str, style_instruction: str) -> bytes:
        if not speech_text.strip():
            raise TtsProviderError("speech text is empty")
        if not style_instruction.strip():
            raise TtsProviderError("style instruction is empty")
        try:
            with tempfile.TemporaryDirectory(prefix="learnnest-windows-tts-") as raw:
                directory = Path(raw)
                speech_path = directory / "speech.txt"
                output_path = directory / "audio.wav"
                speech_path.write_text(speech_text, encoding="utf-8")
                _run_windows_tts(
                    "synthesize",
                    speech_path=speech_path,
                    voice=self.voice,
                    output_path=output_path,
                )
                if not output_path.is_file():
                    raise OSError("output missing")
                return output_path.read_bytes()
        except TtsProviderError:
            raise
        except Exception as error:
            raise TtsProviderError("Windows TTS synthesis failed") from error


def _run_windows_tts(
    mode: str,
    *,
    speech_path: Path | None = None,
    voice: str | None = None,
    output_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("pwsh")
    if executable is None or not _WINDOWS_TTS_SCRIPT.is_file():
        raise TtsProviderError("Windows TTS is unavailable")
    arguments = [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(_WINDOWS_TTS_SCRIPT),
        "-Mode",
        mode,
    ]
    if mode == "synthesize":
        if speech_path is None or voice is None or output_path is None:
            raise TtsProviderError("Windows TTS synthesis failed")
        arguments.extend(
            [
                "-SpeechPath",
                str(speech_path),
                "-Voice",
                voice,
                "-OutputPath",
                str(output_path),
            ]
        )
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise TtsProviderError("Windows TTS is unavailable") from error
    if result.returncode != 0:
        message = (
            "Windows TTS voice list is unavailable"
            if mode == "voices"
            else "Windows TTS synthesis failed"
        )
        raise TtsProviderError(message)
    return result
