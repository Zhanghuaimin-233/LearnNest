from __future__ import annotations

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from learnnest.automation_models import AutomationBudget
from learnnest.automation_models import AutomationPolicy
from learnnest.automation_runner import AutomationProviders, run_automation_tasks
from learnnest.automation_store import authorize, save_policy
from learnnest.automation_store import load_task_state
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.provider_profiles import (
    ProviderConnectionBoundError,
    ProviderConnectionNotFoundError,
    ProviderRoleNotBoundError,
    ProviderSettings,
    clear_role_binding,
    connect,
    delete_connection,
    freeze_role_bindings,
    get_connection,
    load_settings,
    save_settings,
    set_role_binding,
    settings_sha256,
    update_limits,
)
from learnnest.provider_secrets import ProviderSecretStore, SecretStoreError
from learnnest.provider_service import (
    ProviderConnectionCheckError,
    AdmittedAssistedProvider,
    assisted_provider_from_snapshot,
    execute_direct_provider_call,
    podcast_provider_from_snapshot,
    run_synthetic_llm_check,
    tts_provider_from_snapshot,
)
from learnnest.task_store import create_task
from learnnest.web_app import create_web_app
import learnnest.web_app as web_app
from learnnest.note_providers import NoteProviderError


def test_dpapi_secret_is_ciphertext_and_never_enters_settings_or_web_api(
    tmp_path: Path,
) -> None:
    secret = "stage3-secret-never-persisted-plain"
    connection = connect(
        tmp_path,
        name="mimo-note",
        preset="mimo",
        secret_value=secret,
        now=datetime(2026, 8, 2, tzinfo=UTC),
    )

    stored_secret = next(
        (tmp_path / ".learnnest" / "providers" / "secrets").glob("*.dpapi")
    ).read_bytes()
    settings = (tmp_path / ".learnnest" / "providers" / "settings.json").read_text(
        encoding="utf-8"
    )
    response = TestClient(create_web_app(tmp_path)).get("/api/providers/settings")

    assert stored_secret != secret.encode("utf-8")
    assert secret not in settings
    assert secret not in response.text
    assert response.status_code == 200
    assert response.json()["connections"][0]["state"] == "连接配置可读取"
    assert "secret_id" not in response.text
    assert connection.secret_id is not None


def test_provider_settings_projects_the_real_webui_catalog_and_editable_limits(
    tmp_path: Path,
) -> None:
    response = TestClient(create_web_app(tmp_path)).get("/api/providers/settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["adapters"] == [
        {"preset": "mimo", "name": "MiMo", "capability": "文本模型", "local": False},
        {
            "preset": "deepseek",
            "name": "DeepSeek",
            "capability": "文本模型",
            "local": False,
        },
        {
            "preset": "mimo-tts",
            "name": "MiMo TTS",
            "capability": "语音服务",
            "local": False,
        },
        {
            "preset": "windows-tts",
            "name": "Windows 系统语音",
            "capability": "语音服务",
            "local": True,
        },
    ]
    assert payload["limits"] == {
        "retries_per_role": 1,
        "global_calls_per_day": 80,
        "budget_group_calls_per_day": {
            "note": 20,
            "podcast": 20,
            "tts": 20,
            "asr": 20,
            "ocr": 20,
        },
    }
    assert "endpoint" not in response.text


@pytest.mark.parametrize(
    ("name", "preset", "secret_value", "expected_provider"),
    [
        ("mimo", "mimo", "mimo-label-secret", "MiMo"),
        ("deepseek", "deepseek", "deepseek-label-secret", "DeepSeek"),
        ("mimo-tts", "mimo-tts", "mimo-tts-label-secret", "MiMo TTS"),
        ("windows-tts", "windows-tts", None, "Windows 系统语音"),
    ],
)
def test_webui_provider_settings_labels_persistent_provider_ids(
    tmp_path: Path,
    name: str,
    preset: str,
    secret_value: str | None,
    expected_provider: str,
) -> None:
    connect(tmp_path, name=name, preset=preset, secret_value=secret_value)

    response = TestClient(create_web_app(tmp_path)).get("/api/providers/settings")

    assert response.status_code == 200
    assert response.json()["connections"] == [
        {
            "name": name,
            "provider": expected_provider,
            "state": (
                "本地配置可读取" if preset == "windows-tts" else "连接配置可读取"
            ),
        }
    ]


