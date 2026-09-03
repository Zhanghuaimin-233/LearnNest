from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

import learnnest.cli as cli
import learnnest.learning_workspace as learning_workspace
import learnnest.web_app as web_app
from learnnest.local_models import LocalModelCancelled, LocalModelService
from learnnest.execution import RecoveryPlan
from learnnest.launcher import load_launcher_config
from learnnest.automation_models import (
    AutomationAttempt,
    AutomationIntake,
    AutomationTaskState,
)
from learnnest.automation_coordinator import AutomationCoordinator, _has_paid_task_trace
from learnnest.automation_runner import (
    AutomationProviders,
    AutomationRunResult,
    run_automation_tasks,
)
from learnnest.automation_store import (
    create_intake,
    load_intake,
    load_status,
    load_task_state,
    save_intake,
    save_task_state,
)
from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.pipeline import PipelineError
from learnnest.task_store import create_task, load_task, write_task_atomic
from learnnest.task_control import load_task_control
from learnnest.provider_profiles import (
    connect,
    freeze_role_bindings,
    load_settings,
    set_role_binding,
    settings_sha256,
    update_limits,
)


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


def _wait_for_model(client: TestClient, package_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get("/api/local-models")
        model = next(
            item
            for item in response.json()["models"]
            if item["package_id"] == package_id
        )
        if model["state"] in {"ready", "failed", "cancelled"}:
            return model
        time.sleep(0.01)
    raise AssertionError("model job did not complete")


def test_local_model_api_lists_and_installs_without_exposing_paths(
    tmp_path: Path,
) -> None:
    def installer(spec, staging, progress, cancel) -> None:  # type: ignore[no-untyped-def]
        component = staging / "model"
        component.mkdir(parents=True)
        for name in (
            "config.json",
            "model.bin",
            "preprocessor_config.json",
            "tokenizer.json",
            "vocabulary.json",
        ):
            (component / name).write_bytes(name.encode())

    models = LocalModelService(
        tmp_path / "device-models", installer=installer, discover_external=False
    )
    client = TestClient(web_app.create_web_app(tmp_path, local_models=models))

    listed = client.get("/api/local-models")
    accepted = client.post("/api/local-models/faster-whisper-large-v3/download")
    ready = _wait_for_model(client, "faster-whisper-large-v3")

    assert listed.status_code == 200
    assert listed.json()["model_home"] == str(models.root)
    assert accepted.status_code == 202
    assert ready["state"] == "ready"
    assert ready["asset_id"] == "faster-whisper-large-v3"
    assert ready["managed"] is True
    assert str(tmp_path) not in json.dumps(ready)


def test_local_model_api_rejects_unknown_package(tmp_path: Path) -> None:
    models = LocalModelService(tmp_path / "device-models", discover_external=False)
    client = TestClient(web_app.create_web_app(tmp_path, local_models=models))

    response = client.post("/api/local-models/not-a-package/download")

    assert response.status_code == 404


def test_local_model_api_rejects_duplicate_download_and_accepts_cancel(
    tmp_path: Path,
) -> None:
    started = threading.Event()

    def installer(spec, staging, progress, cancel) -> None:  # type: ignore[no-untyped-def]
        started.set()
        assert cancel.wait(timeout=2)
        raise LocalModelCancelled

    models = LocalModelService(
        tmp_path / "device-models", installer=installer, discover_external=False
    )
    client = TestClient(web_app.create_web_app(tmp_path, local_models=models))

    first = client.post("/api/local-models/faster-whisper-large-v3/download")
    assert started.wait(timeout=1)
    duplicate = client.post("/api/local-models/faster-whisper-large-v3/download")
    cancelled = client.post("/api/local-models/faster-whisper-large-v3/cancel")

    assert first.status_code == 202
    assert duplicate.status_code == 409
    assert cancelled.status_code == 202


class _RetryableWriter:
    name = "xiaomi-mimo"
    model = "mimo-v2.5"
    endpoint_identity = "https://api.xiaomimimo.com/v1"

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.writer_calls = 0
        self.reviewer_calls = 0

    def write_markdown(self, _dossier_json: str) -> str:
        self.writer_calls += 1
        if self.writer_calls <= self.failures:
            raise RuntimeError("HTTP 503 temporary writer failure")
        return "# Retry\n\n正文。"

    def review_markdown(self, _dossier_json: str, _candidate_markdown: str) -> str:
        self.reviewer_calls += 1
        return "# Retry\n\n复核正文。"


class _UnusedAutomationProvider:
    name = "unused"
    model = "unused"
    voice = "unused"


def _retry_providers(provider: _RetryableWriter) -> AutomationProviders:
    return AutomationProviders(
        writer=provider,
        reviewer=provider,
        podcast=_UnusedAutomationProvider(),
        tts=_UnusedAutomationProvider(),
    )


def _prepare_due_automation_retry(
    root: Path, *, default_output: str = "complete_note"
) -> tuple[
    Path,
    TaskRecord,
    AutomationIntake,
    AutomationTaskState,
    _RetryableWriter,
]:
    task_dir, task = _task(root)
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "stages": {"content_pack": StageStatus.COMPLETED},
                "artifacts": {"content_pack": ["content_pack.json"]},
            }
        ),
    )
    (task_dir / "content_pack.json").write_text(
        ContentPack(
            task_id=task.task_id,
            source_fingerprint=task.source_fingerprint,
            evidence=[
                Evidence(
                    id="tr_0001",
                    kind="transcript",
                    start_ms=0,
                    end_ms=1_000,
                    text="retry test",
                    artifact_path="content_pack.json",
                )
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )
    connect(
        root, name="note", preset="mimo", secret_value="fake-key", model="mimo-v2.5"
    )
    set_role_binding(root, role="note_writer", connection_name="note")
    set_role_binding(root, role="note_reviewer", connection_name="note")
    if default_output == "complete_note_with_audio":
        connect(
            root,
            name="podcast",
            preset="deepseek",
            secret_value="fake-key",
            model="deepseek-v4-pro",
        )
        set_role_binding(root, role="podcast", connection_name="podcast")
        connect(root, name="voice", preset="windows-tts")
        set_role_binding(root, role="tts", connection_name="voice")
    service = web_app.WebService(root)
    service.configure_automation(
        web_app.AutomationConfigureRequest(
            default_output=default_output,
            check_interval_minutes=1,
            max_items_per_tick=1,
        )
    )
    service.authorize_automation(web_app.AutomationAuthorizeRequest(confirm_paid=True))
    bindings = freeze_role_bindings(root)
    write_task_atomic(
        task_dir,
        load_task(task_dir).model_copy(
            update={
                "provider_bindings": bindings,
                "provider_settings_sha256": settings_sha256(load_settings(root)),
            }
        ),
    )
    status = load_status(root)
    assert status is not None
    intake = AutomationIntake(
        task_id=task.task_id,
        source_kind="local_video",
        default_output=default_output,  # type: ignore[arg-type]
        created_at=datetime.now(UTC),
        status="pending",
    )
    create_intake(root, intake)
    provider = _RetryableWriter()
    failed_at = datetime.now(UTC) - timedelta(minutes=2)
    coordinator = AutomationCoordinator(
        root,
        clock=lambda: failed_at,
        run_tasks=lambda output_root, task_ids, *, default_outputs, now: (
            run_automation_tasks(
                output_root,
                task_ids,
                _retry_providers(provider),
                default_outputs=default_outputs,
                now=now,
            )
        ),
    )
    result = coordinator.tick_once()
    assert result is not None
    assert result.failed_task_ids == (task.task_id,)
    state = load_task_state(root, task.task_id, status.policy_sha256)
    assert state is not None
    intake = load_intake(root, task.task_id)
    assert state.status == "pending"
    assert state.blocked_reason == "retry_wait"
    assert intake.status == "needs_attention"
    return task_dir, load_task(task_dir), intake, state, provider


def _retry_fact_bytes(root: Path, task_id: str, policy_sha: str) -> tuple[bytes, bytes]:
    return (
        (
            root / ".learnnest" / "automation" / "intake" / f"{task_id}.json"
        ).read_bytes(),
        (
            root
            / ".learnnest"
            / "automation"
            / "tasks"
            / task_id
            / f"{policy_sha}.json"
        ).read_bytes(),
    )


def _assert_retry_rejected_without_wake_or_factory(
    root: Path,
    task: TaskRecord,
    state: AutomationTaskState,
    *,
    monkeypatch: Any,
) -> None:
    before = _retry_fact_bytes(root, task.task_id, state.policy_sha256)
    wake_calls: list[str] = []
    monkeypatch.setattr(
        web_app.AutomationCoordinator,
        "wake",
        lambda _self: wake_calls.append(task.task_id) or True,
    )
    response = _client(root).post(
        f"/api/learning/items/{task.task_id}/retry-automation"
    )

    assert response.status_code == 409
    assert _retry_fact_bytes(root, task.task_id, state.policy_sha256) == before
    assert wake_calls == []
    factory_calls: list[str] = []
    AutomationCoordinator(
        root,
        provider_factory=lambda _root, task_id: (
            factory_calls.append(task_id),
            AssertionError("Provider factory must not run"),
        )[1],
    ).tick_once()
    assert factory_calls == []


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


def test_learning_api_pauses_and_resumes_without_advancing_task_facts(
    tmp_path: Path,
) -> None:
    task_dir, task = _task(tmp_path)
    client = _client(tmp_path)

    paused = client.post(f"/api/learning/items/{task.task_id}/pause")

    assert paused.status_code == 200
    paused_item = paused.json()["item"]
    assert {
        key: paused_item[key]
        for key in (
            "state",
            "action",
            "action_kind",
            "manually_paused",
            "can_pause",
        )
    } == {
        "state": "queued",
        "action": "继续任务",
        "action_kind": "resume_task",
        "manually_paused": True,
        "can_pause": False,
    }
    assert load_task(task_dir) == task
    assert load_task_control(task_dir, task.task_id).manually_paused is True

    resumed = client.post(f"/api/learning/items/{task.task_id}/resume")

    assert resumed.status_code == 200
    assert resumed.json()["item"]["manually_paused"] is False
    assert resumed.json()["item"]["can_pause"] is True
    assert load_task(task_dir) == task
    assert load_task_control(task_dir, task.task_id).manually_paused is False


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


def test_web_app_retries_a_durable_source_job_with_the_same_identity(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video")
    _, task = _task(tmp_path)
    calls = 0

    def process_once(*_args: Any, **_kwargs: Any) -> TaskRecord:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PipelineError("transient source failure")
        return task

    monkeypatch.setattr(web_app, "process_video", process_once)
    client = _client(tmp_path)
    first = client.post("/api/learning/submit", json={"source": str(source)})
    failed = _wait_for_job(client, first.json()["job_id"])
    retried = client.post(f"/api/learning/jobs/{failed['job_id']}/retry")
    completed = _wait_for_job(client, failed["job_id"])

    assert failed["status"] == "failed"
    assert retried.status_code == 202
    assert retried.json()["job_id"] == failed["job_id"]
    assert completed["status"] == "completed"
    assert completed["attempts"] == 2
    assert calls == 2


def test_source_job_reuses_an_existing_tasks_frozen_intake(
    tmp_path: Path, monkeypatch: Any
) -> None:
    source = tmp_path / "same-video.mp4"
    source.write_bytes(b"video")
    _, task = _task(tmp_path)
    frozen = AutomationIntake(
        task_id=task.task_id,
        source_kind="local_video",
        default_output="complete_note_with_audio",
        created_at=datetime.now(UTC),
        status="needs_attention",
    )
    create_intake(tmp_path, frozen)
    monkeypatch.setattr(web_app, "process_video", lambda *_args, **_kwargs: task)
    monkeypatch.setattr(
        web_app.WebService,
        "default_automation_output",
        lambda _self: "complete_note",
    )
    client = _client(tmp_path)

    submitted = client.post("/api/learning/submit", json={"source": str(source)})
    completed = _wait_for_job(client, submitted.json()["job_id"])

    assert completed["status"] == "completed"
    assert completed["task_id"] == task.task_id
    assert load_intake(tmp_path, task.task_id) == frozen


def test_web_app_start_automation_persists_the_intake_without_a_provider_call(
    tmp_path: Path,
) -> None:
    task_dir, task = _task(tmp_path)
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "stages": {"content_pack": StageStatus.COMPLETED},
                "artifacts": {"content_pack": ["content_pack.json"]},
            }
        ),
    )
    client = _client(tmp_path)

    response = client.post(f"/api/learning/items/{task.task_id}/start-automation")

    assert response.status_code == 202
    intake = load_intake(tmp_path, task.task_id)
    assert intake.status == "pending"
    assert intake.default_output == "complete_note_with_audio"


