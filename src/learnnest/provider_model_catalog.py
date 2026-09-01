"""Safe, bounded model-directory reads for supported remote LLM connections."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from openai import OpenAI

from learnnest.llm_transports import ANTHROPIC_VERSION
from learnnest.provider_profiles import (
    ProviderConnection,
    ProviderPreset,
    load_settings,
)
from learnnest.provider_secrets import ProviderSecretStore, SecretStoreError

MAX_MODEL_ID_LENGTH = 128
MAX_OWNED_BY_LENGTH = 128
MAX_MODEL_CATALOG_ITEMS = 64
MODEL_CATALOG_TIMEOUT_SECONDS = 10.0
MODEL_CATALOG_PAGE_SIZE = 1000
LIVE_CATALOG_NOTE = (
    "实时目录来自该 Provider 当前可见模型与语栖适配允许列表的交集，"
    "不代表账号已开通全部模型。"
)
CURATED_CATALOG_NOTE = (
    "内置支持列表由语栖维护并标注适配版本，不代表你的账号已开通该模型；"
    "连接健康需用“检查连接”另行确认。"
)
CatalogErrorCode = Literal[
    "connection_not_found",
    "unsupported_preset",
    "unsupported_endpoint",
    "missing_secret",
    "authentication",
    "rate_limited",
    "timeout",
    "service_error",
    "response_format",
    "no_compatible_models",
]
CatalogClientFactory = Callable[..., Any]
CatalogHttpClientFactory = Callable[..., Any]


class ProviderModelCatalogError(ValueError):
    """A safe, public error for one model-directory request."""

    def __init__(self, code: CatalogErrorCode, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(message)


@dataclass(frozen=True)
class ModelCatalogPreview:
    """One bounded catalog answer with its explicit source label."""

    source: Literal["live", "curated"]
    models: list[dict[str, str]]
    note: str
    adapter_revision: str


class _CatalogHttpCarrier(RuntimeError):
    """Carry one HTTP status and parsed body into the shared classifier."""

    def __init__(self, status_code: int, body: Mapping[str, Any] | None) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}")


def preview_model_catalog(
    preset_key: str,
    api_key: str,
    *,
    client_factory: CatalogClientFactory = OpenAI,
    http_client_factory: CatalogHttpClientFactory = httpx.Client,
) -> ModelCatalogPreview:
    """Fetch one preset's catalog with a transient key and no persistence.

    Curated presets never touch the network and never need a key.  Live
    presets send exactly one zero-retry request to the preset's fixed
    official catalog endpoint; failures are surfaced, never downgraded to
    the curated list.
    """
    preset = _llm_preset(preset_key)
    if preset.catalog_mode == "curated":
        return ModelCatalogPreview(
            source="curated",
            models=_curated_models(preset),
            note=CURATED_CATALOG_NOTE,
            adapter_revision=preset.adapter_revision,
        )
    key = (api_key or "").strip()
    if not key:
        raise ProviderModelCatalogError(
            "missing_secret", "未填写 API Key，无法获取模型。"
        )
    return ModelCatalogPreview(
        source="live",
        models=_fetch_live_models(
            key,
            preset,
            client_factory=client_factory,
            http_client_factory=http_client_factory,
        ),
        note=LIVE_CATALOG_NOTE,
        adapter_revision=preset.adapter_revision,
    )


def fetch_model_catalog(
    output_root: str | Path,
    connection_name: str,
    *,
    client_factory: CatalogClientFactory = OpenAI,
    http_client_factory: CatalogHttpClientFactory = httpx.Client,
) -> list[dict[str, str]]:
    """Fetch and filter one supported connection's official model directory.

    The connection's encrypted secret is read only after its endpoint has been
    checked against the product registry.  The returned list is the only
    provider data that escapes this boundary; no raw response or task attempt
    is persisted.
    """
    settings = load_settings(output_root)
    connection = settings.connections.get(connection_name)
    if connection is None:
        raise ProviderModelCatalogError(
            "connection_not_found", "连接不存在，无法获取模型。"
        )
    preset = _catalog_preset_for_connection(connection)
    if preset.catalog_mode == "curated":
        return _curated_models(preset)
    if connection.secret_id is None:
        raise ProviderModelCatalogError(
            "missing_secret", "连接未保存 API Key，无法获取模型。"
        )
    try:
        secret = ProviderSecretStore(output_root).read(connection.secret_id)
    except SecretStoreError as error:
        raise ProviderModelCatalogError(
            "missing_secret", "连接未保存 API Key，无法获取模型。"
        ) from error
    return _fetch_live_models(
        secret.get_secret_value(),
        preset,
        client_factory=client_factory,
        http_client_factory=http_client_factory,
    )


def normalize_model_catalog(
    response: object, *, allowed_models: Iterable[str]
) -> list[dict[str, str]]:
    """Project one OpenAI-compatible list response to bounded public fields."""
    data = _response_data(response)
    allowed = set(allowed_models)
    by_id: dict[str, dict[str, str]] = {}
    for item in data:
        model_id, owned_by = _model_fields(item)
        if model_id not in allowed:
            continue
        projected: dict[str, str] = {"id": model_id}
        if owned_by:
            projected["owned_by"] = owned_by
        existing = by_id.get(model_id)
        if existing is None:
            by_id[model_id] = projected
        elif "owned_by" not in existing and "owned_by" in projected:
            existing["owned_by"] = projected["owned_by"]
    return [by_id[model_id] for model_id in sorted(by_id)[:MAX_MODEL_CATALOG_ITEMS]]


def _llm_preset(preset_key: str) -> ProviderPreset:
    from learnnest.provider_profiles import connection_presets

    preset = connection_presets().get(preset_key)
    if preset is None or preset.capability != "llm" or preset.catalog_mode is None:
        raise ProviderModelCatalogError(
            "unsupported_preset", "该服务不支持获取模型目录。"
        )
    return preset


def _curated_models(preset: ProviderPreset) -> list[dict[str, str]]:
    return [{"id": model_id} for model_id in sorted(preset.allowed_models)]


def _catalog_preset_for_connection(connection: ProviderConnection) -> ProviderPreset:
    from learnnest.provider_profiles import PRESETS

    preset = PRESETS.get(connection.preset)
    if (
        preset is None
        or preset.capability != "llm"
        or preset.catalog_mode is None
        or connection.capability != "llm"
        or connection.api_family != preset.api_family
        or connection.endpoint != preset.endpoint
    ):
        raise ProviderModelCatalogError(
            "unsupported_endpoint", "该连接不支持获取官方模型目录。"
        )
    return preset


def _fetch_live_models(
    api_key: str,
    preset: ProviderPreset,
    *,
    client_factory: CatalogClientFactory,
    http_client_factory: CatalogHttpClientFactory,
) -> list[dict[str, str]]:
    if preset.api_family in {"openai_chat", "openai_responses"}:
        models = _fetch_openai_style_models(api_key, preset, client_factory)
    elif preset.api_family == "anthropic":
        models = _fetch_anthropic_models(api_key, preset, http_client_factory)
    elif preset.api_family == "gemini":
        models = _fetch_gemini_models(api_key, preset, http_client_factory)
    else:
        raise ProviderModelCatalogError(
            "unsupported_endpoint", "该连接不支持获取官方模型目录。"
        )
    if not models:
        raise ProviderModelCatalogError(
            "no_compatible_models", "官方目录中没有当前能力已适配的模型。"
        )
    return models


def _fetch_openai_style_models(
    api_key: str,
    preset: ProviderPreset,
    client_factory: CatalogClientFactory,
) -> list[dict[str, str]]:
    assert preset.catalog_endpoint is not None
    try:
        client = client_factory(
            api_key=api_key,
            base_url=preset.catalog_endpoint.removesuffix("/models"),
            max_retries=0,
            timeout=MODEL_CATALOG_TIMEOUT_SECONDS,
        )
        response = client.models.list()
    except ProviderModelCatalogError:
        raise
    except Exception as error:
        raise _classify_request_error(error) from error
    return normalize_model_catalog(response, allowed_models=preset.allowed_models)


def _fetch_anthropic_models(
    api_key: str,
    preset: ProviderPreset,
    http_client_factory: CatalogHttpClientFactory,
) -> list[dict[str, str]]:
    assert preset.catalog_endpoint is not None
    body = _http_get_json(
        preset.catalog_endpoint,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        params={"limit": MODEL_CATALOG_PAGE_SIZE},
        http_client_factory=http_client_factory,
    )
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, list):
        raise ProviderModelCatalogError(
            "response_format", "官方模型目录响应格式无法识别。"
        )
    model_ids = [_require_model_id(item) for item in data]
    return _filtered_catalog(model_ids, preset.allowed_models)


def _fetch_gemini_models(
    api_key: str,
    preset: ProviderPreset,
    http_client_factory: CatalogHttpClientFactory,
) -> list[dict[str, str]]:
    assert preset.catalog_endpoint is not None
    body = _http_get_json(
        preset.catalog_endpoint,
        headers={"x-goog-api-key": api_key},
        params={"pageSize": MODEL_CATALOG_PAGE_SIZE},
        http_client_factory=http_client_factory,
    )
    models = body.get("models") if isinstance(body, Mapping) else None
    if not isinstance(models, list):
        raise ProviderModelCatalogError(
            "response_format", "官方模型目录响应格式无法识别。"
        )
    model_ids = [
        _require_model_id(item, name_field="name", prefix="models/") for item in models
    ]
    return _filtered_catalog(model_ids, preset.allowed_models)


def _http_get_json(
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, object],
    http_client_factory: CatalogHttpClientFactory,
) -> object:
    try:
        client = http_client_factory(timeout=MODEL_CATALOG_TIMEOUT_SECONDS)
        with client:
            response = client.get(url, headers=headers, params=params)
        try:
            body: object = response.json()
        except Exception:
            body = None
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and status_code >= 400:
            raise _CatalogHttpCarrier(
                status_code, body if isinstance(body, Mapping) else None
            )
    except ProviderModelCatalogError:
        raise
    except Exception as error:
        raise _classify_request_error(error) from error
    return body


def _require_model_id(item: object, *, name_field: str = "id", prefix: str = "") -> str:
    model_id = item.get(name_field) if isinstance(item, Mapping) else None
    if not isinstance(model_id, str) or not (
        model_id := model_id.strip().removeprefix(prefix)
    ):
        raise ProviderModelCatalogError(
            "response_format", "官方模型目录响应格式无法识别。"
        )
    if len(model_id) > MAX_MODEL_ID_LENGTH:
        raise ProviderModelCatalogError(
            "response_format", "官方模型目录响应格式无法识别。"
        )
    return model_id


def _filtered_catalog(
    model_ids: Iterable[str], allowed_models: Iterable[str]
) -> list[dict[str, str]]:
    allowed = set(allowed_models)
    unique = {model_id for model_id in model_ids if model_id in allowed}
    return [{"id": model_id} for model_id in sorted(unique)[:MAX_MODEL_CATALOG_ITEMS]]


def _response_data(response: object) -> list[object]:
    if isinstance(response, Mapping):
        data = response.get("data")
    else:
        data = getattr(response, "data", None)
    if not isinstance(data, list):
        raise ProviderModelCatalogError(
            "response_format", "官方模型目录响应格式无法识别。"
        )
    return data


def _model_fields(item: object) -> tuple[str, str | None]:
    if isinstance(item, Mapping):
        model_id = item.get("id")
        owned_by = item.get("owned_by")
    else:
        model_id = getattr(item, "id", None)
        owned_by = getattr(item, "owned_by", None)
    if (
        not isinstance(model_id, str)
        or not (model_id := model_id.strip())
        or len(model_id) > MAX_MODEL_ID_LENGTH
    ):
        raise ProviderModelCatalogError(
            "response_format", "官方模型目录响应格式无法识别。"
        )
    if owned_by is not None:
        if not isinstance(owned_by, str) or len(owned_by.strip()) > MAX_OWNED_BY_LENGTH:
            raise ProviderModelCatalogError(
                "response_format", "官方模型目录响应格式无法识别。"
            )
        owned_by = owned_by.strip() or None
    return model_id, owned_by


def _classify_request_error(error: Exception) -> ProviderModelCatalogError:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    error_name = type(error).__name__.lower()
    if (
        status_code in {401, 403}
        or "authentication" in error_name
        or "permission" in error_name
    ):
        return ProviderModelCatalogError(
            "authentication", "官方目录认证失败，请检查已保存 API Key。"
        )
    if status_code == 429 or "ratelimit" in error_name or "rate_limit" in error_name:
        return ProviderModelCatalogError(
            "rate_limited", "官方模型目录请求达到频率限制，请稍后重试。"
        )
    if isinstance(error, TimeoutError) or "timeout" in error_name:
        return ProviderModelCatalogError(
            "timeout", "官方模型目录请求超时，请稍后重试。"
        )
    return ProviderModelCatalogError(
        "service_error", "官方模型目录服务暂时不可用，请稍后重试。"
    )