def test_webui_provider_settings_projects_a_corrupt_dpapi_secret_as_unavailable(
    tmp_path: Path,
) -> None:
    secret = "corrupt-dpapi-secret-never-disclose"
    connection = connect(tmp_path, name="mimo", preset="mimo", secret_value=secret)
    set_role_binding(tmp_path, role="note_writer", connection_name=connection.name)
    set_role_binding(tmp_path, role="note_reviewer", connection_name=connection.name)
    assert connection.secret_id is not None
    ProviderSecretStore(tmp_path).path_for(connection.secret_id).write_bytes(
        b"corrupt-dpapi-payload"
    )

    response = TestClient(create_web_app(tmp_path)).get(
        "/api/providers/settings?default_output=complete_note"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["connections"][0]["state"] == "连接密钥不可用"
    assert [role["state"] for role in payload["readiness"]["required_roles"]] == [
        "连接密钥不可用",
        "连接密钥不可用",
    ]
    assert secret not in response.text
    assert connection.secret_id not in response.text
    assert ".dpapi" not in response.text


def test_dpapi_corruption_unknown_secret_and_cross_user_failure_are_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProviderSecretStore(tmp_path)
    secret_id = store.put("mimo-note", "secret")
    path = store.path_for(secret_id)
    path.write_bytes(b"damaged")

    with pytest.raises(SecretStoreError, match="unavailable"):
        store.read(secret_id)
    with pytest.raises(SecretStoreError, match="unavailable"):
        store.read("unknown-secret")

    fresh = ProviderSecretStore(tmp_path)
    fresh_id = fresh.put("mimo-podcast", "other-secret")
    monkeypatch.setattr(fresh, "_unprotect", lambda _: (_ for _ in ()).throw(OSError()))
    with pytest.raises(SecretStoreError, match="unavailable"):
        fresh.read(fresh_id)


def test_staged_secret_deletion_is_ciphertext_only_and_reversible(
    tmp_path: Path,
) -> None:
    store = ProviderSecretStore(tmp_path)
    secret_id = store.put("mimo-note", "secret")
    path = store.path_for(secret_id)

    staged = store.stage_for_deletion(secret_id)

    assert staged is not None
    assert not path.exists()
    assert staged.staged.is_file()
    assert b"secret" not in staged.staged.read_bytes()

    store.restore_staged_deletion(staged)

    assert path.is_file()
    assert store.read(secret_id).get_secret_value() == "secret"


def test_v1_settings_migrate_and_unsupported_products_are_rejected(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / ".learnnest" / "providers" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        '{"schema_version":"1.0","connections":{}}', encoding="utf-8"
    )

    assert load_settings(tmp_path).schema_version == "2.0"
    for preset in ("openai", "anthropic", "gemini", "coding-plan", "openai-compatible"):
        with pytest.raises(ValueError, match="unsupported provider"):
            connect(tmp_path, name=f"bad-{preset}", preset=preset, secret_value="x")


def test_role_snapshot_is_secret_free_immutable_and_invalidates_old_authorization(
    tmp_path: Path,
) -> None:
    first = connect(tmp_path, name="mimo-note", preset="mimo", secret_value="one")
    set_role_binding(tmp_path, role="note_writer", connection_name=first.name)
    set_role_binding(tmp_path, role="note_reviewer", connection_name=first.name)
    frozen = freeze_role_bindings(tmp_path)
    task = create_task(
        task_id="20260802-snapshot",
        source_path="C:/private/lesson.mp4",
        source_fingerprint="snapshot",
        title="课程",
        provider_bindings=frozen,
    )
    old_sha = task.provider_settings_sha256

    updated = connect(tmp_path, name="mimo-note", preset="mimo", secret_value="two")
    set_role_binding(tmp_path, role="note_writer", connection_name=updated.name)
    current = load_settings(tmp_path)

    assert {
        role: binding.model_dump(mode="json")
        for role, binding in task.provider_bindings.items()
    } == {role: binding.model_dump(mode="json") for role, binding in frozen.items()}
    assert "one" not in task.model_dump_json()
    assert "two" not in task.model_dump_json()
    assert task.provider_bindings["note_writer"].secret_id != updated.secret_id
    assert old_sha != settings_sha256(current)


def test_role_factories_use_only_frozen_endpoint_and_role_secret(
    tmp_path: Path,
) -> None:
    writer = connect(
        tmp_path,
        name="mimo-writer",
        preset="mimo",
        secret_value="same-key",
        endpoint="https://writer.example/v1",
    )
    reviewer = connect(
        tmp_path,
        name="deepseek-reviewer",
        preset="deepseek",
        secret_value="same-key",
        endpoint="https://reviewer.example/v1",
    )
    podcast = connect(
        tmp_path,
        name="deepseek-podcast",
        preset="deepseek",
        secret_value="same-key",
        endpoint="https://podcast.example/v1",
    )
    tts = connect(
        tmp_path,
        name="mimo-tts",
        preset="mimo-tts",
        secret_value="same-key",
        endpoint="https://tts.example/v1",
    )
    set_role_binding(tmp_path, role="note_writer", connection_name=writer.name)
    set_role_binding(tmp_path, role="note_reviewer", connection_name=reviewer.name)
    set_role_binding(tmp_path, role="podcast", connection_name=podcast.name)
    set_role_binding(tmp_path, role="tts", connection_name=tts.name)
    frozen = freeze_role_bindings(tmp_path)

    calls: list[dict[str, object]] = []

    def client_factory(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    note_writer = assisted_provider_from_snapshot(
        tmp_path, frozen["note_writer"], client_factory=client_factory
    )
    note_reviewer = assisted_provider_from_snapshot(
        tmp_path, frozen["note_reviewer"], client_factory=client_factory
    )
    podcast_provider = podcast_provider_from_snapshot(
        tmp_path, frozen["podcast"], client_factory=client_factory
    )
    tts_provider = tts_provider_from_snapshot(
        tmp_path, frozen["tts"], client_factory=client_factory
    )

    assert writer.secret_id != reviewer.secret_id != podcast.secret_id != tts.secret_id
    assert note_writer.endpoint_identity == "https://writer.example/v1"
    assert note_reviewer.endpoint_identity == "https://reviewer.example/v1"
    assert podcast_provider.name == "deepseek"
    assert podcast_provider.model == "deepseek-v4-pro"
    assert tts_provider.name == "xiaomi-mimo-tts"
    assert tts_provider.model == "mimo-v2.5-tts"
    assert [call["base_url"] for call in calls] == [
        "https://writer.example/v1",
        "https://reviewer.example/v1",
        "https://podcast.example/v1",
        "https://tts.example/v1",
    ]


def test_role_factories_call_only_the_bound_fake_transport_without_fallback(
    tmp_path: Path,
) -> None:
    writer = connect(
        tmp_path,
        name="mimo-writer",
        preset="mimo",
        secret_value="writer-key",
    )
    reviewer = connect(
        tmp_path,
        name="deepseek-reviewer",
        preset="deepseek",
        secret_value="reviewer-key",
    )
    podcast = connect(
        tmp_path,
        name="deepseek-podcast",
        preset="deepseek",
        secret_value="podcast-key",
    )
    tts = connect(
        tmp_path,
        name="mimo-tts",
        preset="mimo-tts",
        secret_value="tts-key",
    )
    set_role_binding(tmp_path, role="note_writer", connection_name=writer.name)
    set_role_binding(tmp_path, role="note_reviewer", connection_name=reviewer.name)
    set_role_binding(tmp_path, role="podcast", connection_name=podcast.name)
    set_role_binding(tmp_path, role="tts", connection_name=tts.name)
    frozen = freeze_role_bindings(tmp_path)
    requests: list[tuple[str, str]] = []

    def client_factory(**kwargs: object) -> object:
        endpoint = str(kwargs["base_url"])

        class Completions:
            def create(self, **request: object) -> object:
                requests.append((endpoint, str(request["model"])))
                if endpoint == "https://api.xiaomimimo.com/v1" and request.get("audio"):
                    message = type(
                        "Message",
                        (),
                        {"audio": {"data": base64.b64encode(b"wav").decode()}},
                    )()
                else:
                    message = type(
                        "Message", (), {"content": '{"schema_version":"1.0"}'}
                    )()
                return type(
                    "Response",
                    (),
                    {"choices": [type("Choice", (), {"message": message})()]},
                )()

        return type(
            "Client",
            (),
            {"chat": type("Chat", (), {"completions": Completions()})()},
        )()

    assisted_provider_from_snapshot(
        tmp_path, frozen["note_writer"], client_factory=client_factory
    ).write_markdown("{}")
    assisted_provider_from_snapshot(
        tmp_path, frozen["note_reviewer"], client_factory=client_factory
    ).review_markdown("{}", "# Candidate")
    podcast_provider_from_snapshot(
        tmp_path, frozen["podcast"], client_factory=client_factory
    ).generate("{}", ())
    tts_provider_from_snapshot(
        tmp_path, frozen["tts"], client_factory=client_factory
    ).synthesize("speech", "style")

    assert requests == [
        ("https://api.xiaomimimo.com/v1", "mimo-v2.5"),
        ("https://api.deepseek.com/v1", "deepseek-v4-pro"),
        ("https://api.deepseek.com/v1", "deepseek-v4-pro"),
        ("https://api.xiaomimimo.com/v1", "mimo-v2.5-tts"),
    ]

    def failing_factory(**_: object) -> object:
        class Completions:
            def create(self, **__: object) -> object:
                raise RuntimeError("MiMo unavailable")

        return type(
            "Client",
            (),
            {"chat": type("Chat", (), {"completions": Completions()})()},
        )()

    with pytest.raises(NoteProviderError, match="assisted Writer failed"):
        assisted_provider_from_snapshot(
            tmp_path, frozen["note_writer"], client_factory=failing_factory
        ).write_markdown("{}")
    assert requests[-1] == ("https://api.xiaomimimo.com/v1", "mimo-v2.5-tts")


def test_role_and_global_caps_block_before_fake_provider_invocation(
    tmp_path: Path,
) -> None:
    settings = ProviderSettings(
        global_calls_per_day=1,
        budget_group_calls_per_day={
            "note": 0,
            "podcast": 1,
            "tts": 1,
            "asr": 1,
            "ocr": 1,
        },
    )

    assert settings.call_allowed("note_writer", used_global=0, used_group=0) is False
    assert settings.call_allowed("note_reviewer", used_global=1, used_group=0) is False


def test_connection_check_cap_blocks_before_client_construction(tmp_path: Path) -> None:
    connect(tmp_path, name="mimo", preset="mimo", secret_value="check-secret")
    settings = load_settings(tmp_path).model_copy(update={"global_calls_per_day": 0})
    save_settings(tmp_path, ProviderSettings.model_validate(settings.model_dump()))
    calls = 0

    def client_factory(**_: object) -> object:
        nonlocal calls
        calls += 1
        return object()

    with pytest.raises(ProviderConnectionCheckError, match="limit is exhausted"):
        run_synthetic_llm_check(str(tmp_path), "mimo", client_factory=client_factory)
    assert calls == 0


@pytest.mark.parametrize("stage", ["writer", "reviewer", "podcast", "tts"])
def test_every_direct_paid_stage_is_admitted_before_its_factory_at_zero_cap(
    tmp_path: Path, stage: str
) -> None:
    update = load_settings(tmp_path).model_copy(update={"global_calls_per_day": 0})
    save_settings(tmp_path, ProviderSettings.model_validate(update.model_dump()))
    constructions = 0

    def invoke() -> None:
        nonlocal constructions
        constructions += 1

    with pytest.raises(ValueError, match="provider call limit is exhausted"):
        execute_direct_provider_call(tmp_path, "task-a", stage, invoke)  # type: ignore[arg-type]
    assert constructions == 0


def test_direct_unknown_call_is_charged_and_blocks_the_shared_automation_ledger(
    tmp_path: Path,
) -> None:
    settings = load_settings(tmp_path).model_copy(
        update={"global_calls_per_day": 1, "retries_per_role": 0}
    )
    save_settings(tmp_path, ProviderSettings.model_validate(settings.model_dump()))
    calls = 0

    def timed_out() -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("provider timed out")

    with pytest.raises(TimeoutError):
        execute_direct_provider_call(tmp_path, "task-a", "writer", timed_out)
    with pytest.raises(ValueError, match="provider call limit is exhausted"):
        execute_direct_provider_call(
            tmp_path,
            "task-b",
            "podcast",
            lambda: (_ for _ in ()).throw(AssertionError()),
        )
    assert calls == 1


def test_concurrent_direct_admission_does_not_exceed_one_cap(tmp_path: Path) -> None:
    settings = load_settings(tmp_path).model_copy(
        update={"global_calls_per_day": 1, "retries_per_role": 0}
    )
    save_settings(tmp_path, ProviderSettings.model_validate(settings.model_dump()))
    calls = 0

    def invoke(task_id: str) -> str:
        nonlocal calls

        def factory() -> None:
            nonlocal calls
            calls += 1

        try:
            execute_direct_provider_call(tmp_path, task_id, "writer", factory)
        except ValueError:
            return "blocked"
        return "called"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(invoke, ("a", "b")))

    assert sorted(outcomes) == ["blocked", "called"]
    assert calls == 1


def test_local_asr_and_ocr_bindings_freeze_without_secrets(tmp_path: Path) -> None:
    asr = connect(tmp_path, name="asr-local", preset="local-asr")
    ocr = connect(tmp_path, name="ocr-local", preset="local-ocr")
    set_role_binding(tmp_path, role="asr", connection_name=asr.name)
    set_role_binding(tmp_path, role="ocr", connection_name=ocr.name)

    frozen = freeze_role_bindings(tmp_path)

    assert frozen["asr"].capability == "asr"
    assert frozen["ocr"].capability == "ocr"
    assert frozen["asr"].secret_id is None
    assert frozen["ocr"].secret_id is None
    assert frozen["asr"].endpoint == "local://asr"
    assert frozen["ocr"].endpoint == "local://ocr"


def test_windows_tts_voice_is_secret_free_public_and_frozen_exactly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.provider_profiles as profiles

    monkeypatch.setattr(profiles, "default_windows_tts_voice", lambda: "Huihui Desktop")
    windows = connect(tmp_path, name="windows-voice", preset="windows-tts")
    set_role_binding(tmp_path, role="tts", connection_name=windows.name)

    frozen = freeze_role_bindings(tmp_path)
    public = profiles.public_settings(tmp_path)

    assert windows.secret_id is None
    assert frozen["tts"].provider == "windows-tts"
    assert frozen["tts"].endpoint == "local://windows-tts/Huihui%20Desktop"
    assert public["connections"][0]["configured"] is True
    assert public["connections"][0]["voice"] == "Huihui Desktop"
    assert public["connections"][0]["secret_status"] == "local_configured"


def test_mimo_and_deepseek_connections_have_no_fallback_or_shared_secret_object(
    tmp_path: Path,
) -> None:
    mimo = connect(tmp_path, name="mimo", preset="mimo", secret_value="same-key")
    deepseek = connect(
        tmp_path, name="deepseek", preset="deepseek", secret_value="same-key"
    )

    assert mimo.provider == "xiaomi-mimo"
    assert deepseek.provider == "deepseek"
    assert mimo.secret_id != deepseek.secret_id
    assert get_connection(tmp_path, "mimo").provider == "xiaomi-mimo"
    assert get_connection(tmp_path, "deepseek").provider == "deepseek"


def test_incompatible_same_name_connection_update_is_rejected_before_secret_or_settings_write(
    tmp_path: Path,
) -> None:
    original = connect(
        tmp_path, name="shared", preset="mimo-tts", secret_value="old-secret"
    )
    set_role_binding(tmp_path, role="tts", connection_name="shared")
    settings_path = tmp_path / ".learnnest" / "providers" / "settings.json"
    secrets_dir = settings_path.parent / "secrets"
    before_settings = settings_path.read_bytes()
    before_secret_ids = sorted(path.name for path in secrets_dir.glob("*.dpapi"))

    with pytest.raises(ValueError, match="provider connection update is incompatible"):
        connect(tmp_path, name="shared", preset="mimo", secret_value="new-secret")

    assert settings_path.read_bytes() == before_settings
    assert (
        sorted(path.name for path in secrets_dir.glob("*.dpapi")) == before_secret_ids
    )
    assert get_connection(tmp_path, "shared").secret_id == original.secret_id
    assert load_settings(tmp_path).role_bindings["tts"].connection_id == "shared"


def test_delete_connection_removes_unbound_local_and_cloud_connections(
    tmp_path: Path,
) -> None:
    cloud = connect(tmp_path, name="mimo", preset="mimo", secret_value="secret")
    connect(tmp_path, name="local-asr", preset="local-asr")
    before_sha = settings_sha256(load_settings(tmp_path))
    assert cloud.secret_id is not None
    secret_path = ProviderSecretStore(tmp_path).path_for(cloud.secret_id)

    delete_connection(tmp_path, name="mimo")
    after_cloud_delete = load_settings(tmp_path)
    delete_connection(tmp_path, name="local-asr")

    assert "mimo" not in after_cloud_delete.connections
    assert not secret_path.exists()
    assert before_sha != settings_sha256(after_cloud_delete)
    assert load_settings(tmp_path).connections == {}


def test_delete_connection_rejects_missing_or_bound_connections_without_mutation(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path, name="mimo", preset="mimo", secret_value="secret")
    set_role_binding(tmp_path, role="note_writer", connection_name=connection.name)
    before = load_settings(tmp_path).model_dump(mode="json")
    assert connection.secret_id is not None
    secret_path = ProviderSecretStore(tmp_path).path_for(connection.secret_id)

    with pytest.raises(ProviderConnectionBoundError) as bound:
        delete_connection(tmp_path, name="mimo")
    with pytest.raises(ProviderConnectionNotFoundError):
        delete_connection(tmp_path, name="missing")

    assert bound.value.roles == ("note_writer",)
    assert load_settings(tmp_path).model_dump(mode="json") == before
    assert secret_path.is_file()


def test_clear_role_binding_keeps_connection_and_changes_settings_sha(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path, name="mimo", preset="mimo", secret_value="secret")
    set_role_binding(tmp_path, role="note_writer", connection_name=connection.name)
    before_sha = settings_sha256(load_settings(tmp_path))
    assert connection.secret_id is not None
    secret_path = ProviderSecretStore(tmp_path).path_for(connection.secret_id)

    clear_role_binding(tmp_path, role="note_writer")

    settings = load_settings(tmp_path)
    assert settings.role_bindings == {}
    assert settings.connections[connection.name] == connection
    assert secret_path.is_file()
    assert before_sha != settings_sha256(settings)
    with pytest.raises(ProviderRoleNotBoundError):
        clear_role_binding(tmp_path, role="note_writer")


def test_delete_connection_rolls_back_when_setting_or_secret_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import learnnest.provider_profiles as profiles

    connection = connect(tmp_path, name="mimo", preset="mimo", secret_value="secret")
    assert connection.secret_id is not None
    secret_path = ProviderSecretStore(tmp_path).path_for(connection.secret_id)

    monkeypatch.setattr(
        profiles,
        "save_settings",
        lambda *_: (_ for _ in ()).throw(OSError("write failed")),
    )
    with pytest.raises(ValueError, match="cannot be deleted"):
        delete_connection(tmp_path, name="mimo")
    assert "mimo" in load_settings(tmp_path).connections
    assert secret_path.is_file()

    monkeypatch.undo()
    monkeypatch.setattr(
        ProviderSecretStore,
        "discard_staged_deletion",
        lambda *_: (_ for _ in ()).throw(SecretStoreError("delete failed")),
    )
    with pytest.raises(ValueError, match="cannot be deleted"):
        delete_connection(tmp_path, name="mimo")
    assert "mimo" in load_settings(tmp_path).connections
    assert secret_path.is_file()


def test_note_and_podcast_cannot_share_connection_or_secret_even_if_json_is_tampered(
    tmp_path: Path,
) -> None:
    note = connect(tmp_path, name="note", preset="mimo", secret_value="same-key")
    podcast = connect(
        tmp_path, name="podcast", preset="deepseek", secret_value="same-key"
    )
    set_role_binding(tmp_path, role="note_writer", connection_name=note.name)
    set_role_binding(tmp_path, role="note_reviewer", connection_name=note.name)

    with pytest.raises(ValueError, match="provider role binding is incompatible"):
        set_role_binding(tmp_path, role="podcast", connection_name=note.name)

    set_role_binding(tmp_path, role="podcast", connection_name=podcast.name)
    settings_path = tmp_path / ".learnnest" / "providers" / "settings.json"
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    payload["connections"]["podcast"]["secret_id"] = payload["connections"]["note"][
        "secret_id"
    ]
    settings_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="provider settings are missing or invalid"):
        load_settings(tmp_path)