def test_web_app_restarts_attention_that_never_reached_a_provider(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task = _task(tmp_path)
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "stages": {
                    "content_pack": StageStatus.COMPLETED,
                    "note": StageStatus.SKIPPED,
                    "publish": StageStatus.SKIPPED,
                    "podcast_script": StageStatus.SKIPPED,
                    "tts": StageStatus.SKIPPED,
                },
                "artifacts": {"content_pack": ["content_pack.json"]},
            }
        ),
    )
    connect(
        tmp_path, name="note", preset="mimo", secret_value="fake-key", model="mimo-v2.5"
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="note")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="note")
    service = web_app.WebService(tmp_path)
    service.configure_automation(
        web_app.AutomationConfigureRequest(
            default_output="complete_note",
            check_interval_minutes=1,
            max_items_per_tick=1,
        )
    )
    service.authorize_automation(web_app.AutomationAuthorizeRequest(confirm_paid=True))
    create_intake(
        tmp_path,
        AutomationIntake(
            task_id=task.task_id,
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime.now(UTC),
            status="needs_attention",
        ),
    )
    wake_calls: list[str] = []
    monkeypatch.setattr(
        web_app.AutomationCoordinator,
        "wake",
        lambda _self: wake_calls.append(task.task_id) or True,
    )

    response = _client(tmp_path).post(
        f"/api/learning/items/{task.task_id}/start-automation"
    )

    assert response.status_code == 202
    assert load_intake(tmp_path, task.task_id).status == "pending"
    assert wake_calls == [task.task_id]


