"""Safe, bounded model-directory reads for supported remote LLM connections."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI

from learnnest.provider_profiles import ProviderConnection, load_settings
from learnnest.provider_secrets import ProviderSecretStore, SecretStoreError

MAX_MODEL_ID_LENGTH = 128
MAX_OWNED_BY_LENGTH = 128
MAX_MODEL_CATALOG_ITEMS = 64
MODEL_CATALOG_TIMEOUT_SECONDS = 10.0
CatalogErrorCode = Literal[
    "connection_not_found",
    "unsupported_endpoint",
    "missing_secret",
    "authentication",
    "rate_limited",
    "timeout",
    "service_error",
    "response_format",
    "no_compatible_models",
]


class ProviderModelCatalogError(ValueError):
    """A safe, public error for one model-directory request."""

    def __init__(self, code: CatalogErrorCode, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(message)


def fetch_model_catalog(
    output_root: str | Path,
    connection_name: str,
    *,
    client_factory: Callable[..., Any] = OpenAI,
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
    catalog = _catalog_for_connection(connection)
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

    try:
        client = client_factory(
            api_key=secret.get_secret_value(),
            base_url=catalog["base_url"],
            max_retries=0,
            timeout=MODEL_CATALOG_TIMEOUT_SECONDS,
        )
        response = client.models.list()
    except ProviderModelCatalogError:
        raise
    except Exception as error:
        raise _classify_request_error(error) from error

    models = normalize_model_catalog(response, allowed_models=catalog["allowed_models"])
    if not models:
        raise ProviderModelCatalogError(
            "no_compatible_models", "官方目录中没有当前能力已适配的模型。"
        )
    return models


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


def _catalog_for_connection(connection: ProviderConnection) -> dict[str, object]:
    from learnnest.provider_profiles import PRESETS

    preset = PRESETS.get(connection.preset)
    if (
        preset is None
        or preset.catalog_endpoint is None
        or not preset.allowed_models
        or connection.capability != "llm"
        or connection.api_family != "openai_chat"
        or connection.endpoint != preset.endpoint
    ):
        raise ProviderModelCatalogError(
            "unsupported_endpoint", "该连接不支持获取官方模型目录。"
        )
    return {
        "base_url": preset.catalog_endpoint.removesuffix("/models"),
        "allowed_models": preset.allowed_models,
    }


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