def test_webui_podcast_options_exclude_note_domain_connections_and_explain_isolation(
    tmp_path: Path,
) -> None:
    note = connect(tmp_path, name="mimo-note", preset="mimo", secret_value="same-key")
    set_role_binding(tmp_path, role="note_writer", connection_name=note.name)
    set_role_binding(tmp_path, role="note_reviewer", connection_name=note.name)
    client = TestClient(create_web_app(tmp_path))

    response = client.get(
        "/api/providers/settings?default_output=complete_note_with_audio"
    )
    podcast_role = next(
        role
        for role in response.json()["readiness"]["required_roles"]
        if role["name"] == "播客"
    )

    assert response.status_code == 200
    assert podcast_role["options"] == []
    assert (
        podcast_role["hint"] == "播客需要单独的 MiMo/DeepSeek 连接，不能复用笔记连接。"
    )

    connect(
        tmp_path,
        name="mimo-podcast",
        preset="mimo",
        secret_value="same-key",
    )
    response = client.get(
        "/api/providers/settings?default_output=complete_note_with_audio"
    )
    podcast_role = next(
        role
        for role in response.json()["readiness"]["required_roles"]
        if role["name"] == "播客"
    )

    assert podcast_role["options"] == [{"name": "mimo-podcast", "provider": "MiMo"}]
    assert podcast_role["hint"] is None