def test_web_app_restarts_with_the_intake_frozen_output_not_the_new_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task = _task(tmp_path)
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "stages": {
                    "content_pack": StageStatus.COMPLETED,
                    "note": StageStatus.SKIPPED,
                    "publish": StageStatus.SKIPPED,
                    "podcast_script": StageStatus.SKIPPED,
                    "tts": StageStatus.SKIPPED,
                },
                "artifacts": {"content_pack": ["content_pack.json"]},
            }
        ),
    )
    connect(
        tmp_path, name="note", preset="mimo", secret_value="fake-key", model="mimo-v2.5"
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="note")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="note")
    connect(
        tmp_path,
        name="podcast",
        preset="deepseek",
        secret_value="fake-key",
        model="deepseek-v4-pro",
    )
    set_role_binding(tmp_path, role="podcast", connection_name="podcast")
    connect(tmp_path, name="voice", preset="windows-tts")
    set_role_binding(tmp_path, role="tts", connection_name="voice")
    service = web_app.WebService(tmp_path)
    service.configure_automation(
        web_app.AutomationConfigureRequest(
            default_output="complete_note",
            check_interval_minutes=1,
            max_items_per_tick=1,
        )
    )
    service.authorize_automation(web_app.AutomationAuthorizeRequest(confirm_paid=True))
    create_intake(
        tmp_path,
        AutomationIntake(
            task_id=task.task_id,
            source_kind="local_video",
            default_output="complete_note_with_audio",
            created_at=datetime.now(UTC),
            status="needs_attention",
        ),
    )
    monkeypatch.setattr(web_app.AutomationCoordinator, "wake", lambda _self: True)

    response = _client(tmp_path).post(
        f"/api/learning/items/{task.task_id}/start-automation"
    )

    assert response.status_code == 202
    restarted = load_intake(tmp_path, task.task_id)
    assert restarted.status == "pending"
    assert restarted.default_output == "complete_note_with_audio"


def test_learning_api_moves_one_task_to_the_internal_trash(tmp_path: Path) -> None:
    task_dir, task = _task(tmp_path)
    other_dir = tmp_path / "视频学习素材" / "other"
    other = create_task(
        task_id="20260814-other",
        source_path="C:/private-media/other.mp4",
        source_fingerprint="other",
        title="其他任务",
    )
    write_task_atomic(other_dir, other)

    response = _client(tmp_path).delete(f"/api/learning/items/{task.task_id}")

    assert response.status_code == 200
    assert response.json() == {"status": "trashed", "task_id": task.task_id}
    assert not task_dir.exists()
    assert other_dir.is_dir()
    assert list((tmp_path / ".learnnest" / "trash" / "tasks").glob("*/task/task.json"))

    listing = _client(tmp_path).get("/api/learning/trash")
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 1
    trashed = listing.json()["items"][0]
    assert set(trashed) == {"bundle_id", "task_id", "title", "trashed_at"}
    assert trashed["task_id"] == task.task_id
    assert "private-media" not in listing.text

    restored = _client(tmp_path).post(
        f"/api/learning/trash/{trashed['bundle_id']}/restore"
    )
    assert restored.status_code == 200
    assert restored.json() == {"status": "restored", "task_id": task.task_id}
    assert task_dir.is_dir()
    assert _client(tmp_path).get("/api/learning/trash").json() == {"items": []}
    assert (
        _client(tmp_path)
        .post(f"/api/learning/trash/{trashed['bundle_id']}/restore")
        .status_code
        == 404
    )


def test_skipped_provider_stages_are_not_a_paid_execution_trace(tmp_path: Path) -> None:
    _task_dir, task = _task(tmp_path)
    task = task.model_copy(
        update={
            "stages": {
                "content_pack": StageStatus.COMPLETED,
                "note": StageStatus.SKIPPED,
                "publish": StageStatus.SKIPPED,
                "podcast_script": StageStatus.SKIPPED,
                "tts": StageStatus.SKIPPED,
            }
        }
    )

    assert _has_paid_task_trace(task) is False


