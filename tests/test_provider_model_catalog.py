from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from learnnest.automation_models import AutomationBudget, AutomationPolicy
from learnnest.automation_store import authorize, load_status, save_policy
from learnnest.cli import app
from learnnest.provider_model_catalog import (
    MAX_MODEL_CATALOG_ITEMS,
    MAX_MODEL_ID_LENGTH,
    ProviderModelCatalogError,
    fetch_model_catalog,
    normalize_model_catalog,
)
from learnnest.provider_profiles import (
    PRESETS,
    connect,
    freeze_role_bindings,
    load_settings,
    set_role_binding,
    settings_sha256,
    update_connection_model,
)
from learnnest.provider_secrets import ProviderSecretStore
from learnnest.provider_service import assisted_snapshot_from_binding
from learnnest.web_app import create_web_app
import learnnest.cli as cli
import learnnest.web_app as web_app


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
        self.calls = 0

    def list(self) -> object:
        self.calls += 1
        return self.response


class _Client:
    def __init__(self, response: object) -> None:
        self.models = _Models(response)


def _factory(response: object, calls: list[dict[str, object]]):
    def create(**kwargs: object) -> _Client:
        calls.append(kwargs)
        return _Client(response)

    return create


def test_registry_declares_only_supported_remote_llm_catalogs() -> None:
    assert PRESETS["mimo"].catalog_endpoint == "https://api.xiaomimimo.com/v1/models"
    assert PRESETS["mimo"].allowed_models == ("mimo-v2.5", "mimo-v2.5-pro")
    assert PRESETS["deepseek"].catalog_endpoint == "https://api.deepseek.com/models"
    assert PRESETS["deepseek"].allowed_models == (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    )
    assert PRESETS["mimo-tts"].catalog_endpoint is None
    assert PRESETS["mimo-tts"].allowed_models == ()
    assert PRESETS["local-asr"].catalog_endpoint is None


def test_model_catalog_fetches_from_fixed_official_client_and_filters_sorted_unique_ids(
    tmp_path: Path,
) -> None:
    connection = connect(
        tmp_path,
        name="mimo",
        preset="mimo",
        secret_value="catalog-secret",
        model="mimo-v2.5",
    )
    calls: list[dict[str, object]] = []
    response = _ModelPage(
        [
            _Model("mimo-v2.5-pro", "xiaomi"),
            _Model("unrelated-model", "other"),
            _Model("mimo-v2.5"),
            _Model("mimo-v2.5-pro", "duplicate"),
        ]
    )
    before = load_settings(tmp_path)

    models = fetch_model_catalog(
        tmp_path, connection.name, client_factory=_factory(response, calls)
    )
    after = load_settings(tmp_path)

    assert models == [
        {"id": "mimo-v2.5"},
        {"id": "mimo-v2.5-pro", "owned_by": "xiaomi"},
    ]
    assert calls == [
        {
            "api_key": "catalog-secret",
            "base_url": "https://api.xiaomimimo.com/v1",
            "max_retries": 0,
            "timeout": 10.0,
        }
    ]
    assert connection.secret_id is not None
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert connection.secret_id == after.connections["mimo"].secret_id
    assert not list((tmp_path / ".learnnest" / "automation").rglob("*.json"))
    assert "catalog-secret" not in json.dumps(models)


def test_deepseek_catalog_uses_models_root_without_v1(tmp_path: Path) -> None:
    connect(
        tmp_path,
        name="deepseek",
        preset="deepseek",
        secret_value="secret",
        model="deepseek-v4-pro",
    )
    calls: list[dict[str, object]] = []

    result = fetch_model_catalog(
        tmp_path,
        "deepseek",
        client_factory=_factory(
            _ModelPage([_Model("deepseek-v4-flash"), _Model("deepseek-v4-pro")]),
            calls,
        ),
    )

    assert [item["id"] for item in result] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ]
    assert calls[0]["base_url"] == "https://api.deepseek.com"


