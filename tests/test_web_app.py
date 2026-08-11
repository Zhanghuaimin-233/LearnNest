from __future__ import annotations

import time
import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

import learnnest.cli as cli
import learnnest.learning_workspace as learning_workspace
import learnnest.web_app as web_app
from learnnest.execution import RecoveryPlan
from learnnest.automation_store import load_intake
from learnnest.models import StageStatus, TaskRecord
from learnnest.task_store import create_task, write_task_atomic
from learnnest.provider_profiles import connect, set_role_binding


def _task(root: Path, *, error_summary: str | None = None) -> tuple[Path, TaskRecord]:
    task_dir = root / "视频学习素材" / "20260728-lesson"
    task = create_task(
        task_id="20260728-a1b2c3d4",
        source_path="C:/private-media/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="本地课程",
    ).model_copy(
        update={
            "stages": {"source": StageStatus.COMPLETED},
            "artifacts": {"source": ["report.txt"]},
            "error_summary": error_summary,
        }
    )
    task_dir.mkdir(parents=True)
    (task_dir / "report.txt").write_text("safe artifact", encoding="utf-8")
    write_task_atomic(task_dir, task)
    return task_dir, task


def _client(root: Path) -> TestClient:
    return TestClient(web_app.create_web_app(root))


def _wait_for_job(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        payload = response.json()
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("background job did not complete")


def test_web_app_lists_sanitized_tasks_and_serves_declared_artifacts(
    tmp_path: Path,
) -> None:
    _, task = _task(tmp_path, error_summary="failed at C:\\private-media\\lesson.mp4")
    client = _client(tmp_path)

    health = client.get("/healthz")
    listing = client.get("/api/tasks")
    detail = client.get(f"/api/tasks/{task.task_id}")

    assert health.json() == {"status": "ok", "scope": "loopback"}
    assert listing.status_code == 200
    assert listing.json()["tasks"][0]["source"] == "lesson.mp4"
    assert "C:/private-media" not in detail.text
    assert "C:\\private-media" not in detail.text
    artifact = detail.json()["artifacts"][0]
    assert client.get(artifact["href"]).text == "safe artifact"
    assert (
        client.get(f"/api/tasks/{task.task_id}/artifacts/../../task.json").status_code
        == 404
    )


def test_web_app_validates_source_without_returning_local_path(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/jobs/process", json={"source": "C:/private-media/missing.mp4"}
    )

    assert response.status_code == 400
    assert "C:/private-media" not in response.text
    assert "本地视频" in response.json()["detail"]


def test_web_endpoints_reject_non_public_url_before_processing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    called = False

    def fail_process(*_: Any, **__: Any) -> TaskRecord:
        nonlocal called
        called = True
        raise AssertionError("private URL reached the pipeline")

    monkeypatch.setattr(web_app, "process_source", fail_process)
    monkeypatch.setattr(learning_workspace, "process_source", fail_process)
    client = _client(tmp_path)

    legacy = client.post(
        "/api/jobs/process", json={"source": "http://127.0.0.1:8000/private"}
    )
    learning = client.post(
        "/api/learning/items", json={"source": "http://[::1]/private"}
    )

    assert legacy.status_code == 400
    assert learning.status_code == 400
    assert "公开" in legacy.json()["detail"]
    assert "公开" in learning.json()["detail"]
    assert called is False


def test_web_app_strips_credentials_and_query_from_legacy_url_task(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "视频学习素材" / "20260728-url"
    task = create_task(
        task_id="20260728-url12345",
        source_path="https://person:secret@example.com/lesson?token=private",
        source_type="url",
        source_fingerprint="url12345",
        title="公开课程",
    )
    write_task_atomic(task_dir, task)

    detail = _client(tmp_path).get(f"/api/tasks/{task.task_id}")

    assert detail.json()["source"] == "example.com/lesson"
    assert "secret" not in detail.text
    assert "token" not in detail.text


def test_web_app_processes_one_source_in_background_with_evidence_profile(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video")
    _, task = _task(tmp_path)
    observed: dict[str, Any] = {}

    def fake_process_video(
        received_source: Path, output_root: Path, profile: str
    ) -> TaskRecord:
        observed.update(
            source=received_source, output_root=output_root, profile=profile
        )
        return task

    monkeypatch.setattr(web_app, "process_video", fake_process_video)
    client = _client(tmp_path)

    response = client.post("/api/jobs/process", json={"source": str(source)})
    job = _wait_for_job(client, response.json()["job_id"])

    assert response.status_code == 202
    assert job["status"] == "completed"
    assert job["task_id"] == task.task_id
    assert observed == {
        "source": source.resolve(),
        "output_root": tmp_path.resolve(),
        "profile": "evidence",
    }
    intake = load_intake(tmp_path, task.task_id)
    assert intake.source_kind == "local_video"
    assert intake.default_output == "complete_note_with_audio"


def test_web_app_recover_rejects_paid_stage_without_rerunning(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task = _task(tmp_path)
    called = False

    def fake_plan_recovery(_: Path) -> RecoveryPlan:
        return RecoveryPlan(
            task_id=task.task_id,
            from_stage="note",
            reasons=["missing note"],
            requires_paid=True,
        )

    def fail_rerun(*_: Any, **__: Any) -> TaskRecord:
        nonlocal called
        called = True
        return task

    monkeypatch.setattr(web_app, "plan_recovery", fake_plan_recovery)
    monkeypatch.setattr(web_app, "rerun_task", fail_rerun)
    client = _client(tmp_path)

    response = client.post(f"/api/tasks/{task.task_id}/recover")
    job = _wait_for_job(client, response.json()["job_id"])

    assert task_dir.is_dir()
    assert job["status"] == "failed"
    assert job["error"] == "操作无法执行，请检查输入或在 CLI 中查看恢复计划。"
    assert called is False


def test_web_app_exposes_automation_as_read_only_status(tmp_path: Path) -> None:
    client = _client(tmp_path)

    assert client.get("/api/automation/status").json() == {
        "configured": False,
        "enabled": False,
    }


def test_web_app_configures_authorizes_and_disables_automation_without_calls(
    tmp_path: Path,
) -> None:
    connect(tmp_path, name="note", preset="mimo", secret_value="fake-key")
    set_role_binding(tmp_path, role="note_writer", connection_name="note")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="note")
    client = _client(tmp_path)

    configured = client.post(
        "/api/automation/configure",
        json={
            "default_output": "complete_note",
            "auto_organize_new_favorites": False,
            "check_interval_seconds": 300,
            "max_items_per_tick": 1,
        },
    )
    authorized = client.post("/api/automation/authorize", json={"confirm_paid": True})
    disabled = client.post("/api/automation/disable")

    assert configured.status_code == 200
    assert configured.json()["enabled"] is False
    assert configured.json()["default_output"] == "complete_note"
    assert authorized.json()["enabled"] is True
    assert disabled.json()["enabled"] is False
    assert "fake-key" not in configured.text + authorized.text + disabled.text


def test_web_automation_authorization_requires_explicit_paid_confirmation(
    tmp_path: Path,
) -> None:
    connect(tmp_path, name="note", preset="mimo", secret_value="fake-key")
    set_role_binding(tmp_path, role="note_writer", connection_name="note")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="note")
    client = _client(tmp_path)
    client.post(
        "/api/automation/configure",
        json={
            "default_output": "complete_note",
            "auto_organize_new_favorites": False,
            "check_interval_seconds": 300,
            "max_items_per_tick": 1,
        },
    )

    for payload in ({}, {"confirm_paid": False}, {"confirm_paid": True, "extra": 1}):
        assert client.post("/api/automation/authorize", json=payload).status_code == 422
    assert client.get("/api/automation/status").json()["enabled"] is False


def test_web_upload_uses_controlled_storage_and_rejects_unsafe_names(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _, task = _task(tmp_path)
    observed: dict[str, Any] = {}

    def fake_process_video(source: Path, output_root: Path, profile: str) -> TaskRecord:
        observed.update(source=source, output_root=output_root, profile=profile)
        return task

    monkeypatch.setattr(web_app, "process_video", fake_process_video)
    client = _client(tmp_path)

    rejected = client.post(
        "/api/learning/uploads?name=..%2Fprivate.mp4", content=b"video"
    )
    uploaded = client.post("/api/learning/uploads?name=lesson.mp4", content=b"video")
    job = _wait_for_job(client, uploaded.json()["job_id"])

    assert rejected.status_code == 400
    assert "private" not in rejected.text
    assert job["status"] == "completed"
    assert observed["source"].is_relative_to(tmp_path / ".learnnest" / "uploads")
    assert observed["source"].read_bytes() == b"video"
    assert "uploads" not in uploaded.text


def test_web_upload_removes_partial_file_when_stream_fails(tmp_path: Path) -> None:
    async def broken_stream():
        yield b"partial"
        raise OSError("connection interrupted")

    service = web_app.WebService(tmp_path)

    with pytest.raises(ValueError):
        asyncio.run(service.save_uploaded_video("lesson.mp4", broken_stream()))

    assert not list((tmp_path / ".learnnest" / "uploads").glob("*.partial"))


def test_selected_douyin_favorites_use_the_existing_source_job_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from learnnest.douyin_favorites import DouyinFavoritesStore

    _, task = _task(tmp_path)
    store = DouyinFavoritesStore(tmp_path)
    store.directory.mkdir(parents=True)
    store.facts_path.write_text(
        '{"synced_at":"now","items":[{"aweme_id":"123","title":"收藏","url":"https://www.douyin.com/video/123","synced_at":"now","thumbnail_path":null}]}',
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_process_source(source: Any, root: Path, profile: str) -> TaskRecord:
        calls.append(source.input)
        assert root == tmp_path.resolve()
        assert profile == "evidence"
        return task

    monkeypatch.setattr(web_app, "process_source", fake_process_source)
    client = _client(tmp_path)

    response = client.post("/api/douyin/favorites/select", json={"aweme_ids": ["123"]})
    job = _wait_for_job(client, response.json()["jobs"][0]["job_id"])

    assert response.status_code == 202
    assert job["status"] == "completed"
    assert calls == ["https://www.douyin.com/video/123"]


def test_web_server_is_fixed_to_loopback(tmp_path: Path, monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_run(application: Any, **kwargs: Any) -> None:
        captured["application"] = application
        captured.update(kwargs)

    monkeypatch.setattr(web_app.uvicorn, "run", fake_run)

    web_app.serve_web_app(tmp_path, port=8123)

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8123
    assert captured["application"].title == "语栖学习收件箱"


def test_web_cli_forwards_only_root_and_port(tmp_path: Path, monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_serve(root: Path, *, port: int) -> None:
        captured.update(root=root, port=port)

    monkeypatch.setattr(cli, "serve_web_app", fake_serve)

    result = CliRunner().invoke(
        cli.app, ["web", "serve", "--output-root", str(tmp_path), "--port", "8123"]
    )

    assert result.exit_code == 0, result.output
    assert captured == {"root": tmp_path.resolve(), "port": 8123}


def _published_learning_task(
    root: Path,
    *,
    name: str,
    markdown: str = "# 一篇笔记",
) -> tuple[Path, TaskRecord]:
    task_dir = root / "视频学习素材" / name
    task = create_task(
        task_id=f"20260728-{name[-8:]}",
        source_path="C:/private-media/lesson.mp4",
        source_fingerprint=name,
        title="本地课程",
        profile="note",
    ).model_copy(
        update={
            "stages": {"publish": StageStatus.COMPLETED},
            "artifacts": {"publish": ["note.md"]},
        }
    )
    task_dir.mkdir(parents=True)
    (task_dir / "note.md").write_text(
        f"<!-- learnnest-task-id: {task.task_id} -->\n\n{markdown}",
        encoding="utf-8",
    )
    write_task_atomic(task_dir, task)
    return task_dir, task


def test_learning_api_adds_note_and_opens_safe_rendered_markdown(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video")
    task_dir, task = _published_learning_task(
        tmp_path,
        name="learning-note",
        markdown="# 一篇笔记\n\n![封面](cover.png)\n\n<script>bad()</script>",
    )
    (task_dir / "cover.png").write_bytes(b"png")
    observed: dict[str, Any] = {}

    def fake_process(video: Path, root: Path, profile: str) -> TaskRecord:
        observed.update(video=video, root=root, profile=profile)
        return task

    monkeypatch.setattr(learning_workspace, "process_video", fake_process)
    client = _client(tmp_path)

    page = client.get("/")
    response = client.post("/api/learning/items", json={"source": str(source)})
    snapshot = client.get("/api/learning/snapshot")
    note = client.get(f"/api/learning/items/{task.task_id}/note")

    assert response.status_code == 201
    assert observed["profile"] == "note"
    assert snapshot.json()["library"][0]["state"] == "ready"
    assert "<h1>一篇笔记</h1>" in note.text
    assert "<script>bad()</script>" not in note.text
    assert f"/api/learning/items/{task.task_id}/images/cover.png" in note.text
    assert (
        client.get(f"/api/learning/items/{task.task_id}/images/cover.png").content
        == b"png"
    )
    for forbidden in (
        "evidence",
        "sha",
        "stage",
        "plan",
        "recover",
        "route",
        "task id",
    ):
        assert forbidden not in page.text.lower()


def test_douyin_webui_keeps_login_and_favorite_vocabulary_human_facing(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    page = client.get("/").text
    script = client.get("/static/workspace.js").text

    for visible_text in (
        "连接抖音",
        "默认视频收藏",
        "手动同步",
        "打开抖音验证窗口",
        "Windows 当前用户加密",
        "手机号验证码",
        "抖音 App 扫码确认",
        "两步完成后才会连接",
        "已扫码",
        "需验证",
        "需重连",
    ):
        assert visible_text in page or visible_text in script
    for forbidden in (
        "cookie",
        "token",
        "passport",
        "evidence",
        "sha",
        "stage",
        "plan",
        "recover",
        "route",
    ):
        assert forbidden not in page.lower()
    assert "error.status === 401" in script
    assert "登录已失效，请重新连接抖音。" in script
    assert "请在原抖音官方窗口继续完成短信、扫码或页面要求的额外验证。" in script
    assert 'api("/api/douyin/login/browser"' in script
    assert 'api("/api/douyin/login/current"' in script
    assert 'type="tel"' not in page


def test_webui_preserves_favorite_selection_and_gives_automation_toggle_its_own_row(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    page = client.get("/").text
    script = client.get("/static/workspace.js").text
    stylesheet = client.get("/static/workspace.css").text

    assert "let selectedFavoriteIdsState = new Set();" in script
    assert "selectedFavoriteIdsState.has(item.aweme_id)" in script
    assert "selectedFavoriteIdsState.clear();" in script
    assert 'class="checkbox-label automation-favorite-option"' in page
    assert ".automation-favorite-option {" in stylesheet
    assert "grid-column: 1 / -1" in stylesheet


def test_learning_snapshot_observes_external_atomic_task_update(tmp_path: Path) -> None:
    task_dir, task = _task(tmp_path)
    client = _client(tmp_path)

    first = client.get("/api/learning/snapshot").json()
    (task_dir / "note.md").write_text(
        f"<!-- learnnest-task-id: {task.task_id} -->\n\n# 已完成",
        encoding="utf-8",
    )
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "stages": {"publish": StageStatus.COMPLETED},
                "artifacts": {"publish": ["note.md"]},
            }
        ),
    )
    started = time.monotonic()
    changed = client.get(f"/api/learning/snapshot?revision={first['revision']}").json()

    assert time.monotonic() - started < 3
    assert changed["unchanged"] is False
    assert changed["revision"] != first["revision"]
    assert changed["library"][0]["item_ref"] == task.task_id
    assert (
        client.get(f"/api/learning/snapshot?revision={changed['revision']}").json()[
            "unchanged"
        ]
        is True
    )


def test_learning_note_rejects_escaped_or_unreferenced_images(tmp_path: Path) -> None:
    _, escaped = _published_learning_task(
        tmp_path, name="escaped-image", markdown="![x](../../outside.png)"
    )
    task_dir, safe = _published_learning_task(
        tmp_path, name="safe-image", markdown="# 没有图片"
    )
    (task_dir / "unreferenced.png").write_bytes(b"not used")
    client = _client(tmp_path)

    assert client.get(f"/api/learning/items/{escaped.task_id}/note").status_code == 409
    assert (
        client.get(
            f"/api/learning/items/{safe.task_id}/images/unreferenced.png"
        ).status_code
        == 404
    )


def test_learning_api_declines_paid_continue_without_rerun(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _, task = _task(tmp_path)
    called = False

    monkeypatch.setattr(
        learning_workspace,
        "plan_recovery",
        lambda _: RecoveryPlan(
            task_id=task.task_id,
            from_stage="note",
            reasons=["missing note"],
            requires_paid=True,
        ),
    )

    def fail_rerun(*_: Any, **__: Any) -> TaskRecord:
        nonlocal called
        called = True
        return task

    monkeypatch.setattr(learning_workspace, "rerun_task", fail_rerun)

    response = _client(tmp_path).post(f"/api/learning/items/{task.task_id}/continue")

    assert response.status_code == 200
    assert response.json()["outcome"] == "needs_setup"
    assert called is False
