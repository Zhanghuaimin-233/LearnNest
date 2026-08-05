from __future__ import annotations

import base64
import json
import subprocess
from types import SimpleNamespace

import pytest
from pydantic import SecretStr


class FakeCompletions:
    def __init__(self, audio_data: str | None = None, error: Exception | None = None):
        self.audio_data = audio_data
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        audio = (
            SimpleNamespace(data=self.audio_data)
            if self.audio_data is not None
            else None
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(audio=audio))]
        )


class FakeFactory:
    def __init__(self, completions: FakeCompletions):
        self.completions = completions
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=self.completions))


def test_mimo_tts_provider_uses_official_nonstreaming_wav_contract() -> None:
    from learnnest.tts_providers import MimoTtsProvider

    wav = b"RIFFfakeWAVEdata"
    completions = FakeCompletions(base64.b64encode(wav).decode())
    factory = FakeFactory(completions)
    provider = MimoTtsProvider(
        SecretStr("test-secret-not-real"),
        client_factory=factory,
    )

    result = provider.synthesize("今天复习模型设置。", "温和、清晰、语速适中。")

    assert result == wav
    assert provider.name == "xiaomi-mimo"
    assert provider.model == "mimo-v2.5-tts"
    assert provider.voice == "冰糖"
    assert factory.calls[0]["max_retries"] == 0
    call = completions.calls[0]
    assert call["model"] == "mimo-v2.5-tts"
    assert call["messages"] == [
        {"role": "user", "content": "温和、清晰、语速适中。"},
        {"role": "assistant", "content": "今天复习模型设置。"},
    ]
    assert call["audio"] == {"format": "wav", "voice": "冰糖"}
    assert call["stream"] is False


def test_mimo_tts_provider_rejects_missing_audio_and_invalid_base64() -> None:
    from learnnest.tts_providers import MimoTtsProvider, TtsProviderError

    missing = MimoTtsProvider(
        SecretStr("test-secret-not-real"),
        client_factory=FakeFactory(FakeCompletions()),
    )
    invalid = MimoTtsProvider(
        SecretStr("test-secret-not-real"),
        client_factory=FakeFactory(FakeCompletions("not-base64***")),
    )

    with pytest.raises(TtsProviderError, match="no audio data"):
        missing.synthesize("正文", "平静")
    with pytest.raises(TtsProviderError, match="invalid Base64"):
        invalid.synthesize("正文", "平静")


def test_mimo_tts_provider_rejects_blank_key_and_scrubs_errors() -> None:
    from learnnest.tts_providers import MimoTtsProvider, TtsProviderError

    factory = FakeFactory(FakeCompletions())
    with pytest.raises(TtsProviderError, match="MIMO_API_KEY is missing"):
        MimoTtsProvider(SecretStr(" "), client_factory=factory)
    assert factory.calls == []

    secret = "test-secret-not-real"
    provider = MimoTtsProvider(
        SecretStr(secret),
        client_factory=FakeFactory(
            FakeCompletions(error=RuntimeError(f"transport leaked {secret}"))
        ),
    )
    with pytest.raises(TtsProviderError) as captured:
        provider.synthesize("正文", "平静")
    assert secret not in str(captured.value)
    assert "RuntimeError" in str(captured.value)


def test_windows_tts_uses_file_arguments_and_selects_a_stable_zh_cn_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import learnnest.tts_providers as providers

    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "-Mode" in args and args[args.index("-Mode") + 1] == "voices":
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {"name": "Zulu English", "culture": "en-US"},
                        {"name": "Huihui Desktop", "culture": "zh-CN"},
                        {"name": "Ava Chinese", "culture": "zh-CN"},
                    ]
                ),
                "",
            )
        output = args[args.index("-OutputPath") + 1]
        with open(output, "wb") as stream:
            stream.write(b"RIFFfakeWAVEdata")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)

    assert providers.default_windows_tts_voice() == "Ava Chinese"
    provider = providers.WindowsTtsProvider("Ava Chinese")
    assert provider.synthesize('恶意 " 字符; $(whoami)', "自然") == b"RIFFfakeWAVEdata"

    synth_call = calls[-1]
    assert "-Command" not in synth_call
    assert "-File" in synth_call
    assert synth_call[synth_call.index("-Voice") + 1] == "Ava Chinese"
    assert "恶意" not in " ".join(synth_call)


def test_windows_tts_failure_is_sanitized_and_never_returns_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import learnnest.tts_providers as providers

    monkeypatch.setattr(
        providers.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 1, "", "untrusted C:\\private\\speech.txt"
        ),
    )
    with pytest.raises(
        providers.TtsProviderError, match="Windows TTS synthesis failed"
    ) as error:
        providers.WindowsTtsProvider("Huihui Desktop").synthesize("正文", "自然")
    assert "private" not in str(error.value)
