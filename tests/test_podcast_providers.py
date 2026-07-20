from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr


class FakeCompletions:
    def __init__(self, content: str = "{}", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class FakeFactory:
    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=self.completions))


def test_mimo_podcast_provider_sends_complete_schema_and_zero_retry() -> None:
    from learnnest.podcast_providers import MimoPodcastProvider

    completions = FakeCompletions('{"schema_version":"1.0"}')
    factory = FakeFactory(completions)
    provider = MimoPodcastProvider(
        SecretStr("test-secret-not-real"),
        client_factory=factory,
    )

    raw = provider.generate('{"content_pack":{},"note":{}}', ())

    assert raw == '{"schema_version":"1.0"}'
    assert provider.name == "xiaomi-mimo"
    assert provider.model == "mimo-v2.5"
    assert factory.calls[0]["max_retries"] == 0
    call = completions.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["stream"] is False
    messages = call["messages"]
    system = messages[0]["content"]
    for field in (
        '"note_content_sha256"',
        '"segments"',
        '"order"',
        '"kind"',
        '"text"',
        '"evidence_ids"',
        '"ai_supplements"',
    ):
        assert field in system
    assert messages[1]["content"] == '{"content_pack":{},"note":{}}'


def test_mimo_podcast_provider_sends_only_validator_feedback_on_retry() -> None:
    from learnnest.podcast_providers import MimoPodcastProvider

    completions = FakeCompletions()
    provider = MimoPodcastProvider(
        SecretStr("test-secret-not-real"),
        client_factory=FakeFactory(completions),
    )

    provider.generate("source-context", ("segments.1.text: invalid",))

    messages = completions.calls[0]["messages"]
    assert messages[1]["content"] == "source-context"
    assert messages[2]["role"] == "user"
    assert "segments.1.text" in messages[2]["content"]
    assert "source-context" not in messages[2]["content"]


def test_mimo_podcast_provider_rejects_blank_key_and_empty_content() -> None:
    from learnnest.podcast_providers import MimoPodcastProvider, PodcastProviderError

    factory = FakeFactory(FakeCompletions())
    with pytest.raises(PodcastProviderError, match="MIMO_API_KEY is missing"):
        MimoPodcastProvider(SecretStr(" "), client_factory=factory)
    assert factory.calls == []

    provider = MimoPodcastProvider(
        SecretStr("test-secret-not-real"),
        client_factory=FakeFactory(FakeCompletions(" ")),
    )
    with pytest.raises(PodcastProviderError, match="no message content"):
        provider.generate("{}", ())


def test_mimo_podcast_provider_error_never_exposes_secret() -> None:
    from learnnest.podcast_providers import MimoPodcastProvider, PodcastProviderError

    secret = "test-secret-not-real"
    provider = MimoPodcastProvider(
        SecretStr(secret),
        client_factory=FakeFactory(
            FakeCompletions(error=RuntimeError(f"transport leaked {secret}"))
        ),
    )

    with pytest.raises(PodcastProviderError) as captured:
        provider.generate("{}", ())

    assert secret not in str(captured.value)
    assert "RuntimeError" in str(captured.value)