def test_custom_endpoint_is_rejected_before_any_client_or_secret_use(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    with pytest.raises(ValueError, match="endpoint"):
        connect(
            tmp_path,
            name="proxy",
            preset="mimo",
            endpoint="https://proxy.example/v1",
            secret_value="must-not-leave-store",
            model="mimo-v2.5",
        )

    assert load_settings(tmp_path).connections == {}
    assert not (tmp_path / ".learnnest" / "providers" / "secrets").exists()
    assert calls == []


def test_missing_secret_is_safe_and_does_not_construct_a_client(tmp_path: Path) -> None:
    connection = connect(
        tmp_path, name="mimo", preset="mimo", secret_value="secret", model="mimo-v2.5"
    )
    assert connection.secret_id is not None
    ProviderSecretStore(tmp_path).path_for(connection.secret_id).unlink()
    calls: list[dict[str, object]] = []

    with pytest.raises(ProviderModelCatalogError, match="API Key") as error:
        fetch_model_catalog(tmp_path, "mimo", client_factory=_factory({}, calls))

    assert error.value.code == "missing_secret"
    assert calls == []


@pytest.mark.parametrize(
    ("failure", "code", "message"),
    [
        (
            type("AuthFailure", (Exception,), {"status_code": 401}),
            "authentication",
            "认证失败",
        ),
        (
            type("ForbiddenFailure", (Exception,), {"status_code": 403}),
            "authentication",
            "认证失败",
        ),
        (
            type("RateFailure", (Exception,), {"status_code": 429}),
            "rate_limited",
            "频率限制",
        ),
        (TimeoutError("provider timeout"), "timeout", "超时"),
        (
            type("ServiceFailure", (Exception,), {"status_code": 503}),
            "service_error",
            "暂时不可用",
        ),
    ],
)
def test_catalog_errors_are_classified_without_provider_details(
    tmp_path: Path,
    failure: object,
    code: str,
    message: str,
) -> None:
    connect(
        tmp_path,
        name="mimo",
        preset="mimo",
        secret_value="secret-value",
        model="mimo-v2.5",
    )

    class FailingModels:
        def list(self) -> object:
            if isinstance(failure, type):
                raise failure("raw response body https://evil.example?key=secret-value")
            raise failure

    class FailingClient:
        models = FailingModels()

    with pytest.raises(ProviderModelCatalogError) as error:
        fetch_model_catalog(
            tmp_path, "mimo", client_factory=lambda **_kwargs: FailingClient()
        )

    assert error.value.code == code
    assert message in str(error.value)
    assert "secret-value" not in str(error.value)
    assert "evil.example" not in str(error.value)


def test_catalog_format_and_compatibility_errors_are_distinct(tmp_path: Path) -> None:
    connect(
        tmp_path, name="mimo", preset="mimo", secret_value="secret", model="mimo-v2.5"
    )

    with pytest.raises(ProviderModelCatalogError) as invalid:
        fetch_model_catalog(
            tmp_path,
            "mimo",
            client_factory=_factory(_ModelPage({"id": "mimo-v2.5"}), []),
        )
    assert invalid.value.code == "response_format"
    assert "响应格式" in str(invalid.value)

    with pytest.raises(ProviderModelCatalogError) as empty:
        fetch_model_catalog(
            tmp_path,
            "mimo",
            client_factory=_factory(_ModelPage([_Model("not-supported")]), []),
        )
    assert empty.value.code == "no_compatible_models"
    assert "没有当前能力已适配" in str(empty.value)


def test_normalized_catalog_is_bounded_and_does_not_return_raw_fields() -> None:
    allowed = [f"model-{index:03d}" for index in range(MAX_MODEL_CATALOG_ITEMS + 5)]
    payload = {
        "data": [
            {"id": model_id, "owned_by": "owner", "created": 123}
            for model_id in allowed
        ]
    }

    result = normalize_model_catalog(payload, allowed_models=allowed)

    assert len(result) == MAX_MODEL_CATALOG_ITEMS
    assert all(set(item) <= {"id", "owned_by"} for item in result)
    assert all(len(item["id"]) <= MAX_MODEL_ID_LENGTH for item in result)


def test_update_connection_model_preserves_connection_identity_and_changes_settings_sha(
    tmp_path: Path,
) -> None:
    connection = connect(
        tmp_path, name="mimo", preset="mimo", secret_value="secret", model="mimo-v2.5"
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="mimo")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="mimo")
    before = load_settings(tmp_path)
    before_sha = settings_sha256(before)
    frozen = freeze_role_bindings(tmp_path)

    updated = update_connection_model(
        tmp_path,
        name="mimo",
        model="mimo-v2.5-pro",
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )
    current = load_settings(tmp_path)

    assert updated.model == "mimo-v2.5-pro"
    assert updated.preset == connection.preset
    assert updated.endpoint == connection.endpoint
    assert updated.secret_id == connection.secret_id
    assert current.connections["mimo"].secret_id == connection.secret_id
    assert settings_sha256(current) != before_sha
    assert frozen["note_writer"].model == "mimo-v2.5"
    assert frozen["note_reviewer"].model == "mimo-v2.5"
    assert frozen["note_writer"].secret_id == connection.secret_id