def test_webui_settings_page_and_api_expose_selected_readiness_but_never_a_key(
    tmp_path: Path,
) -> None:
    connect(tmp_path, name="mimo", preset="mimo", secret_value="never-show-this")
    set_role_binding(tmp_path, role="note_writer", connection_name="mimo")
    client = TestClient(create_web_app(tmp_path))

    page = client.get("/")
    settings = client.get("/api/providers/settings")

    assert page.status_code == 200
    assert "所选结果的准备情况" in page.text
    assert "never-show-this" not in page.text
    assert settings.status_code == 200
    assert settings.json()["readiness"]["state"] == "等待设置"
    assert "never-show-this" not in settings.text

    updated = client.put(
        "/api/providers/limits",
        json={
            "retries_per_role": 0,
            "global_calls_per_day": 2,
            "budget_group_calls_per_day": {
                "note": 1,
                "podcast": 1,
                "tts": 1,
                "asr": 1,
                "ocr": 1,
            },
        },
    )

    assert updated.status_code == 200
    assert load_settings(tmp_path).retries_per_role == 0
    assert load_settings(tmp_path).global_calls_per_day == 2
    assert load_settings(tmp_path).budget_group_calls_per_day["note"] == 1
    assert "secret_id" not in updated.text


@pytest.mark.parametrize(
    ("default_output", "expected_roles", "expected_states"),
    [
        (
            "complete_note",
            ["笔记 Writer", "笔记 Reviewer"],
            ["连接配置可读取", "连接配置可读取"],
        ),
        (
            "complete_note_with_audio",
            ["笔记 Writer", "笔记 Reviewer", "播客", "TTS"],
            [
                "连接配置可读取",
                "连接配置可读取",
                "连接配置可读取",
                "本地配置可读取",
            ],
        ),
    ],
)
def test_webui_projects_only_selected_output_readiness_without_exposing_internal_settings(
    tmp_path: Path,
    default_output: str,
    expected_roles: list[str],
    expected_states: list[str],
) -> None:
    note = connect(tmp_path, name="note", preset="mimo", secret_value="never-show")
    podcast = connect(
        tmp_path, name="podcast", preset="deepseek", secret_value="never-show"
    )
    voice = connect(tmp_path, name="voice", preset="windows-tts")
    for role, connection in (
        ("note_writer", note),
        ("note_reviewer", note),
        ("podcast", podcast),
        ("tts", voice),
    ):
        set_role_binding(tmp_path, role=role, connection_name=connection.name)
    connect(tmp_path, name="legacy-asr", preset="local-asr")
    connect(tmp_path, name="legacy-ocr", preset="local-ocr")
    client = TestClient(create_web_app(tmp_path))

    configured = client.post(
        "/api/automation/configure",
        json={
            "default_output": default_output,
            "auto_organize_new_favorites": False,
            "check_interval_seconds": 300,
            "max_items_per_tick": 1,
        },
    )
    response = client.get("/api/providers/settings")

    assert configured.status_code == 200
    assert response.status_code == 200
    readiness = response.json()["readiness"]
    assert readiness["default_output"] in {"完整笔记", "完整笔记和播客音频"}
    assert [role["name"] for role in readiness["required_roles"]] == expected_roles
    assert [role["state"] for role in readiness["required_roles"]] == expected_states
    assert readiness["authorization"]["state"] == "等待授权"
    assert "settings_sha256" not in response.text
    assert "never-show" not in response.text


