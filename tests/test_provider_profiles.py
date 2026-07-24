from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from learnnest.provider_profiles import (
    check_capability,
    connect,
    get_connection,
    load_settings,
    profile_for_connection,
    set_authorization,
)


def _draft() -> str:
    return json.dumps(
        {"schema_version": "2.0", "title": "capability check", "sections": []}
    )


def test_check_uses_bounded_order_and_persists_no_payloads(tmp_path: Path) -> None:
    connection = connect(
        tmp_path,
        name="main",
        preset="openai-compatible",
        endpoint="https://example.test/v1/",
        model="model-a",
        secret_env="TEST_KEY",
        now=datetime(2026, 7, 24, tzinfo=UTC),
    )
    calls: list[str] = []

    def invoke(strategy: str) -> str:
        calls.append(strategy)
        if strategy != "json_object":
            raise RuntimeError("response_format unsupported")
        return _draft()

    profile = check_capability(
        tmp_path, connection, invoke, now=datetime(2026, 7, 24, tzinfo=UTC)
    )

    assert calls == ["native_json_schema", "tool_call", "json_object"]
    assert profile.status == "verified"
    assert profile.strategy == "json_object"
    assert profile.extractor == "message_content"
    stored = next(
        (tmp_path / ".learnnest" / "providers" / "profiles").glob("*.json")
    ).read_text(encoding="utf-8")
    assert "capability check" not in stored
    assert "TEST_KEY" not in stored


def test_check_stops_after_four_failures_and_records_safe_categories(
    tmp_path: Path,
) -> None:
    connection = connect(
        tmp_path, name="main", preset="mimo", now=datetime(2026, 7, 24, tzinfo=UTC)
    )
    profile = check_capability(
        tmp_path,
        connection,
        lambda strategy: "not json",
        now=datetime(2026, 7, 24, tzinfo=UTC),
    )

    assert profile.status == "failed"
    assert len(profile.attempts) == 4
    assert {attempt.failure_class for attempt in profile.attempts} == {
        "invalid_contract"
    }


def test_connection_change_invalidates_old_profile_and_authorization_is_local(
    tmp_path: Path,
) -> None:
    connection = connect(
        tmp_path, name="main", preset="mimo", now=datetime(2026, 7, 24, tzinfo=UTC)
    )
    check_capability(
        tmp_path,
        connection,
        lambda strategy: _draft(),
        now=datetime(2026, 7, 24, tzinfo=UTC),
    )
    assert load_settings(tmp_path).authorization == "local_only"
    set_authorization(tmp_path, "automatic")
    assert load_settings(tmp_path).authorization == "automatic"

    changed = connect(
        tmp_path,
        name="main",
        preset="mimo",
        model="mimo-v2.5-alt",
        now=datetime(2026, 7, 24, tzinfo=UTC),
    )

    assert profile_for_connection(tmp_path, changed) is None
    assert get_connection(tmp_path).model == "mimo-v2.5-alt"


def test_generic_connection_requires_explicit_identity(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires endpoint"):
        connect(tmp_path, name="bad", preset="openai-compatible")


def test_generic_connection_can_keep_a_stable_provider_name(tmp_path: Path) -> None:
    connection = connect(
        tmp_path,
        name="coding-plan",
        preset="openai-compatible",
        endpoint="https://example.test/v1",
        model="model-a",
        secret_env="CODING_PLAN_KEY",
        provider_name="volcengine-coding-plan",
    )

    assert connection.provider == "volcengine-coding-plan"
