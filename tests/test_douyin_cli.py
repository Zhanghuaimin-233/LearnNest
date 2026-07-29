from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from learnnest.cli import _load_douyin_cookie, _schedule_adapter, app
from learnnest.download_queue import DownloadOutcome
from learnnest.douyin_cookie_store import DouyinCookieStore
from learnnest.schedule_models import ScheduleRecord
from learnnest.schedules import ScheduleOutcome


def _schedule(*, folder_ids: list[str] | None = None) -> ScheduleRecord:
    return ScheduleRecord(
        schedule_id="douyin-favorites",
        status="enabled",
        source={
            "kind": "douyin",
            "url": "https://www.douyin.com/user/example/favorite",
            "folder_ids": folder_ids,
        },
        trigger={"kind": "manual"},
        created_at=datetime(2026, 7, 19, tzinfo=UTC),
        updated_at=datetime(2026, 7, 19, tzinfo=UTC),
    )


def test_load_douyin_cookie_accepts_raw_single_line_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOUYIN_COOKIE", raising=False)
    secret = "sessionid=runtime-only; msToken=opaque"
    (tmp_path / ".env").write_text(secret, encoding="utf-8")

    loaded = _load_douyin_cookie()

    assert loaded == SecretStr(secret)


def test_load_douyin_cookie_prefers_runtime_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOUYIN_COOKIE", "from-process")
    (tmp_path / ".env").write_text("from-file", encoding="utf-8")

    loaded = _load_douyin_cookie()

    assert loaded == SecretStr("from-process")


def test_load_douyin_cookie_accepts_key_value_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOUYIN_COOKIE", raising=False)
    (tmp_path / ".env").write_text(
        "MIMO_API_KEY=not-used\nDOUYIN_COOKIE='from-key'\n",
        encoding="utf-8",
    )

    loaded = _load_douyin_cookie()

    assert loaded == SecretStr("from-key")


def test_encrypted_output_root_cookie_precedes_legacy_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOUYIN_COOKIE", "legacy-env-cookie")
    output_root = tmp_path / "vault"
    encrypted = SecretStr("sessionid=encrypted-output-root")
    DouyinCookieStore(output_root).save(encrypted)

    loaded = _load_douyin_cookie(output_root)

    assert loaded == encrypted


def test_output_root_without_encrypted_cookie_falls_back_to_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOUYIN_COOKIE", "legacy-env-cookie")

    loaded = _load_douyin_cookie(tmp_path / "vault")

    assert loaded == SecretStr("legacy-env-cookie")


def test_schedule_adapter_defaults_to_verified_video_scope() -> None:
    adapter = _schedule_adapter(_schedule(), SecretStr("runtime-cookie"))

    assert adapter.folder_ids == ()
    assert adapter.include_default_video is True
    assert adapter.transport.cookie == SecretStr("runtime-cookie")


def test_schedule_adapter_rejects_unverified_custom_folder_scope() -> None:
    with pytest.raises(RuntimeError, match="custom Douyin folder"):
        _schedule_adapter(_schedule(folder_ids=["folder-1"]), SecretStr("cookie"))


def test_download_pending_passes_runtime_cookie_to_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    secret = "sessionid=runtime-only"
    (tmp_path / ".env").write_text(secret, encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run_pending_downloads(root, **kwargs):
        captured["root"] = root
        captured.update(kwargs)
        return DownloadOutcome(None, 0, 0, 0)

    monkeypatch.setattr(
        "learnnest.cli.run_pending_downloads", fake_run_pending_downloads
    )

    result = CliRunner().invoke(
        app,
        ["download", "pending", "--output-root", str(tmp_path / "vault")],
    )

    assert result.exit_code == 0, result.output
    assert captured["douyin_cookie"] == SecretStr(secret)
    assert secret not in result.output


def test_download_pending_latest_only_is_an_explicit_scoped_test_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    def fake_run_pending_downloads(root, **kwargs):
        captured["root"] = root
        captured.update(kwargs)
        return DownloadOutcome(None, 0, 0, 0)

    monkeypatch.setattr(
        "learnnest.cli.run_pending_downloads", fake_run_pending_downloads
    )

    result = CliRunner().invoke(
        app,
        [
            "download",
            "pending",
            "--schedule-id",
            "douyin-favorites",
            "--latest-only",
            "--output-root",
            str(tmp_path / "vault"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["schedule_id"] == "douyin-favorites"
    assert captured["selection"] == "latest_observed"
    assert captured["max_items"] == 1


def test_schedule_run_executes_only_the_named_foreground_monitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_schedule_once(root, schedule_id, **kwargs):
        captured["root"] = root
        captured["schedule_id"] = schedule_id
        captured.update(kwargs)
        return ScheduleOutcome(schedule_id, "completed", batch_id="batch-1")

    monkeypatch.setattr("learnnest.cli.run_schedule_once", fake_run_schedule_once)

    result = CliRunner().invoke(
        app,
        [
            "schedule",
            "run",
            "douyin-favorites",
            "--output-root",
            str(tmp_path / "vault"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["schedule_id"] == "douyin-favorites"
    assert "batch-1" in result.output