def test_webui_hides_unsupported_input_and_budget_controls_but_keeps_existing_settings(
    tmp_path: Path,
) -> None:
    connect(tmp_path, name="legacy-asr", preset="local-asr")
    connect(tmp_path, name="legacy-ocr", preset="local-ocr")
    before = load_settings(tmp_path).model_dump(mode="json")
    client = TestClient(create_web_app(tmp_path))

    page = client.get("/")

    assert page.status_code == 200
    assert "local-asr" not in page.text
    assert "local-ocr" not in page.text
    assert "每日调用上限" not in page.text
    assert 'id="provider-role-form"' not in page.text
    assert load_settings(tmp_path).model_dump(mode="json") == before


def test_webui_local_connections_save_without_api_keys(tmp_path: Path) -> None:
    client = TestClient(create_web_app(tmp_path))

    for name, preset in (
        ("local-asr", "local-asr"),
        ("local-ocr", "local-ocr"),
        ("windows-tts", "windows-tts"),
    ):
        response = client.post(
            "/api/providers/connections",
            json={"name": name, "preset": preset, "voice": "Microsoft Huihui"}
            if preset == "windows-tts"
            else {"name": name, "preset": preset},
        )

        assert response.status_code == 200

    settings = client.get("/api/providers/settings").json()

    assert {item["name"] for item in settings["connections"]} == {"windows-tts"}
    assert settings["connections"][0]["state"] == "本地配置可读取"
    assert {item.name for item in load_settings(tmp_path).connections.values()} == {
        "local-asr",
        "local-ocr",
        "windows-tts",
    }


