from __future__ import annotations

from pathlib import Path

import pytest

from learnnest.provider_profiles import (
    PRESETS,
    connect,
    get_connection,
    load_settings,
    set_role_binding,
)


def test_product_registry_has_only_declared_cloud_and_local_capabilities() -> None:
    assert set(PRESETS) == {
        "mimo",
        "deepseek",
        "openai",
        "kimi",
        "glm",
        "bailian",
        "ark",
        "hunyuan",
        "minimax",
        "longcat",
        "antling",
        "xai",
        "openrouter",
        "modelscope",
        "nvidia-nim",
        "anthropic",
        "gemini",
        "mimo-tts",
        "local-asr",
        "local-ocr",
    }
    assert PRESETS["mimo"].capability == "llm"
    assert PRESETS["deepseek"].capability == "llm"
    assert PRESETS["openai"].capability == "llm"
    assert PRESETS["anthropic"].capability == "llm"
    assert PRESETS["gemini"].capability == "llm"
    assert PRESETS["mimo-tts"].capability == "tts"
    assert PRESETS["local-asr"].requires_secret is False
    assert PRESETS["local-ocr"].requires_secret is False


def test_connections_are_capability_checked_and_secret_free_in_settings(
    tmp_path: Path,
) -> None:
    connection = connect(
        tmp_path, name="mimo", preset="mimo", secret_value="key", model="mimo-v2.5"
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="mimo")

    settings = load_settings(tmp_path)

    assert get_connection(tmp_path, "mimo") == connection
    assert settings.role_bindings["note_writer"].connection_id == "mimo"
    assert (
        "key"
        not in (tmp_path / ".learnnest" / "providers" / "settings.json").read_text()
    )
    with pytest.raises(ValueError, match="capability"):
        set_role_binding(tmp_path, role="tts", connection_name="mimo")


@pytest.mark.parametrize(
    "preset", ["coding-plan", "openai-compatible", "cc-switch", "siliconflow"]
)
def test_removed_product_presets_are_rejected(tmp_path: Path, preset: str) -> None:
    with pytest.raises(ValueError, match="unsupported provider"):
        connect(tmp_path, name="removed", preset=preset, secret_value="key")