def test_web_app_explains_and_resumes_a_zero_call_budget_block(
    tmp_path: Path, monkeypatch: Any
) -> None:
    connect(
        tmp_path, name="note", preset="mimo", secret_value="fake-key", model="mimo-v2.5"
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="note")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="note")
    update_limits(
        tmp_path,
        retries_per_role=0,
        global_calls_per_day=0,
        budget_group_calls_per_day={
            "note": 0,
            "podcast": 0,
            "tts": 0,
            "asr": 1,
            "ocr": 1,
        },
    )
    service = web_app.WebService(tmp_path)
    service.configure_automation(
        web_app.AutomationConfigureRequest(
            default_output="complete_note",
            check_interval_minutes=1,
            max_items_per_tick=1,
        )
    )
    service.authorize_automation(web_app.AutomationAuthorizeRequest(confirm_paid=True))
    old_status = load_status(tmp_path)
    assert old_status is not None
    task_dir, task = _task(tmp_path)
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "stages": {
                    "content_pack": StageStatus.COMPLETED,
                    "note": StageStatus.SKIPPED,
                    "publish": StageStatus.SKIPPED,
                    "podcast_script": StageStatus.SKIPPED,
                    "tts": StageStatus.SKIPPED,
                },
                "artifacts": {"content_pack": ["content_pack.json"]},
                "provider_bindings": freeze_role_bindings(tmp_path),
                "provider_settings_sha256": settings_sha256(load_settings(tmp_path)),
            }
        ),
    )
    create_intake(
        tmp_path,
        AutomationIntake(
            task_id=task.task_id,
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime.now(UTC),
            status="needs_attention",
        ),
    )
    save_task_state(
        tmp_path,
        AutomationTaskState(
            task_id=task.task_id,
            policy_sha256=old_status.policy_sha256,
            status="pending",
            blocked_reason="provider_budget_exhausted",
        ),
    )

    blocked = _client(tmp_path).get("/api/learning/snapshot").json()["processing"][0]

    assert blocked["message"] == "今日模型调用额度已用完；材料已经保存。"
    assert blocked["action"] == "调整调用额度"
    assert blocked["action_kind"] == "open_settings"

    update_limits(
        tmp_path,
        retries_per_role=0,
        global_calls_per_day=2,
        budget_group_calls_per_day={
            "note": 2,
            "podcast": 0,
            "tts": 0,
            "asr": 1,
            "ocr": 1,
        },
    )
    service.authorize_automation(web_app.AutomationAuthorizeRequest(confirm_paid=True))
    ready = _client(tmp_path).get("/api/learning/snapshot").json()["processing"][0]

    assert ready["message"] == "新的调用额度已经可用，可以继续生成笔记。"
    assert ready["action"] == "继续生成笔记"
    assert ready["action_kind"] == "start_automation"

    wake_calls: list[str] = []
    monkeypatch.setattr(
        web_app.AutomationCoordinator,
        "wake",
        lambda _self: wake_calls.append(task.task_id) or True,
    )
    response = _client(tmp_path).post(
        f"/api/learning/items/{task.task_id}/start-automation"
    )

    assert response.status_code == 202
    assert load_intake(tmp_path, task.task_id).status == "pending"
    assert wake_calls == [task.task_id]

    runs: list[tuple[str, ...]] = []
    coordinator = AutomationCoordinator(
        tmp_path,
        run_tasks=lambda _root, task_ids, **_kwargs: (
            runs.append(task_ids),
            AutomationRunResult(task_ids, task_ids, ()),
        )[1],
    )
    coordinator.tick_once()

    assert runs == [(task.task_id,)]
    rebound = load_task(task_dir)
    assert {
        role: binding.model_dump(mode="json")
        for role, binding in rebound.provider_bindings.items()
    } == {
        role: binding.model_dump(mode="json")
        for role, binding in freeze_role_bindings(tmp_path).items()
    }
    assert rebound.provider_settings_sha256 == settings_sha256(load_settings(tmp_path))


def test_web_app_retries_only_a_due_retryable_automation_fact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _task_dir, task, intake, state, provider = _prepare_due_automation_retry(tmp_path)
    before_attempts = state.attempts
    wake_calls: list[str] = []
    monkeypatch.setattr(
        web_app.AutomationCoordinator,
        "wake",
        lambda _self: wake_calls.append(task.task_id) or True,
    )
    client = _client(tmp_path)

    response = client.post(f"/api/learning/items/{task.task_id}/retry-automation")

    assert response.status_code == 202
    assert wake_calls == [task.task_id]
    assert load_intake(tmp_path, task.task_id) == intake.model_copy(
        update={"status": "pending"}
    )
    requeued = load_task_state(tmp_path, task.task_id, state.policy_sha256)
    assert requeued is not None
    assert requeued.status == "pending"
    assert requeued.blocked_reason is None
    assert requeued.attempts == before_attempts

    second_coordinator = AutomationCoordinator(
        tmp_path,
        clock=lambda: datetime.now(UTC),
        run_tasks=lambda output_root, task_ids, *, default_outputs, now: (
            run_automation_tasks(
                output_root,
                task_ids,
                _retry_providers(provider),
                default_outputs=default_outputs,
                now=now,
            )
        ),
    )
    second = second_coordinator.tick_once()
    assert second is not None
    assert second.task_ids == (task.task_id,)
    assert second.completed_task_ids == (task.task_id,)
    assert provider.writer_calls == 2


def test_web_app_rejects_handwritten_needs_attention_state(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task, _intake, state, _provider = _prepare_due_automation_retry(tmp_path)
    handwritten = state.model_copy(update={"status": "needs_attention"})
    save_task_state(tmp_path, handwritten)

    _assert_retry_rejected_without_wake_or_factory(
        tmp_path, load_task(task_dir), handwritten, monkeypatch=monkeypatch
    )


@pytest.mark.parametrize(
    "blocked_reason",
    ["unknown_result", "non_retryable_failure", "provider_budget_exhausted"],
)
def test_web_app_never_retries_an_unsafe_automation_fact(
    tmp_path: Path, blocked_reason: str
) -> None:
    task_dir, task = _task(tmp_path)
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "stages": {"content_pack": StageStatus.COMPLETED},
                "artifacts": {"content_pack": ["content_pack.json"]},
            }
        ),
    )
    connect(
        tmp_path, name="note", preset="mimo", secret_value="fake-key", model="mimo-v2.5"
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="note")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="note")
    service = web_app.WebService(tmp_path)
    service.configure_automation(
        web_app.AutomationConfigureRequest(
            default_output="complete_note",
            check_interval_minutes=1,
            max_items_per_tick=1,
        )
    )
    service.authorize_automation(web_app.AutomationAuthorizeRequest(confirm_paid=True))
    status = load_status(tmp_path)
    assert status is not None
    create_intake(
        tmp_path,
        AutomationIntake(
            task_id=task.task_id,
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime.now(UTC),
            status="needs_attention",
        ),
    )
    save_task_state(
        tmp_path,
        AutomationTaskState(
            task_id=task.task_id,
            policy_sha256=status.policy_sha256,
            status="needs_attention",
            blocked_reason=blocked_reason,  # type: ignore[arg-type]
            attempts=[
                AutomationAttempt(
                    stage="writer",
                    attempt=1,
                    status="unknown"
                    if blocked_reason == "unknown_result"
                    else "failed",
                    started_at=datetime.now(UTC),
                    disposition="retryable",
                    next_retry_at=datetime.now(UTC),
                )
            ],
        ),
    )
    client = _client(tmp_path)

    response = client.post(f"/api/learning/items/{task.task_id}/retry-automation")

    assert response.status_code == 409
    assert load_intake(tmp_path, task.task_id).status == "needs_attention"