def test_webui_deletes_only_unbound_connections_without_exposing_secrets(
    tmp_path: Path,
) -> None:
    cloud = connect(tmp_path, name="mimo", preset="mimo", secret_value="never-show")
    connect(tmp_path, name="local-asr", preset="local-asr")
    client = TestClient(create_web_app(tmp_path))
    assert cloud.secret_id is not None
    secret_path = ProviderSecretStore(tmp_path).path_for(cloud.secret_id)

    deleted = client.delete("/api/providers/connections/mimo")
    deleted_local = client.delete("/api/providers/connections/local-asr")
    missing = client.delete("/api/providers/connections/missing")

    assert deleted.status_code == 200
    assert deleted_local.status_code == 200
    assert deleted.json()["connections"] == []
    assert not secret_path.exists()
    assert missing.status_code == 404
    assert "never-show" not in deleted.text + missing.text


def test_webui_rejects_deleting_a_bound_connection_with_role_guidance(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path, name="mimo", preset="mimo", secret_value="secret")
    set_role_binding(tmp_path, role="note_writer", connection_name=connection.name)
    client = TestClient(create_web_app(tmp_path))

    response = client.delete("/api/providers/connections/mimo")

    assert response.status_code == 409
    assert "笔记 Writer" in response.text
    assert "secret" not in response.text
    assert "mimo" in load_settings(tmp_path).connections


