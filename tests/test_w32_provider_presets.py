"""W3.2 official Provider preset, catalog, atomic-create, and transport contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from learnnest.cli import app
from learnnest.note_providers import NoteProviderError
from learnnest.podcast_providers import PodcastProviderError
from learnnest.provider_model_catalog import (
    ModelCatalogPreview,
    ProviderModelCatalogError,
    fetch_model_catalog,
    preview_model_catalog,
)
from learnnest.provider_profiles import (
    PRESETS,
    ProviderRoleSnapshot,
    connect,
    connection_presets,
    freeze_role_bindings,
    load_settings,
    set_role_binding,
    settings_sha256,
    update_connection_model,
)
from learnnest.provider_service import (
    assisted_provider_from_snapshot,
    podcast_provider_from_snapshot,
)
from learnnest.web_app import create_web_app
import learnnest.cli as cli
import learnnest.web_app as web_app


# --------------------------------------------------------------------------- #
# Registry contract: the 15 new W3.2 presets plus the two baselines.
# --------------------------------------------------------------------------- #

W32_PRESET_CONTRACT: dict[str, dict[str, object]] = {
    "openai": {
        "provider": "openai",
        "api_family": "openai_responses",
        "endpoint": "https://api.openai.com/v1",
        "catalog_mode": "live",
        "catalog_endpoint": "https://api.openai.com/v1/models",
        "allowed_models": (
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.2",
            "gpt-5.1",
            "gpt-5",
        ),
        "display_name": "OpenAI",
        "key_entry_url": "https://platform.openai.com/api-keys",
    },
    "kimi": {
        "provider": "moonshot-kimi",
        "api_family": "openai_chat",
        "endpoint": "https://api.moonshot.cn/v1",
        "catalog_mode": "live",
        "catalog_endpoint": "https://api.moonshot.cn/v1/models",
        "allowed_models": (
            "kimi-k3",
            "kimi-k2.7-code",
            "kimi-k2.7-code-highspeed",
            "kimi-k2.6",
        ),
        "display_name": "Kimi",
        "key_entry_url": "https://platform.kimi.com/console/api-keys",
    },
    "glm": {
        "provider": "zhipu-glm",
        "api_family": "openai_chat",
        "endpoint": "https://open.bigmodel.cn/api/paas/v4",
        "catalog_mode": "curated",
        "catalog_endpoint": None,
        "allowed_models": (
            "glm-5.3",
            "glm-5.3-flash",
            "glm-5.2",
            "glm-5.1",
            "glm-5",
            "glm-4.7",
            "glm-4.6",
        ),
        "display_name": "智谱 GLM",
        "key_entry_url": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
    },
    "bailian": {
        "provider": "alibaba-bailian",
        "api_family": "openai_chat",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "catalog_mode": "curated",
        "catalog_endpoint": None,
        "allowed_models": (
            "qwen3.8-max",
            "qwen3.7-plus",
            "qwen3.8-flash",
            "qwen3-max",
            "qwen-plus",
            "qwen3-coder-plus",
        ),
        "display_name": "阿里云百炼",
        "key_entry_url": "https://bailian.console.aliyun.com",
    },
    "ark": {
        "provider": "volcengine-ark",
        "api_family": "openai_chat",
        "endpoint": "https://ark.cn-beijing.volces.com/api/v3",
        "catalog_mode": "curated",
        "catalog_endpoint": None,
        "allowed_models": (
            "doubao-seed-evolving",
            "doubao-seed-2-1-pro-260628",
            "doubao-seed-2-1-turbo-260628",
            "doubao-seed-2-0-pro-260215",
            "doubao-seed-2-0-lite-260428",
        ),
        "display_name": "火山方舟（豆包）",
        "key_entry_url": "https://console.volcengine.com/ark",
    },
    "hunyuan": {
        "provider": "tencent-hunyuan",
        "api_family": "openai_chat",
        "endpoint": "https://tokenhub.tencentmaas.com/v1",
        "catalog_mode": "live",
        "catalog_endpoint": "https://tokenhub.tencentmaas.com/v1/models",
        "allowed_models": ("hy4-preview", "hy3"),
        "display_name": "腾讯混元（TokenHub）",
        "key_entry_url": "https://console.cloud.tencent.com/tokenhub/apikey",
    },
    "minimax": {
        "provider": "minimax",
        "api_family": "openai_chat",
        "endpoint": "https://api.minimaxi.com/v1",
        "catalog_mode": "live",
        "catalog_endpoint": "https://api.minimaxi.com/v1/models",
        "allowed_models": (
            "MiniMax-M3",
            "MiniMax-M2.7",
            "MiniMax-M2.5",
            "MiniMax-M2.1",
        ),
        "display_name": "MiniMax",
        "key_entry_url": "https://platform.minimaxi.com",
    },
    "longcat": {
        "provider": "meituan-longcat",
        "api_family": "openai_chat",
        "endpoint": "https://api.longcat.chat/openai/v1",
        "catalog_mode": "live",
        "catalog_endpoint": "https://api.longcat.chat/openai/v1/models",
        "allowed_models": ("LongCat-2.0",),
        "display_name": "美团 LongCat",
        "key_entry_url": "https://longcat.chat/platform/api_keys",
    },
    "antling": {
        "provider": "ant-ling",
        "api_family": "openai_chat",
        "endpoint": "https://api.ant-ling.com/v1",
        "catalog_mode": "curated",
        "catalog_endpoint": None,
        "allowed_models": (
            "Ling-3.0-flash",
            "Ling-2.6-1T",
            "Ling-2.6-flash",
            "Ring-2.6-1T",
        ),
        "display_name": "蚂蚁百灵",
        "key_entry_url": "https://chat.ant-ling.com/ope",
    },
    "xai": {
        "provider": "xai",
        "api_family": "openai_chat",
        "endpoint": "https://api.x.ai/v1",
        "catalog_mode": "live",
        "catalog_endpoint": "https://api.x.ai/v1/models",
        "allowed_models": ("grok-4.6", "grok-4.5", "grok-4.3", "grok-4.3-latest"),
        "display_name": "xAI Grok",
        "key_entry_url": "https://console.x.ai",
    },
    "openrouter": {
        "provider": "openrouter",
        "api_family": "openai_chat",
        "endpoint": "https://openrouter.ai/api/v1",
        "catalog_mode": "live",
        "catalog_endpoint": "https://openrouter.ai/api/v1/models",
        "allowed_models": (
            "openai/gpt-5.6-luna",
            "anthropic/claude-sonnet-4.5",
            "google/gemini-2.5-pro",
            "deepseek/deepseek-v4-flash-0731",
            "z-ai/glm-5.3",
            "qwen/qwen3.8-27b",
            "xiaomi/mimo-v2.5",
            "tencent/hy3",
        ),
        "display_name": "OpenRouter（多模型平台）",
        "key_entry_url": "https://openrouter.ai/keys",
    },
    "modelscope": {
        "provider": "modelscope",
        "api_family": "openai_chat",
        "endpoint": "https://api-inference.modelscope.cn/v1",
        "catalog_mode": "live",
        "catalog_endpoint": "https://api-inference.modelscope.cn/v1/models",
        "allowed_models": (
            "Qwen/Qwen3-235B-A22B",
            "deepseek-ai/DeepSeek-V4-Pro",
            "ZhipuAI/GLM-5.2",
            "MiniMax/MiniMax-M3",
        ),
        "display_name": "ModelScope（多模型平台）",
        "key_entry_url": "https://modelscope.cn/my/myaccesstoken",
    },
    "nvidia-nim": {
        "provider": "nvidia-nim",
        "api_family": "openai_chat",
        "endpoint": "https://integrate.api.nvidia.com/v1",
        "catalog_mode": "live",
        "catalog_endpoint": "https://integrate.api.nvidia.com/v1/models",
        "allowed_models": (
            "meta/llama-3.3-70b-instruct",
            "deepseek-ai/deepseek-r1",
            "qwen/qwq-32b",
            "openai/gpt-oss-120b",
        ),
        "display_name": "NVIDIA NIM（多模型平台）",
        "key_entry_url": "https://build.nvidia.com",
    },
    "anthropic": {
        "provider": "anthropic",
        "api_family": "anthropic",
        "endpoint": "https://api.anthropic.com",
        "catalog_mode": "live",
        "catalog_endpoint": "https://api.anthropic.com/v1/models",
        "allowed_models": (
            "claude-fable-5",
            "claude-opus-5",
            "claude-sonnet-5",
            "claude-haiku-4-5",
            "claude-sonnet-4-5",
            "claude-opus-4-6",
        ),
        "display_name": "Anthropic Claude",
        "key_entry_url": "https://platform.claude.com/settings/keys",
    },
    "gemini": {
        "provider": "google-gemini",
        "api_family": "gemini",
        "endpoint": "https://generativelanguage.googleapis.com",
        "catalog_mode": "live",
        "catalog_endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
        "allowed_models": (
            "gemini-3.1-pro-preview",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ),
        "display_name": "Google Gemini",
        "key_entry_url": "https://aistudio.google.com/apikey",
    },
}


@pytest.mark.parametrize("preset_key", sorted(W32_PRESET_CONTRACT))
def test_w32_preset_identity_is_locked(preset_key: str) -> None:
    expected = W32_PRESET_CONTRACT[preset_key]
    presets = connection_presets()
    assert preset_key in presets
    preset = presets[preset_key]
    assert preset.capability == "llm"
    assert preset.provider == expected["provider"]
    assert preset.api_family == expected["api_family"]
    assert preset.endpoint == expected["endpoint"]
    assert preset.catalog_mode == expected["catalog_mode"]
    assert preset.catalog_endpoint == expected["catalog_endpoint"]
    assert tuple(preset.allowed_models) == tuple(expected["allowed_models"])  # type: ignore[arg-type]
    assert preset.display_name == expected["display_name"]
    assert preset.key_entry_url == expected["key_entry_url"]
    assert preset.default_model is None
    assert preset.requires_secret is True
    assert preset.adapter_revision == "1"


def test_w32_registry_is_complete_unique_and_llm_only_catalogs() -> None:
    llm_presets = {
        key: preset for key, preset in PRESETS.items() if preset.capability == "llm"
    }
    assert set(llm_presets) == set(W32_PRESET_CONTRACT) | {"mimo", "deepseek"}
    providers = [preset.provider for preset in PRESETS.values()]
    assert len(providers) == len(set(providers))
    for preset in PRESETS.values():
        if preset.capability == "llm":
            assert preset.catalog_mode in {"live", "curated"}
            assert preset.allowed_models
            assert preset.default_model is None
            if preset.catalog_mode == "live":
                assert preset.catalog_endpoint is not None
                assert preset.catalog_endpoint.startswith("https://")
                assert preset.catalog_endpoint.endswith("/models")
            else:
                assert preset.catalog_endpoint is None
        else:
            assert preset.catalog_mode is None
            assert preset.catalog_endpoint is None
    assert PRESETS["mimo"].catalog_mode == "live"
    assert PRESETS["deepseek"].catalog_mode == "live"
    assert PRESETS["mimo"].key_entry_url is not None
    assert PRESETS["deepseek"].key_entry_url is not None


def test_w32_platform_presets_do_not_disguise_as_model_vendors() -> None:
    for key in ("openrouter", "modelscope", "nvidia-nim"):
        assert "平台" in connection_presets()[key].display_name
    for key in (
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
        "anthropic",
        "gemini",
    ):
        assert "平台" not in connection_presets()[key].display_name


# --------------------------------------------------------------------------- #
# Shared fakes
# --------------------------------------------------------------------------- #


class _Model:
    def __init__(self, model_id: str, owned_by: str | None = None) -> None:
        self.id = model_id
        self.owned_by = owned_by


class _ModelPage:
    def __init__(self, data: object) -> None:
        self.data = data


class _Models:
    def __init__(self, response: object) -> None:
        self.response = response

    def list(self) -> object:
        return self.response


class _SdkClient:
    def __init__(self, response: object) -> None:
        self.models = _Models(response)


def _sdk_factory(response: object, calls: list[dict[str, object]]):
    def create(**kwargs: object) -> _SdkClient:
        calls.append(kwargs)
        return _SdkClient(response)

    return create


class _HttpResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _HttpClient:
    def __init__(self, response: _HttpResponse, calls: list[dict[str, object]]) -> None:
        self._response = response
        self._calls = calls

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | None = None,
    ) -> _HttpResponse:
        self._calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": dict(headers or {}),
                "params": dict(params or {}),
            }
        )
        return self._response

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: object | None = None,
    ) -> _HttpResponse:
        self._calls.append(
            {"method": "POST", "url": url, "headers": dict(headers or {}), "json": json}
        )
        return self._response

    def __enter__(self) -> "_HttpClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _http_factory(response: _HttpResponse, calls: list[dict[str, object]]):
    def create(**kwargs: object) -> _HttpClient:
        calls.append({"__init__": kwargs})
        return _HttpClient(response, calls)

    return create


def _forbidden_factories() -> tuple[object, object]:
    def _fail(**_kwargs: object) -> object:
        raise AssertionError("curated catalog must not touch the network")

    return _fail, _fail


# --------------------------------------------------------------------------- #
# Catalog preview: live/curated per family, no persistence, no fallback.
# --------------------------------------------------------------------------- #


def test_preview_live_catalog_for_openai_chat_family_uses_temp_key_once(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    response = _ModelPage(
        [
            _Model("kimi-k3", "moonshot"),
            _Model("kimi-k2.6"),
            _Model("unrelated-model", "other"),
        ]
    )

    preview = preview_model_catalog(
        "kimi", "temp-key", client_factory=_sdk_factory(response, calls)
    )

    assert isinstance(preview, ModelCatalogPreview)
    assert preview.source == "live"
    assert preview.models == [
        {"id": "kimi-k2.6"},
        {"id": "kimi-k3", "owned_by": "moonshot"},
    ]
    assert preview.adapter_revision == "1"
    assert "实时" in preview.note
    assert calls == [
        {
            "api_key": "temp-key",
            "base_url": "https://api.moonshot.cn/v1",
            "max_retries": 0,
            "timeout": 10.0,
        }
    ]
    assert not list(tmp_path.rglob("*"))


def test_preview_live_catalog_for_openai_responses_family(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    response = _ModelPage(
        [
            _Model("gpt-5.2"),
            _Model("gpt-5.6-sol", "openai"),
            _Model("text-embedding-3-large"),
        ]
    )

    preview = preview_model_catalog(
        "openai", "temp-key", client_factory=_sdk_factory(response, calls)
    )

    assert preview.source == "live"
    assert [item["id"] for item in preview.models] == ["gpt-5.2", "gpt-5.6-sol"]
    assert calls[0]["base_url"] == "https://api.openai.com/v1"
    assert calls[0]["max_retries"] == 0


def test_preview_live_catalog_for_anthropic_family(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    response = _HttpResponse(
        200,
        {
            "data": [
                {"id": "claude-fable-5", "display_name": "Claude Fable 5"},
                {"id": "claude-sonnet-5"},
                {"id": "claude-unknown-new"},
            ],
            "has_more": False,
        },
    )

    preview = preview_model_catalog(
        "anthropic", "temp-key", http_client_factory=_http_factory(response, calls)
    )

    assert preview.source == "live"
    assert [item["id"] for item in preview.models] == [
        "claude-fable-5",
        "claude-sonnet-5",
    ]
    request = calls[1]
    assert request["method"] == "GET"
    assert request["url"] == "https://api.anthropic.com/v1/models"
    assert request["headers"] == {
        "x-api-key": "temp-key",
        "anthropic-version": "2023-06-01",
    }
    assert request["params"] == {"limit": 1000}
    assert calls[0]["__init__"] == {"timeout": 10.0}


def test_preview_live_catalog_for_gemini_family(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    response = _HttpResponse(
        200,
        {
            "models": [
                {"name": "models/gemini-3.6-flash", "displayName": "Gemini 3.6 Flash"},
                {"name": "models/gemini-2.5-pro"},
                {"name": "models/text-embedding-004"},
            ]
        },
    )

    preview = preview_model_catalog(
        "gemini", "temp-key", http_client_factory=_http_factory(response, calls)
    )

    assert preview.source == "live"
    assert [item["id"] for item in preview.models] == [
        "gemini-2.5-pro",
        "gemini-3.6-flash",
    ]
    request = calls[1]
    assert request["url"] == "https://generativelanguage.googleapis.com/v1beta/models"
    assert request["headers"] == {"x-goog-api-key": "temp-key"}
    assert request["params"] == {"pageSize": 1000}


@pytest.mark.parametrize("preset_key", ["glm", "bailian", "ark", "antling"])
def test_preview_curated_catalog_never_calls_the_network(preset_key: str) -> None:
    client_factory, http_factory = _forbidden_factories()

    preview = preview_model_catalog(
        preset_key,
        "temp-key",
        client_factory=client_factory,
        http_client_factory=http_factory,
    )

    expected = sorted(W32_PRESET_CONTRACT[preset_key]["allowed_models"])  # type: ignore[arg-type]
    assert preview.source == "curated"
    assert preview.models == [{"id": model_id} for model_id in expected]
    assert preview.adapter_revision == "1"
    assert "不代表" in preview.note
    assert "已开通" in preview.note


def test_preview_live_failure_never_falls_back_to_curated() -> None:
    class _AuthModels:
        def list(self) -> object:
            failure = type("AuthFailure", (Exception,), {"status_code": 401})
            raise failure("raw body with secret sk-live-1234567890")

    class _AuthClient:
        models = _AuthModels()

    with pytest.raises(ProviderModelCatalogError) as error:
        preview_model_catalog(
            "kimi", "sk-live-1234567890", client_factory=lambda **_kw: _AuthClient()
        )

    assert error.value.code == "authentication"
    assert "sk-live-1234567890" not in str(error.value)


def test_preview_rejects_unknown_preset_and_missing_key() -> None:
    with pytest.raises(ProviderModelCatalogError) as unknown:
        preview_model_catalog("not-a-preset", "key")
    assert unknown.value.code == "unsupported_preset"

    with pytest.raises(ProviderModelCatalogError) as missing:
        preview_model_catalog("kimi", "  ")
    assert missing.value.code == "missing_secret"

    with pytest.raises(ProviderModelCatalogError) as not_llm:
        preview_model_catalog("mimo-tts", "key")
    assert not_llm.value.code == "unsupported_preset"


def test_preview_errors_are_classified_and_sanitized(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    response = _HttpResponse(401, {"error": {"message": "bad key sk-live-1234567890"}})

    with pytest.raises(ProviderModelCatalogError) as error:
        preview_model_catalog(
            "anthropic",
            "sk-live-1234567890",
            http_client_factory=_http_factory(response, calls),
        )

    assert error.value.code == "authentication"
    assert "sk-live-1234567890" not in str(error.value)
    assert len(calls) == 2  # one client construction, one request, zero retries


# --------------------------------------------------------------------------- #
# Atomic create: explicit model required, failure leaves no residue.
# --------------------------------------------------------------------------- #


def test_connect_refuses_new_llm_preset_without_explicit_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="model"):
        connect(tmp_path, name="w32", preset="openai", secret_value="sk-test")

    assert load_settings(tmp_path).connections == {}
    assert not (tmp_path / ".learnnest").exists()


@pytest.mark.parametrize("preset_key", ["mimo", "deepseek"])
def test_baseline_llm_presets_have_no_default_model_bypass(
    preset_key: str, tmp_path: Path
) -> None:
    preset = connection_presets()[preset_key]
    assert preset.default_model is None
    assert preset.allowed_models
    assert preset.catalog_mode == "live"

    with pytest.raises(ValueError, match="model"):
        connect(tmp_path, name="baseline", preset=preset_key, secret_value="sk-test")

    assert load_settings(tmp_path).connections == {}
    assert not (tmp_path / ".learnnest").exists()

    saved = connect(
        tmp_path,
        name="baseline",
        preset=preset_key,
        secret_value="sk-test",
        model=preset.allowed_models[0],
    )
    assert saved.model == preset.allowed_models[0]


def test_connect_refuses_unsupported_model_and_endpoint_override(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="model"):
        connect(
            tmp_path,
            name="w32",
            preset="anthropic",
            secret_value="sk",
            model="claude-unknown",
        )

    with pytest.raises(ValueError, match="endpoint"):
        connect(
            tmp_path,
            name="w32",
            preset="anthropic",
            secret_value="sk",
            model="claude-sonnet-5",
            endpoint="https://evil.example/v1",
        )

    assert load_settings(tmp_path).connections == {}


def test_connect_creates_new_family_connection_atomically(tmp_path: Path) -> None:
    connection = connect(
        tmp_path,
        name="w32-anthropic",
        preset="anthropic",
        secret_value="sk-anthropic",
        model="claude-sonnet-5",
    )

    assert connection.api_family == "anthropic"
    assert connection.model == "claude-sonnet-5"
    assert connection.endpoint == "https://api.anthropic.com"
    settings = load_settings(tmp_path)
    assert settings.connections["w32-anthropic"].secret_id == connection.secret_id
    secret_files = list((tmp_path / ".learnnest" / "providers").glob("*.json"))
    assert len(secret_files) == 1
    stored = secret_files[0].read_text(encoding="utf-8")
    assert "sk-anthropic" not in stored


def test_connect_rollback_keeps_settings_and_secret_on_disk_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect(
        tmp_path,
        name="baseline",
        preset="mimo",
        secret_value="keep-me",
        model="mimo-v2.5",
    )
    before_settings = (
        tmp_path / ".learnnest" / "providers" / "settings.json"
    ).read_text(encoding="utf-8")
    before_files = sorted(
        path.name for path in (tmp_path / ".learnnest" / "providers").glob("*")
    )

    import learnnest.provider_profiles as provider_profiles

    def _fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(provider_profiles, "save_settings", _fail_save)
    with pytest.raises(OSError):
        connect(
            tmp_path, name="w32", preset="kimi", secret_value="sk-kimi", model="kimi-k3"
        )
    monkeypatch.undo()

    after_files = sorted(
        path.name for path in (tmp_path / ".learnnest" / "providers").glob("*")
    )
    assert after_files == before_files
    assert (tmp_path / ".learnnest" / "providers" / "settings.json").read_text(
        encoding="utf-8"
    ) == before_settings


def test_update_connection_model_enforces_allowlist_and_keeps_frozen_task(
    tmp_path: Path,
) -> None:
    connect(
        tmp_path,
        name="w32",
        preset="gemini",
        secret_value="sk",
        model="gemini-2.5-flash",
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="w32")
    before = load_settings(tmp_path)
    before_sha = settings_sha256(before)
    frozen = freeze_role_bindings(tmp_path)

    with pytest.raises(ValueError, match="model"):
        update_connection_model(tmp_path, name="w32", model="gemini-unknown")

    updated = update_connection_model(tmp_path, name="w32", model="gemini-3.6-flash")
    current = load_settings(tmp_path)

    assert updated.model == "gemini-3.6-flash"
    assert updated.secret_id == before.connections["w32"].secret_id
    assert settings_sha256(current) != before_sha
    assert frozen["note_writer"].model == "gemini-2.5-flash"
    assert frozen["note_writer"].secret_id == before.connections["w32"].secret_id


def test_saved_connection_catalog_supports_curated_and_live_dispatch(
    tmp_path: Path,
) -> None:
    connect(
        tmp_path, name="w32-glm", preset="glm", secret_value="sk-glm", model="glm-5.3"
    )
    client_factory, http_factory = _forbidden_factories()

    curated = fetch_model_catalog(
        tmp_path,
        "w32-glm",
        client_factory=client_factory,
        http_client_factory=http_factory,
    )
    assert curated == [
        {"id": model_id}
        for model_id in sorted(W32_PRESET_CONTRACT["glm"]["allowed_models"])
    ]  # type: ignore[arg-type]

    connect(
        tmp_path,
        name="w32-anthropic",
        preset="anthropic",
        secret_value="sk",
        model="claude-sonnet-5",
    )
    calls: list[dict[str, object]] = []
    response = _HttpResponse(200, {"data": [{"id": "claude-sonnet-5"}]})

    live = fetch_model_catalog(
        tmp_path,
        "w32-anthropic",
        client_factory=client_factory,
        http_client_factory=_http_factory(response, calls),
    )
    assert live == [{"id": "claude-sonnet-5"}]
    assert calls[-1]["url"] == "https://api.anthropic.com/v1/models"
    assert calls[-1]["headers"]["x-api-key"] == "sk"


# --------------------------------------------------------------------------- #
# Native transports: Writer, Reviewer, and Podcast paths per api family.
# --------------------------------------------------------------------------- #


def _bind_and_freeze(
    tmp_path: Path, *, note_connection: str, podcast_connection: str | None = None
):
    set_role_binding(tmp_path, role="note_writer", connection_name=note_connection)
    set_role_binding(tmp_path, role="note_reviewer", connection_name=note_connection)
    if podcast_connection is not None:
        set_role_binding(tmp_path, role="podcast", connection_name=podcast_connection)
    return freeze_role_bindings(tmp_path)


_DOSSIER = json.dumps({"title": "测试材料", "facts": ["事实一"]}, ensure_ascii=False)


def test_openai_responses_family_transport_writer_reviewer_podcast(
    tmp_path: Path,
) -> None:
    connect(
        tmp_path,
        name="w32-note",
        preset="openai",
        secret_value="sk-openai",
        model="gpt-5.2",
    )
    connect(
        tmp_path,
        name="w32-podcast",
        preset="openai",
        secret_value="sk-openai",
        model="gpt-5.2",
    )
    frozen = _bind_and_freeze(
        tmp_path, note_connection="w32-note", podcast_connection="w32-podcast"
    )

    text_response = _HttpResponse(
        200,
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "# 笔记"}],
                }
            ]
        },
    )
    calls: list[dict[str, object]] = []
    writer = assisted_provider_from_snapshot(
        str(tmp_path),
        frozen["note_writer"],
        http_client_factory=_http_factory(text_response, calls),
    )
    markdown = writer.write_markdown(_DOSSIER)

    assert markdown == "# 笔记"
    assert writer.name == "openai"
    assert writer.model == "gpt-5.2"
    request = calls[-1]
    assert request["method"] == "POST"
    assert request["url"] == "https://api.openai.com/v1/responses"
    assert request["headers"]["Authorization"] == "Bearer sk-openai"
    payload = request["json"]
    assert payload["model"] == "gpt-5.2"
    assert payload["stream"] is False
    assert [message["role"] for message in payload["input"]] == ["system", "user"]

    reviewer = assisted_provider_from_snapshot(
        str(tmp_path),
        frozen["note_reviewer"],
        http_client_factory=_http_factory(text_response, calls),
    )
    assert reviewer.review_markdown(_DOSSIER, "# 候选笔记") == "# 笔记"
    assert calls[-1]["json"]["input"][-1]["role"] == "user"

    json_response = _HttpResponse(
        200,
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"segments": []}'}],
                }
            ]
        },
    )
    json_calls: list[dict[str, object]] = []
    podcast = podcast_provider_from_snapshot(
        str(tmp_path),
        frozen["podcast"],
        http_client_factory=_http_factory(json_response, json_calls),
    )
    script = podcast.generate(_DOSSIER, ())

    assert script == '{"segments": []}'
    podcast_payload = json_calls[-1]["json"]
    assert podcast_payload["model"] == "gpt-5.2"
    assert podcast_payload["text"] == {"format": {"type": "json_object"}}
    assert [message["role"] for message in podcast_payload["input"]] == [
        "system",
        "user",
    ]


def test_anthropic_family_transport_writer_reviewer_podcast(tmp_path: Path) -> None:
    connect(
        tmp_path,
        name="w32-note",
        preset="anthropic",
        secret_value="sk-anthropic",
        model="claude-sonnet-5",
    )
    connect(
        tmp_path,
        name="w32-podcast",
        preset="anthropic",
        secret_value="sk-anthropic",
        model="claude-sonnet-5",
    )
    frozen = _bind_and_freeze(
        tmp_path, note_connection="w32-note", podcast_connection="w32-podcast"
    )

    text_response = _HttpResponse(
        200,
        {
            "content": [{"type": "text", "text": "# Claude 笔记"}],
            "stop_reason": "end_turn",
        },
    )
    calls: list[dict[str, object]] = []
    writer = assisted_provider_from_snapshot(
        str(tmp_path),
        frozen["note_writer"],
        http_client_factory=_http_factory(text_response, calls),
    )
    assert writer.write_markdown(_DOSSIER) == "# Claude 笔记"

    request = calls[-1]
    assert request["url"] == "https://api.anthropic.com/v1/messages"
    assert request["headers"]["x-api-key"] == "sk-anthropic"
    assert request["headers"]["anthropic-version"] == "2023-06-01"
    payload = request["json"]
    assert payload["model"] == "claude-sonnet-5"
    assert isinstance(payload["max_tokens"], int) and payload["max_tokens"] > 0
    assert isinstance(payload["system"], str) and payload["system"]
    assert [message["role"] for message in payload["messages"]] == ["user"]
    assert "tools" not in payload

    reviewer = assisted_provider_from_snapshot(
        str(tmp_path),
        frozen["note_reviewer"],
        http_client_factory=_http_factory(text_response, calls),
    )
    assert reviewer.review_markdown(_DOSSIER, "# 候选") == "# Claude 笔记"
    assert calls[-1]["json"]["messages"][0]["role"] == "user"

    tool_response = _HttpResponse(
        200,
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "podcast_script",
                    "input": {"schema_version": "1.0"},
                }
            ]
        },
    )
    json_calls: list[dict[str, object]] = []
    podcast = podcast_provider_from_snapshot(
        str(tmp_path),
        frozen["podcast"],
        http_client_factory=_http_factory(tool_response, json_calls),
    )
    script = podcast.generate(_DOSSIER, ())

    assert json.loads(script) == {"schema_version": "1.0"}
    podcast_payload = json_calls[-1]["json"]
    tool = podcast_payload["tools"][0]
    assert tool["name"] == podcast_payload["tool_choice"]["name"]
    assert podcast_payload["tool_choice"] == {"type": "tool", "name": tool["name"]}
    assert isinstance(tool["input_schema"], dict) and tool["input_schema"]
    assert podcast_payload["model"] == "claude-sonnet-5"


def test_gemini_family_transport_writer_reviewer_podcast(tmp_path: Path) -> None:
    connect(
        tmp_path,
        name="w32-note",
        preset="gemini",
        secret_value="sk-gemini",
        model="gemini-3.6-flash",
    )
    connect(
        tmp_path,
        name="w32-podcast",
        preset="gemini",
        secret_value="sk-gemini",
        model="gemini-3.6-flash",
    )
    frozen = _bind_and_freeze(
        tmp_path, note_connection="w32-note", podcast_connection="w32-podcast"
    )

    text_response = _HttpResponse(
        200,
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": "# Gemini 笔记"}], "role": "model"},
                    "finishReason": "STOP",
                }
            ]
        },
    )
    calls: list[dict[str, object]] = []
    writer = assisted_provider_from_snapshot(
        str(tmp_path),
        frozen["note_writer"],
        http_client_factory=_http_factory(text_response, calls),
    )
    assert writer.write_markdown(_DOSSIER) == "# Gemini 笔记"

    request = calls[-1]
    assert request["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
    )
    assert request["headers"]["x-goog-api-key"] == "sk-gemini"
    payload = request["json"]
    assert payload["contents"][0]["role"] == "user"
    assert _DOSSIER in payload["contents"][0]["parts"][0]["text"]
    assert payload["systemInstruction"]["parts"][0]["text"]
    assert "generationConfig" not in payload

    reviewer = assisted_provider_from_snapshot(
        str(tmp_path),
        frozen["note_reviewer"],
        http_client_factory=_http_factory(text_response, calls),
    )
    assert reviewer.review_markdown(_DOSSIER, "# 候选") == "# Gemini 笔记"

    json_response = _HttpResponse(
        200,
        {
            "candidates": [
                {"content": {"parts": [{"text": '{"segments": []}'}], "role": "model"}}
            ]
        },
    )
    json_calls: list[dict[str, object]] = []
    podcast = podcast_provider_from_snapshot(
        str(tmp_path),
        frozen["podcast"],
        http_client_factory=_http_factory(json_response, json_calls),
    )
    assert podcast.generate(_DOSSIER, ()) == '{"segments": []}'
    assert json_calls[-1]["json"]["generationConfig"] == {
        "responseMimeType": "application/json"
    }


def test_openai_chat_new_preset_still_uses_sdk_transport(tmp_path: Path) -> None:
    connect(
        tmp_path,
        name="w32-note",
        preset="kimi",
        secret_value="sk-kimi",
        model="kimi-k3",
    )
    frozen = _bind_and_freeze(tmp_path, note_connection="w32-note")

    class _ChatCompletions:
        def __init__(self, calls: list[dict[str, object]]) -> None:
            self._calls = calls

        def create(self, **kwargs: object) -> object:
            self._calls.append(kwargs)

            class _Message:
                content = "# Kimi 笔记"

            class _Choice:
                message = _Message()

            class _Response:
                choices = [_Choice()]

            return _Response()

    class _Client:
        def __init__(self, calls: list[dict[str, object]]) -> None:
            self.chat = type("Chat", (), {"completions": _ChatCompletions(calls)})()

    sdk_calls: list[dict[str, object]] = []

    def factory(**kwargs: object) -> _Client:
        sdk_calls.append(kwargs)
        return _Client(sdk_calls)

    writer = assisted_provider_from_snapshot(
        str(tmp_path), frozen["note_writer"], client_factory=factory
    )
    assert writer.write_markdown(_DOSSIER) == "# Kimi 笔记"
    assert sdk_calls[0] == {
        "api_key": "sk-kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "max_retries": 0,
    }
    request = sdk_calls[1]
    assert request["model"] == "kimi-k3"


def test_native_transport_errors_are_sanitized_per_domain(tmp_path: Path) -> None:
    connect(
        tmp_path,
        name="w32-note",
        preset="anthropic",
        secret_value="sk-anthropic",
        model="claude-sonnet-5",
    )
    frozen = _bind_and_freeze(tmp_path, note_connection="w32-note")

    response = _HttpResponse(401, {"error": {"message": "invalid key sk-anthropic"}})
    calls: list[dict[str, object]] = []
    writer = assisted_provider_from_snapshot(
        str(tmp_path),
        frozen["note_writer"],
        http_client_factory=_http_factory(response, calls),
    )

    with pytest.raises(NoteProviderError) as error:
        writer.write_markdown(_DOSSIER)
    assert "sk-anthropic" not in str(error.value)
    assert "HTTP 401" in str(error.value)

    connect(
        tmp_path,
        name="w32-podcast",
        preset="gemini",
        secret_value="sk-gemini",
        model="gemini-3.6-flash",
    )
    set_role_binding(tmp_path, role="podcast", connection_name="w32-podcast")
    podcast_frozen = freeze_role_bindings(tmp_path)
    gemini_response = _HttpResponse(500, {"error": {"message": "boom sk-gemini"}})
    gemini_calls: list[dict[str, object]] = []
    podcast = podcast_provider_from_snapshot(
        str(tmp_path),
        podcast_frozen["podcast"],
        http_client_factory=_http_factory(gemini_response, gemini_calls),
    )

    with pytest.raises(PodcastProviderError) as podcast_error:
        podcast.generate(_DOSSIER, ())
    assert "sk-gemini" not in str(podcast_error.value)
    assert "HTTP 500" in str(podcast_error.value)


def test_provider_service_rejects_non_llm_provider_identity() -> None:
    snapshot = ProviderRoleSnapshot(
        capability="llm",
        connection_id="fake",
        secret_id=None,
        provider="xiaomi-mimo-tts",
        endpoint="https://api.xiaomimimo.com/v1",
        model="mimo-v2.5-tts",
        adapter_revision="1",
        settings_sha256="a" * 64,
    )
    with pytest.raises(ValueError, match="LLM"):
        assisted_provider_from_snapshot("unused-root", snapshot)


# --------------------------------------------------------------------------- #
# Web API: preview endpoint, explicit-model create, adapters projection.
# --------------------------------------------------------------------------- #


def test_web_api_projects_w32_adapter_catalog(tmp_path: Path) -> None:
    response = TestClient(create_web_app(tmp_path)).get("/api/providers/settings")

    assert response.status_code == 200
    adapters = {item["preset"]: item for item in response.json()["adapters"]}
    assert set(adapters) >= set(W32_PRESET_CONTRACT) | {"mimo", "deepseek"}
    assert adapters["openai"]["catalog_mode"] == "live"
    assert adapters["openai"]["requires_model"] is True
    assert adapters["openai"]["key_entry"] == "https://platform.openai.com/api-keys"
    assert adapters["openai"]["name"] == "OpenAI"
    assert adapters["glm"]["catalog_mode"] == "curated"
    assert adapters["glm"]["requires_model"] is True
    assert adapters["anthropic"]["catalog_mode"] == "live"
    assert adapters["anthropic"]["api_family"] == "anthropic"
    assert adapters["mimo"]["requires_model"] is True
    assert adapters["mimo"]["catalog_mode"] == "live"
    assert adapters["mimo"]["key_entry"] == (
        "https://platform.xiaomimimo.com/#/console/api-keys"
    )
    assert adapters["deepseek"]["requires_model"] is True
    assert adapters["deepseek"]["key_entry"] == "https://platform.deepseek.com/api_keys"


def test_web_api_preview_catalog_uses_body_key_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_preview(
        preset: str, api_key: str, **_kwargs: object
    ) -> ModelCatalogPreview:
        if preset != "glm":
            raise ProviderModelCatalogError(
                "unsupported_preset", "该服务不支持获取模型目录。"
            )
        calls.append((preset, api_key))
        return ModelCatalogPreview(
            source="curated",
            models=[{"id": "glm-5.3"}],
            note="内置支持列表",
            adapter_revision="1",
        )

    monkeypatch.setattr(web_app, "preview_provider_model_catalog", fake_preview)
    client = TestClient(create_web_app(tmp_path))

    ok = client.post("/api/providers/presets/glm/models", json={"api_key": "body-key"})
    rejected_query = client.post(
        "/api/providers/presets/glm/models?key=https://evil.example"
    )
    rejected_unknown = client.post(
        "/api/providers/presets/not-a-preset/models", json={"api_key": "k"}
    )

    assert ok.status_code == 200
    assert ok.json() == {
        "source": "curated",
        "models": [{"id": "glm-5.3"}],
        "note": "内置支持列表",
        "adapter_revision": "1",
    }
    assert rejected_query.status_code == 422
    assert rejected_unknown.status_code == 404
    assert calls == [("glm", "body-key")]


def test_web_api_create_requires_explicit_model_for_w32_preset(tmp_path: Path) -> None:
    client = TestClient(create_web_app(tmp_path))

    refused = client.post(
        "/api/providers/connections",
        json={"name": "w32", "preset": "openai", "api_key": "sk-test"},
    )
    assert refused.status_code == 400
    assert "模型" in refused.json()["detail"]
    assert load_settings(tmp_path).connections == {}

    saved = client.post(
        "/api/providers/connections",
        json={
            "name": "w32",
            "preset": "openai",
            "api_key": "sk-test",
            "model": "gpt-5.2",
        },
    )
    assert saved.status_code == 200
    connections = {item["name"]: item for item in saved.json()["connections"]}
    assert connections["w32"]["model"] == "gpt-5.2"
    assert load_settings(tmp_path).connections["w32"].secret_id is not None


def test_web_api_preview_error_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_preview(*_args: object, **_kwargs: object) -> ModelCatalogPreview:
        raise ProviderModelCatalogError(
            "authentication", "官方目录认证失败，请检查 API Key。"
        )

    monkeypatch.setattr(web_app, "preview_provider_model_catalog", fake_preview)
    response = TestClient(create_web_app(tmp_path)).post(
        "/api/providers/presets/kimi/models", json={"api_key": "sk-live-1234567890"}
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "官方目录认证失败，请检查 API Key。"}
    assert "sk-live-1234567890" not in response.text


def test_web_api_validation_failures_never_echo_credential_inputs(
    tmp_path: Path,
) -> None:
    over_long_key = "sk-echo-" + "A" * 5000
    client = TestClient(create_web_app(tmp_path))

    preview = client.post(
        "/api/providers/presets/glm/models", json={"api_key": over_long_key}
    )
    connection = client.post(
        "/api/providers/connections",
        json={"name": "echo", "preset": "kimi", "api_key": over_long_key},
    )

    for response in (preview, connection):
        assert response.status_code == 422
        assert over_long_key not in response.text
        assert "AAAA" not in response.text
        details = response.json()["detail"]
        assert details
        assert all("input" not in error for error in details)
        assert all("msg" in error and "loc" in error for error in details)


# --------------------------------------------------------------------------- #
# Thin CLI: preview-models and explicit-model connect.
# --------------------------------------------------------------------------- #


def test_cli_preview_models_prints_curated_catalog_without_key_prompt() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["provider", "preview-models", "glm"])

    assert result.exit_code == 0, result.output
    assert "source=curated" in result.output
    assert "glm-5.3" in result.output
    assert "不代表" in result.output


def test_cli_preview_models_prompts_key_for_live_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_preview(
        preset: str, api_key: str, **_kwargs: object
    ) -> ModelCatalogPreview:
        calls.append((preset, api_key))
        return ModelCatalogPreview(
            source="live",
            models=[{"id": "kimi-k3"}],
            note="实时目录",
            adapter_revision="1",
        )

    monkeypatch.setattr(cli, "preview_provider_model_catalog", fake_preview)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["provider", "preview-models", "kimi"],
        input="cli-key\n",
    )

    assert result.exit_code == 0, result.output
    assert "source=live" in result.output
    assert "kimi-k3" in result.output
    assert calls == [("kimi", "cli-key")]


def test_cli_connect_requires_explicit_model_for_w32_preset(tmp_path: Path) -> None:
    runner = CliRunner()
    refused = runner.invoke(
        app,
        [
            "provider",
            "connect",
            "openai",
            "--name",
            "w32",
            "--output-root",
            str(tmp_path),
        ],
    )
    assert refused.exit_code == 1
    assert "模型" in refused.output
    assert load_settings(tmp_path).connections == {}

    saved = runner.invoke(
        app,
        [
            "provider",
            "connect",
            "openai",
            "--name",
            "w32",
            "--model",
            "gpt-5.2",
            "--output-root",
            str(tmp_path),
        ],
        input="sk-cli\n",
    )
    assert saved.exit_code == 0, saved.output
    assert load_settings(tmp_path).connections["w32"].model == "gpt-5.2"