@pytest.mark.parametrize(
    ("default_output", "role"),
    [
        ("complete_note", "note_writer"),
        ("complete_note", "note_reviewer"),
        ("complete_note_with_audio", "note_writer"),
        ("complete_note_with_audio", "note_reviewer"),
        ("complete_note_with_audio", "podcast"),
        ("complete_note_with_audio", "tts"),
    ],
)
def test_web_app_rejects_retry_when_any_frozen_role_is_inconsistent(
    tmp_path: Path, default_output: str, role: str, monkeypatch: Any
) -> None:
    task_dir, task, _intake, state, _provider = _prepare_due_automation_retry(
        tmp_path, default_output=default_output
    )
    binding = task.provider_bindings[role]
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "provider_bindings": {
                    **task.provider_bindings,
                    role: binding.model_copy(update={"connection_id": f"stale-{role}"}),
                }
            }
        ),
    )

    _assert_retry_rejected_without_wake_or_factory(
        tmp_path, load_task(task_dir), state, monkeypatch=monkeypatch
    )


def test_web_app_rejects_retry_when_intake_and_state_outputs_differ(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task, _intake, state, _provider = _prepare_due_automation_retry(
        tmp_path, default_output="complete_note_with_audio"
    )
    mismatched = state.model_copy(update={"default_output": "complete_note"})
    save_task_state(tmp_path, mismatched)

    _assert_retry_rejected_without_wake_or_factory(
        tmp_path, load_task(task_dir), mismatched, monkeypatch=monkeypatch
    )


@pytest.mark.parametrize("intake_status", ["pending", "claimed", "completed"])
def test_web_app_rejects_retry_when_intake_is_not_needs_attention(
    tmp_path: Path, intake_status: str, monkeypatch: Any
) -> None:
    task_dir, task, intake, state, _provider = _prepare_due_automation_retry(tmp_path)
    save_intake(tmp_path, intake.model_copy(update={"status": intake_status}))

    _assert_retry_rejected_without_wake_or_factory(
        tmp_path, load_task(task_dir), state, monkeypatch=monkeypatch
    )


def test_web_app_rejects_retry_when_only_an_old_policy_state_exists(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task, _intake, state, _provider = _prepare_due_automation_retry(tmp_path)
    status = load_status(tmp_path)
    assert status is not None
    from learnnest.automation_store import save_policy

    save_policy(
        tmp_path,
        status.policy.model_copy(update={"max_items_per_tick": 2}),
    )

    _assert_retry_rejected_without_wake_or_factory(
        tmp_path, load_task(task_dir), state, monkeypatch=monkeypatch
    )


def test_web_app_rejects_retry_before_the_persisted_due_time(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task, _intake, state, _provider = _prepare_due_automation_retry(tmp_path)
    future = state.model_copy(
        update={
            "attempts": [
                state.attempts[0].model_copy(
                    update={"next_retry_at": datetime.now(UTC) + timedelta(minutes=1)}
                )
            ]
        }
    )
    save_task_state(tmp_path, future)

    _assert_retry_rejected_without_wake_or_factory(
        tmp_path, load_task(task_dir), future, monkeypatch=monkeypatch
    )


@pytest.mark.parametrize(
    "blocked_reason",
    [
        "unknown_result",
        "non_retryable_failure",
        "provider_budget_exhausted",
        "stage_attempt_limit",
    ],
)
def test_web_app_rejects_unsafe_reason_only_after_identity_is_current(
    tmp_path: Path, blocked_reason: str, monkeypatch: Any
) -> None:
    task_dir, task, _intake, state, _provider = _prepare_due_automation_retry(tmp_path)
    blocked = state.model_copy(update={"blocked_reason": blocked_reason})
    save_task_state(tmp_path, blocked)

    _assert_retry_rejected_without_wake_or_factory(
        tmp_path, load_task(task_dir), blocked, monkeypatch=monkeypatch
    )

    wake_calls: list[str] = []
    monkeypatch.setattr(
        web_app.AutomationCoordinator,
        "wake",
        lambda _self: wake_calls.append(task.task_id) or True,
    )
    save_task_state(tmp_path, state)
    response = _client(tmp_path).post(
        f"/api/learning/items/{task.task_id}/retry-automation"
    )

    assert response.status_code == 202
    assert wake_calls == [task.task_id]


def test_web_app_requeues_only_the_legal_due_retry_without_mutating_history(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task, intake, state, _provider = _prepare_due_automation_retry(tmp_path)
    wake_calls: list[str] = []
    monkeypatch.setattr(
        web_app.AutomationCoordinator,
        "wake",
        lambda _self: wake_calls.append(task.task_id) or True,
    )

    response = _client(tmp_path).post(
        f"/api/learning/items/{task.task_id}/retry-automation"
    )

    assert response.status_code == 202
    assert wake_calls == [task.task_id]
    assert load_intake(tmp_path, task.task_id) == intake.model_copy(
        update={"status": "pending"}
    )
    status = load_status(tmp_path)
    assert status is not None
    requeued = load_task_state(tmp_path, task.task_id, status.policy_sha256)
    assert requeued is not None
    assert requeued.status == "pending"
    assert requeued.blocked_reason is None
    assert requeued.attempts == state.attempts


def test_web_app_rolls_back_state_when_intake_requeue_write_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task, _intake, state, _provider = _prepare_due_automation_retry(tmp_path)
    before = _retry_fact_bytes(tmp_path, task.task_id, state.policy_sha256)
    wake_calls: list[str] = []
    monkeypatch.setattr(
        web_app.AutomationCoordinator,
        "wake",
        lambda _self: wake_calls.append(task.task_id) or True,
    )
    monkeypatch.setattr(
        web_app,
        "save_intake",
        lambda *_args: (_ for _ in ()).throw(OSError("write failed")),
    )

    response = _client(tmp_path).post(
        f"/api/learning/items/{task.task_id}/retry-automation"
    )

    assert response.status_code == 409
    assert _retry_fact_bytes(tmp_path, task.task_id, state.policy_sha256) == before
    assert wake_calls == []


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
        "paid_authorized": False,
        "auto_new_favorites_enabled": False,
        "auto_new_favorites_active": False,
    }


def test_web_app_configures_authorizes_and_disables_automation_without_calls(
    tmp_path: Path,
) -> None:
    connect(
        tmp_path, name="note", preset="mimo", secret_value="fake-key", model="mimo-v2.5"
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="note")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="note")
    client = _client(tmp_path)

    configured = client.post(
        "/api/automation/configure",
        json={
            "default_output": "complete_note",
            "auto_organize_new_favorites": False,
            "check_interval_minutes": 5,
            "max_items_per_tick": 1,
        },
    )
    authorized = client.post("/api/automation/authorize", json={"confirm_paid": True})
    disabled = client.post("/api/automation/disable")
    status = web_app.load_automation_status(tmp_path)
    expected_settings_sha256 = web_app.settings_sha256(web_app.load_settings(tmp_path))

    assert configured.status_code == 200
    assert configured.json()["enabled"] is False
    assert configured.json()["paid_authorized"] is False
    assert configured.json()["auto_new_favorites_enabled"] is False
    assert configured.json()["auto_new_favorites_active"] is False
    assert configured.json()["default_output"] == "complete_note"
    assert configured.json()["check_interval_minutes"] == 5
    assert "check_interval_seconds" not in configured.json()
    assert authorized.json()["enabled"] is True
    assert authorized.json()["paid_authorized"] is True
    assert authorized.json()["auto_new_favorites_enabled"] is False
    assert authorized.json()["auto_new_favorites_active"] is False
    assert disabled.json()["enabled"] is False
    assert disabled.json()["paid_authorized"] is False
    assert status is not None
    assert status.policy.writer.settings_sha256 == expected_settings_sha256
    assert status.policy.reviewer.settings_sha256 == expected_settings_sha256
    assert status.policy.check_interval_minutes == 5
    assert "fake-key" not in configured.text + authorized.text + disabled.text

    legacy_seconds = client.post(
        "/api/automation/configure",
        json={
            "default_output": "complete_note",
            "auto_organize_new_favorites": False,
            "check_interval_seconds": 300,
            "max_items_per_tick": 1,
        },
    )
    assert legacy_seconds.status_code == 422


def test_web_storage_exposes_current_root_and_saves_the_next_launcher_root(
    tmp_path: Path,
) -> None:
    current_root = (tmp_path / "current-library").resolve()
    current_root.mkdir()
    next_root = (tmp_path / "next-library").resolve()
    config_path = tmp_path / "local-app-data" / "LearnNest" / "launcher.json"
    client = TestClient(
        web_app.create_web_app(current_root, launcher_config_path=config_path)
    )

    initial = client.get("/api/storage")
    changed = client.put(
        "/api/storage/output-root", json={"output_root": str(next_root)}
    )

    assert initial.status_code == 200
    assert initial.json() == {
        "current_output_root": str(current_root),
        "next_output_root": str(current_root),
        "restart_required": False,
    }
    assert changed.status_code == 200
    assert changed.json() == {
        "current_output_root": str(current_root),
        "next_output_root": str(next_root),
        "restart_required": True,
    }
    assert load_launcher_config(config_path).output_root == str(next_root)
    assert next_root.is_dir()


def test_web_storage_rejects_a_relative_or_unwritable_launcher_root(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "local-app-data" / "LearnNest" / "launcher.json"
    client = TestClient(
        web_app.create_web_app(tmp_path, launcher_config_path=config_path)
    )

    response = client.put(
        "/api/storage/output-root", json={"output_root": "relative-output"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "保存位置必须填写绝对路径。"
    assert not config_path.exists()


def test_web_automation_authorization_requires_explicit_paid_confirmation(
    tmp_path: Path,
) -> None:
    connect(
        tmp_path, name="note", preset="mimo", secret_value="fake-key", model="mimo-v2.5"
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="note")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="note")
    client = _client(tmp_path)
    client.post(
        "/api/automation/configure",
        json={
            "default_output": "complete_note",
            "auto_organize_new_favorites": False,
            "check_interval_minutes": 5,
            "max_items_per_tick": 1,
        },
    )

    for payload in ({}, {"confirm_paid": False}, {"confirm_paid": True, "extra": 1}):
        assert client.post("/api/automation/authorize", json=payload).status_code == 422
    assert client.get("/api/automation/status").json()["enabled"] is False


def test_web_automation_authorization_refreshes_unchanged_note_identity_after_audio_setup(
    tmp_path: Path,
) -> None:
    connect(
        tmp_path,
        name="note",
        preset="mimo",
        secret_value="fake-note-key",
        model="mimo-v2.5",
    )
    set_role_binding(tmp_path, role="note_writer", connection_name="note")
    set_role_binding(tmp_path, role="note_reviewer", connection_name="note")
    client = _client(tmp_path)
    client.post(
        "/api/automation/configure",
        json={
            "default_output": "complete_note_with_audio",
            "auto_organize_new_favorites": False,
            "check_interval_minutes": 5,
            "max_items_per_tick": 1,
        },
    )
    connect(
        tmp_path,
        name="podcast",
        preset="deepseek",
        secret_value="fake-podcast-key",
        model="deepseek-v4-pro",
    )
    set_role_binding(tmp_path, role="podcast", connection_name="podcast")
    connect(tmp_path, name="voice", preset="windows-tts")
    set_role_binding(tmp_path, role="tts", connection_name="voice")

    response = client.post("/api/automation/authorize", json={"confirm_paid": True})

    status = web_app.load_automation_status(tmp_path)
    expected_settings_sha256 = web_app.settings_sha256(web_app.load_settings(tmp_path))
    assert response.status_code == 200
    assert status is not None
    assert status.policy.writer.settings_sha256 == expected_settings_sha256
    assert status.policy.reviewer.settings_sha256 == expected_settings_sha256


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
    downloader_marker = object()

    class ConnectedLogin:
        def current_session(self) -> dict[str, Any]:
            return {"session_id": "connected-session", "status": "connected"}

        def cookie_for(self, session_id: str) -> SecretStr:
            assert session_id == "connected-session"
            return SecretStr("sessionid=runtime-only")

        def shutdown(self) -> None:
            pass

    def fake_process_source(
        source: Any,
        root: Path,
        profile: str,
        *,
        downloader: object | None = None,
    ) -> TaskRecord:
        calls.append(source.input)
        assert root == tmp_path.resolve()
        assert profile == "evidence"
        assert downloader is downloader_marker
        return task

    monkeypatch.setattr(web_app, "process_source", fake_process_source)
    monkeypatch.setattr(
        web_app,
        "YtDlpDownloader",
        lambda *, cookie: (
            downloader_marker
            if cookie.get_secret_value() == "sessionid=runtime-only"
            else None
        ),
        raising=False,
    )
    client = TestClient(
        web_app.create_web_app(tmp_path, douyin_login=ConnectedLogin())  # type: ignore[arg-type]
    )

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
    assert snapshot.json()["library"][0]["action_kind"] == "open_note"
    assert snapshot.json()["library"][0]["note_href"].endswith("/note")
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


def test_learning_note_has_a_readable_shell_and_only_shows_verified_audio(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task = _published_learning_task(
        tmp_path, name="shell-note", markdown="# 正文\n\n![封面](cover.png)"
    )
    (task_dir / "cover.png").write_bytes(b"png")
    safe_audio = task_dir / "podcast.mp3"
    safe_audio.write_bytes(b"mp3")
    client = _client(tmp_path)

    monkeypatch.setattr(
        learning_workspace.LearningWorkspace,
        "audio",
        lambda _self, _item_ref: safe_audio,
    )
    with_audio = client.get(f"/api/learning/items/{task.task_id}/note")
    monkeypatch.setattr(
        learning_workspace.LearningWorkspace,
        "audio",
        lambda _self, _item_ref: (_ for _ in ()).throw(
            learning_workspace.LearningWorkspaceError("音频暂不可用。")
        ),
    )
    without_audio = client.get(f"/api/learning/items/{task.task_id}/note")

    assert with_audio.status_code == 200
    assert "<!doctype html>" in with_audio.text.lower()
    assert "<title>本地课程 · 语栖</title>" in with_audio.text
    assert '<link rel="icon" href="data:," />' in with_audio.text
    assert 'href="/#tasks"' in with_audio.text
    assert 'href="/#library"' not in with_audio.text
    for shell_class in ("note-app-header", "note-rail", "note-reading"):
        assert f'class="{shell_class}"' in with_audio.text
    assert "/static/workspace.css?v=20260831-1" in with_audio.text
    assert 'aria-label="播放本篇笔记的音频"' in with_audio.text
    assert f"/api/learning/items/{task.task_id}/audio" in with_audio.text
    assert f"/api/learning/items/{task.task_id}/images/cover.png" in with_audio.text
    assert without_audio.status_code == 200
    assert 'aria-label="播放本篇笔记的音频"' not in without_audio.text


def test_workspace_page_syncs_the_initial_navigation_hash(tmp_path: Path) -> None:
    page = _client(tmp_path).get("/")

    assert page.status_code == 200
    assert "function syncInitialHashBookmark()" in page.text
    assert (
        'document.querySelector(`.bookmark[href="${CSS.escape(hash)}"]`)' in page.text
    )


def test_workspace_page_uses_the_flat_three_view_shell_and_real_video_entry(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    page = client.get("/").text
    script = client.get("/static/workspace.js").text
    stylesheet = client.get("/static/workspace.css").text

    assert page.count("data-view-panel=") == 3
    for view in ("tasks", "sources", "settings"):
        assert f'data-view="{view}"' in page
        assert f'data-view-panel="{view}"' in page
    for element_id in (
        "task-console",
        "task-spine",
        "task-search",
        "task-filter-empty",
        "task-table-heading",
        "task-insight-rail",
        "recent-activity",
        "task-system-status",
        "task-workbench",
        "task-focus",
        "task-list",
        "task-detail-view",
        "task-detail",
        "back-to-tasks",
        "single-video-dialog",
        "single-video-output",
        "storage-form",
        "output-root",
        "provider-key-field",
        "check-connection-dialog",
        "confirm-check-connection",
        "automation-access",
        "automation-authorization-dialog",
    ):
        assert f'id="{element_id}"' in page
    assert page.count("data-open-single-video") == 1
    assert '"local-asr": "local-asr"' in script
    assert '"local-ocr": "local-ocr"' in script
    assert 'id="confirm-paid"' not in page
    assert "每次确认只发送一次，不自动重试" in page
    assert "不计入任务每日调用限额" in page
    assert "云端连接确认后发送 1 次真实请求" in page
    assert "检查不会调用真实 Provider" not in page
    assert "runProviderConnectionCheck" in script
    assert "JSON.stringify({ confirm_paid: true })" in script
    assert "可能产生少量费用" in page
    assert "付费整理许可" in page
    assert "--workspace-max: 2048px" in stylesheet
    assert ".app-header-inner { max-width: var(--workspace-max)" in stylesheet
    assert ".page-header { max-width: var(--workspace-max)" in stylesheet
    assert ".task-page-header { max-width: var(--workspace-max);" in stylesheet
    assert ".task-console { max-width: var(--workspace-max)" in stylesheet
    assert "grid-template-columns: 232px minmax(0, 1fr) 300px" in stylesheet
    assert ".task-table-heading {" in stylesheet
    assert ".task-insight-rail {" in stylesheet
    assert ".source-workspace { max-width: var(--workspace-max)" in stylesheet
    assert ".settings-workspace { max-width: var(--workspace-max)" in stylesheet
    assert 'id="favorite-folders"' in page
    assert 'class="favorite-browser"' in page
    assert ".favorite-browser {" in stylesheet
    assert "grid-template-columns: 210px minmax(0, 1fr)" in stylesheet
    assert "repeat(auto-fill, minmax(210px, 1fr))" in stylesheet
    assert "activeFavoriteFolderId" in script
    assert "data-favorite-folder" in script
    assert "自动加入新收藏" in page
    assert "不会自动加入抖音新收藏" in page
    assert "检查间隔（分钟）" in page
    assert (
        'name="check_interval_minutes" type="number" min="1" max="60" value="30"'
        in page
    )
    assert "检查间隔（秒）" not in page
    assert (
        "automationForm.elements.check_interval_minutes.value = "
        "status.check_interval_minutes" in script
    )
    assert (
        'check_interval_minutes: Number(form.get("check_interval_minutes"))' in script
    )
    assert "check_interval_seconds" not in script
    assert "自动执行权限" not in page
    assert "开启自动整理？" not in page
    assert "mobile-primary-action" not in page
    assert "学习库" not in page
    assert "function showView" in script
    assert "api(`/api/learning/uploads?name=${encodeURIComponent(file.name)}`" in script
    assert 'api("/api/learning/submit"' in script
    assert 'class="app-header"' in page
    assert "function renderTaskFocus(items)" in script
    assert "function renderRecentActivity(items)" in script
    assert "function renderTaskSystemStatus(status)" in script
    assert "activeTaskQuery" in script
    assert "没有匹配的任务" in page
    assert "function showTaskWorkbench()" in script
    assert "taskWorkbench.hidden = true;" in script
    assert "taskDetailView.hidden = false;" in script
    assert ".task-focus {" in stylesheet
    assert ".task-detail-view {" in stylesheet
    assert ".production-track {" in stylesheet
    for element_id in (
        "source-console",
        "source-type-rail",
        "source-canvas",
        "source-insight-rail",
        "settings-insight-rail",
        "settings-summary-output",
        "settings-summary-license",
        "settings-summary-favorites",
        "settings-summary-interval",
    ):
        assert f'id="{element_id}"' in page
    assert "/static/workspace.css?v=20260831-1" in page
    assert "/static/workspace.js?v=20260831-1" in page
    assert ".source-console {" in stylesheet
    assert "grid-template-columns: 232px minmax(0, 1fr) 300px" in stylesheet
    assert ".source-insight-rail {" in stylesheet
    assert ".settings-insight-rail {" in stylesheet
    assert (
        ".task-console.has-detail .task-canvas { grid-column: 2 / -1; }" in stylesheet
    )
    assert ".note-reading {" in stylesheet


def test_workspace_ui_responses_revalidate_static_assets(tmp_path: Path) -> None:
    client = _client(tmp_path)

    page = client.get("/")
    stylesheet = client.get("/static/workspace.css")
    script = client.get("/static/workspace.js")

    assert page.headers["cache-control"] == "no-cache"
    assert stylesheet.headers["cache-control"] == "no-cache"
    assert script.headers["cache-control"] == "no-cache"


def test_workspace_settings_polling_preserves_dirty_forms_and_uses_inline_feedback(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    script = client.get("/static/workspace.js").text

    assert "const dirtySettingsForms = new Set();" in script
    assert "function settingsFormNeedsProtection(form)" in script
    assert "!settingsFormNeedsProtection(providerForm)" in script
    assert "!settingsFormNeedsProtection(providerCapabilityList)" in script
    assert (
        "loadProviderSettings(automationForm.elements.default_output.value, true)"
        in script
    )
    assert (
        "if (protectDirty && (settingsFormNeedsProtection(providerForm) || settingsFormNeedsProtection(providerCapabilityList))) return;"
        in script
    )
    assert "const mutationRevision = providerSettingsMutationRevision;" in script
    assert (
        "if (protectDirty && mutationRevision !== providerSettingsMutationRevision) return;"
        in script
    )
    assert "providerFeedback.textContent" in script
    assert "const options = role.options.map" in script
    assert "role.hint || role.state" in script
    assert "settings.roles?.[capability] || []" in script
    assert "function renderProviderCapabilityCard" in script
    assert "window.setTimeout(() => refresh(), document.hidden ? 5000 : 2000)" in script


def test_workspace_script_uses_explicit_action_kinds_for_all_public_states(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    script = client.get("/static/workspace.js").text
    stylesheet = client.get("/static/workspace.css").text

    for state in (
        "materials_ready",
        "waiting_setup",
        "waiting_authorization",
        "queued",
        "organizing",
        "partial_ready",
        "needs_action",
        "ready",
    ):
        assert f'{state}: "' in script
    for action_kind in (
        "open_note",
        "open_settings",
        "open_automation",
        "open_sources",
        "open_single_video",
        "start_automation",
        "retry_automation",
        "resume_task",
    ):
        assert f'action === "{action_kind}"' in script
    assert 'action !== "continue"' in script
    assert 'item.state === "ready" ? "open" : "continue"' not in script
    assert 'data-action="${escapeHtml(item.action_kind)}"' in script
    assert "partial_ready" in stylesheet
    assert "waiting_setup" in stylesheet
    assert "waiting_authorization" in stylesheet
    assert ".detail-heading > div { min-width: 0; }" in stylesheet
    assert "overflow-wrap: anywhere" in stylesheet
    assert 'data-delete-item-ref="${escapeHtml(item.item_ref)}"' in script
    assert 'data-pause-item-ref="${escapeHtml(item.item_ref)}"' in script
    assert 'if (item.manually_paused) return "已暂停"' in script
    assert 'if (item.failure_reason) return "处理已停止"' in script
    assert "失败阶段" in script
    assert "/api/learning/items/${encodeURIComponent(itemRef)}/pause" in script
    assert "/api/learning/items/${encodeURIComponent(itemRef)}/resume" in script
    assert "/api/learning/trash" in script
    assert "/restore" in script
    assert 'method: "DELETE"' in script
    assert "移入回收区" in client.get("/").text
    assert 'data-filter="paused"' in client.get("/").text
    assert 'data-filter="trash"' in client.get("/").text
    assert 'id="trash-list"' in client.get("/").text
    assert 'if (item.manually_paused) return "paused";' in script
    assert "counts.paused" in script
    assert "项已暂停" in script
    assert 'item.state === "organizing" && !item.manually_paused' in script
    assert "confirmPaid.checked = true" not in script
    assert "status.paid_authorized" in script
    assert "付费整理许可已开启" in script
    assert "自动加入新收藏" in script
    action_block = script.split("async function actOnItem", maxsplit=1)[1].split(
        "uploadForm.addEventListener", maxsplit=1
    )[0]
    assert 'document.querySelector("#settings")?.scrollIntoView' in action_block
    assert "authorizeAutomationButton?.focus" in action_block
    assert "/api/automation/authorize" not in action_block
    assert action_block.index('action !== "continue"') < action_block.index("/continue")
    assert "@media (max-width: 760px)" in stylesheet
    assert (
        ".learning-row { padding: 15px 10px; grid-template-columns: minmax(0, 1fr) auto;"
        in stylesheet
    )
    assert "function renderSourceJobs" in script
    assert '"材料已加入收件箱"' in script
    assert "source_input" not in script


def test_learning_job_api_replays_only_a_safe_public_projection(tmp_path: Path) -> None:
    source = tmp_path / "private-lesson.mp4"
    source.write_bytes(b"video")
    client = _client(tmp_path)

    submitted = client.post("/api/learning/submit", json={"source": str(source)})
    job_id = submitted.json()["job_id"]
    listed = client.get("/api/learning/jobs")
    item = client.get(f"/api/learning/jobs/{job_id}")

    assert submitted.status_code == 202
    assert listed.status_code == item.status_code == 200
    assert item.json()["job_id"] == job_id
    for payload in (listed.text, item.text):
        assert str(source) not in payload
        assert "private-lesson.mp4" not in payload


def test_douyin_webui_keeps_login_and_favorite_vocabulary_human_facing(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    page = client.get("/").text
    script = client.get("/static/workspace.js").text

    for visible_text in (
        "连接抖音",
        "抖音收藏",
        "默认收藏夹",
        "手动同步",
        "打开抖音验证窗口",
        "当前 Windows 用户加密",
        "手机号验证码",
        "抖音 App 扫码确认",
        "通过后才显示已连接",
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
    assert "loginMessage.textContent = state.message" in script
    assert 'state.status === "disconnected"' in script
    assert 'api("/api/douyin/login/browser"' in script
    assert 'api("/api/douyin/login/current"' in script
    assert "let syncFavoritesAfterLogin = false;" in script
    assert "let favoriteStatusProtected = false;" in script
    assert 'if (state.status === "connected" && syncFavoritesAfterLogin)' in script
    assert "if (!favoriteStatusProtected)" in script
    assert "登录有效，但收藏同步未完成：${error.message}" in script
    assert "具体原因见收藏状态" not in script
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