def test_webui_unbinds_a_role_without_deleting_its_connection(tmp_path: Path) -> None:
    secret = "clear-role-secret-never-show"
    connection = connect(tmp_path, name="mimo", preset="mimo", secret_value=secret)
    set_role_binding(tmp_path, role="note_writer", connection_name=connection.name)
    client = TestClient(create_web_app(tmp_path))

    cleared = client.delete("/api/providers/roles/note_writer")
    missing = client.delete("/api/providers/roles/note_writer")

    assert cleared.status_code == 200
    assert load_settings(tmp_path).role_bindings == {}
    assert "mimo" in {item["name"] for item in cleared.json()["connections"]}
    assert missing.status_code == 404
    assert secret not in cleared.text + missing.text


def test_webui_renders_only_selected_role_readiness_with_inline_feedback(
    tmp_path: Path,
) -> None:
    connect(tmp_path, name="mimo", preset="mimo", secret_value="never-show-this")
    set_role_binding(tmp_path, role="note_writer", connection_name="mimo")
    client = TestClient(create_web_app(tmp_path))

    page = client.get("/").text
    script = (web_app._STATIC_DIRECTORY / "workspace.js").read_text(encoding="utf-8")

    assert load_settings(tmp_path).role_bindings == {
        "note_writer": load_settings(tmp_path).role_bindings["note_writer"]
    }
    assert 'id="setup-readiness"' in page
    assert 'id="provider-feedback"' in page
    assert "provider-role-summary" in script
    assert "已绑定" in script
    assert '"windows-tts": "windows-tts"' in script
    assert "function startProviderSave(event)" in script
    assert script.count("const finishSaving = startProviderSave(event);") == 1
    assert 'const current = await api("/api/providers/settings");' not in script
    assert 'data-delete-connection="${escapeHtml(item.name)}"' in script
    assert "window.confirm" in script
    assert "await loadAutomationStatus();" in script
    assert 'data-unbind-setup-role="${escapeHtml(role.name)}"' in script
    assert "async function clearSetupRole(button)" in script
    assert "const submittedForm = event.currentTarget;" in script
    assert "submittedForm.reset();" in script
    assert "event.currentTarget.reset();" not in script
    assert "每日调用上限" not in page


def test_legacy_automation_budget_still_maps_to_global_cap() -> None:
    budget = AutomationBudget.model_validate(
        {"writer_per_day": 2, "reviewer_per_day": 3}
    )

    assert budget.provider_calls_per_day == 2