def test_model_change_invalidates_authorization_but_does_not_rewrite_frozen_policy(
    tmp_path: Path,
) -> None:
    connect(
        tmp_path, name="mimo", preset="mimo", secret_value="secret", model="mimo-v2.5"
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="mimo")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="mimo")
    frozen = freeze_role_bindings(tmp_path)
    writer = assisted_snapshot_from_binding(frozen["note_writer"])
    reviewer = assisted_snapshot_from_binding(frozen["note_reviewer"])
    save_policy(
        tmp_path,
        AutomationPolicy(
            writer=writer,
            reviewer=reviewer,
            budget=AutomationBudget(),
            default_output="complete_note",
        ),
    )
    authorized = authorize(tmp_path, now=datetime(2026, 9, 1, tzinfo=UTC))
    old_frozen_model = authorized.policy.writer.model
    old_authorized_sha = authorized.policy.provider_settings_sha256

    update_connection_model(tmp_path, name="mimo", model="mimo-v2.5-pro")
    current = load_settings(tmp_path)
    status = load_status(tmp_path)

    assert old_frozen_model == "mimo-v2.5"
    assert status is not None
    assert status.policy.writer.model == old_frozen_model
    assert status.policy.provider_settings_sha256 == old_authorized_sha
    assert status.policy.provider_settings_sha256 != settings_sha256(current)


def test_web_api_fetch_and_save_model_do_not_accept_connection_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connection = connect(
        tmp_path, name="mimo", preset="mimo", secret_value="secret", model="mimo-v2.5"
    )
    calls: list[tuple[Path, str]] = []

    def fake_fetch(root: str | Path, name: str) -> list[dict[str, str]]:
        calls.append((Path(root), name))
        return [{"id": "mimo-v2.5-pro", "owned_by": "xiaomi"}]

    monkeypatch.setattr(web_app, "fetch_provider_model_catalog", fake_fetch)
    client = TestClient(create_web_app(tmp_path))

    catalog = client.post("/api/providers/connections/mimo/models")
    rejected = client.post(
        "/api/providers/connections/mimo/models",
        json={"url": "https://evil.example", "api_key": "secret", "provider": "evil"},
    )
    saved = client.put(
        "/api/providers/connections/mimo/model", json={"model": "mimo-v2.5-pro"}
    )

    assert catalog.status_code == 200
    assert catalog.json() == {"models": [{"id": "mimo-v2.5-pro", "owned_by": "xiaomi"}]}
    assert rejected.status_code == 422
    assert saved.status_code == 200
    assert saved.json()["connections"][0]["model"] == "mimo-v2.5-pro"
    assert calls == [(tmp_path.resolve(), "mimo")]
    assert load_settings(tmp_path).connections["mimo"].secret_id == connection.secret_id


def test_web_api_projects_safe_catalog_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect(
        tmp_path, name="mimo", preset="mimo", secret_value="secret", model="mimo-v2.5"
    )
    monkeypatch.setattr(
        web_app,
        "fetch_provider_model_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProviderModelCatalogError(
                "authentication", "官方目录认证失败，请检查已保存 API Key。"
            )
        ),
    )

    response = TestClient(create_web_app(tmp_path)).post(
        "/api/providers/connections/mimo/models"
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "官方目录认证失败，请检查已保存 API Key。"}
    assert "secret" not in response.text


def test_cli_models_and_set_model_use_the_shared_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect(
        tmp_path, name="mimo", preset="mimo", secret_value="secret", model="mimo-v2.5"
    )
    monkeypatch.setattr(
        cli,
        "fetch_provider_model_catalog",
        lambda root, name: [{"id": "mimo-v2.5"}, {"id": "mimo-v2.5-pro"}],
    )
    runner = CliRunner()

    models = runner.invoke(
        app, ["provider", "models", "mimo", "--output-root", str(tmp_path)]
    )
    saved = runner.invoke(
        app,
        [
            "provider",
            "set-model",
            "mimo",
            "mimo-v2.5-pro",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert models.exit_code == 0, models.output
    assert models.output == "mimo-v2.5\nmimo-v2.5-pro\n"
    assert saved.exit_code == 0, saved.output
    assert "mimo-v2.5-pro" in saved.output
    assert load_settings(tmp_path).connections["mimo"].model == "mimo-v2.5-pro"