def test_provider_setting_change_invalidates_automatic_authorization_before_calls(
    tmp_path: Path,
) -> None:
    snapshot = AssistedConnectionSnapshot(
        connection_name="legacy",
        provider="example",
        endpoint_identity="https://example.test/v1",
        model="model",
        adapter_revision="1",
    )
    save_policy(
        tmp_path,
        AutomationPolicy(schedule_id="manual", writer=snapshot, reviewer=snapshot),
    )
    connect(tmp_path, name="mimo", preset="mimo", secret_value="changed")
    authorize(tmp_path, now=datetime(2026, 8, 2, tzinfo=UTC))
    delete_connection(tmp_path, name="mimo")

    providers = AutomationProviders(
        writer=object(),
        reviewer=object(),
        podcast=object(),
        tts=object(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="authorization is invalid"):
        run_automation_tasks(tmp_path, ["would-call"], providers)


def test_retry_or_any_budget_group_change_invalidates_automatic_authorization(
    tmp_path: Path,
) -> None:
    snapshot = AssistedConnectionSnapshot(
        connection_name="legacy",
        provider="example",
        endpoint_identity="https://example.test/v1",
        model="model",
        adapter_revision="1",
    )
    save_policy(
        tmp_path,
        AutomationPolicy(schedule_id="manual", writer=snapshot, reviewer=snapshot),
    )
    authorized = authorize(tmp_path, now=datetime(2026, 8, 2, tzinfo=UTC))
    update_limits(
        tmp_path,
        retries_per_role=0,
        global_calls_per_day=80,
        budget_group_calls_per_day={
            "note": 20,
            "podcast": 20,
            "tts": 20,
            "asr": 20,
            "ocr": 19,
        },
    )
    providers = AutomationProviders(
        writer=object(),
        reviewer=object(),
        podcast=object(),
        tts=object(),  # type: ignore[arg-type]
    )

    assert authorized.policy.provider_settings_sha256 != settings_sha256(
        load_settings(tmp_path)
    )
    with pytest.raises(ValueError, match="authorization is invalid"):
        run_automation_tasks(tmp_path, ["would-call"], providers)


def test_settings_join_the_live_refresh_without_repainting_active_forms() -> None:
    script = (web_app._STATIC_DIRECTORY / "workspace.js").read_text(encoding="utf-8")

    assert (
        "Promise.all([loadFavorites(), refreshProviderSettingsWhenIdle(), "
        "loadStorageStatus(true)])"
    ) in script
    assert "function settingsFormNeedsProtection(form)" in script


def test_synthetic_check_persists_one_safe_attempt_and_never_retries(
    tmp_path: Path,
) -> None:
    connect(tmp_path, name="mimo", preset="mimo", secret_value="check-secret")
    calls = 0

    def client_factory(**_: object) -> object:
        class Completions:
            def create(self, **__: object) -> object:
                nonlocal calls
                calls += 1
                return type("Response", (), {"choices": [object()]})()

        return type(
            "Client",
            (),
            {"chat": type("Chat", (), {"completions": Completions()})()},
        )()

    assert (
        run_synthetic_llm_check(str(tmp_path), "mimo", client_factory=client_factory)
        == "completed"
    )
    with pytest.raises(ProviderConnectionCheckError, match="already attempted"):
        run_synthetic_llm_check(str(tmp_path), "mimo", client_factory=client_factory)

    assert calls == 1
    stored = next(
        (tmp_path / ".learnnest" / "automation" / "tasks").glob("*/*.json")
    ).read_text(encoding="utf-8")
    assert "check-secret" not in stored
    assert '"status": "completed"' in stored


def test_admitted_assisted_provider_maps_reviewer_dossier_and_rejects_unknown_or_repeat(
    tmp_path: Path,
) -> None:
    dossier = '{"task":"review-b"}'
    dossier_sha256 = hashlib.sha256(dossier.encode("utf-8")).hexdigest()
    constructions = 0

    class FakeProvider:
        def review_markdown(self, dossier_json: str, candidate_markdown: str) -> str:
            assert dossier_json == dossier
            assert candidate_markdown == "# candidate"
            return "# reviewed"

    def factory() -> FakeProvider:
        nonlocal constructions
        constructions += 1
        return FakeProvider()

    provider = AdmittedAssistedProvider(
        str(tmp_path),
        {dossier_sha256: "task-b"},
        "reviewer",
        name="fake",
        model="fake-model",
        endpoint_identity="https://example.test/v1",
        provider_factory=factory,
    )

    with pytest.raises(ValueError, match="unknown dossier"):
        provider.review_markdown('{"task":"unknown"}', "# candidate")
    assert constructions == 0
    assert provider.review_markdown(dossier, "# candidate") == "# reviewed"
    with pytest.raises(ValueError, match="consume a dossier twice"):
        provider.review_markdown(dossier, "# candidate")

    fact = load_task_state(
        tmp_path, "manual-reviewer-task-b", settings_sha256(load_settings(tmp_path))
    )
    assert constructions == 1
    assert fact is not None
    assert fact.attempts[0].status == "completed"
