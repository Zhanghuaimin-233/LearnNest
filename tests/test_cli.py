from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from typer.testing import CliRunner

from learnnest.cli import app
from learnnest.assisted_note_generation import (
    create_assisted_plan,
    generate_assisted_plan,
    load_assisted_state,
)
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.automation_store import load_task_state
from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.provider_profiles import (
    ProviderSettings,
    connect,
    load_settings,
    save_settings,
    set_role_binding,
    settings_sha256,
)
from learnnest.provider_service import execute_direct_provider_call
from learnnest.task_store import write_task_atomic


runner = CliRunner()


def _write_assisted_cli_task(root: Path, task_id: str, text: str) -> None:
    task_dir = root / "视频学习素材" / task_id
    task_dir.mkdir(parents=True)
    pack = ContentPack(
        task_id=task_id,
        source_fingerprint=f"fingerprint-{task_id}",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text=text,
                artifact_path="content_pack.json",
            )
        ],
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2), encoding="utf-8"
    )
    write_task_atomic(
        task_dir,
        TaskRecord(
            task_id=task_id,
            source_path=f"C:/videos/{task_id}.mp4",
            source_fingerprint=pack.source_fingerprint,
            title=task_id,
            stages={"content_pack": StageStatus.COMPLETED},
            artifacts={"content_pack": ["content_pack.json"]},
        ),
    )


def test_help_lists_all_pipeline_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "doctor",
        "process",
        "run",
        "recover",
        "retry",
        "index",
        "history",
        "status",
        "queue",
        "validate",
        "assisted-note",
        "quality-note",
        "podcast",
        "tts",
        "scan",
        "schedule",
        "flow",
        "layout",
    ):
        assert command in result.output


def test_provider_delete_requires_confirmation_and_rejects_bound_connections(
    tmp_path: Path,
) -> None:
    cloud = connect(tmp_path, name="mimo", preset="mimo", secret_value="secret")
    assert cloud.secret_id is not None
    result = runner.invoke(
        app, ["provider", "delete", "mimo", "--output-root", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "--confirm is required" in result.output
    assert "mimo" in load_settings(tmp_path).connections

    set_role_binding(tmp_path, role="note_writer", connection_name="mimo")
    bound = runner.invoke(
        app,
        [
            "provider",
            "delete",
            "mimo",
            "--confirm",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert bound.exit_code == 1
    assert "still bound" in bound.output
    assert "mimo" in load_settings(tmp_path).connections


def test_provider_delete_removes_an_unbound_connection(tmp_path: Path) -> None:
    connection = connect(tmp_path, name="local-asr", preset="local-asr")

    result = runner.invoke(
        app,
        [
            "provider",
            "delete",
            connection.name,
            "--confirm",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "Deleted local-asr\n"
    assert load_settings(tmp_path).connections == {}


def test_assisted_cli_admits_the_real_b_dossier_after_a_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    root = tmp_path / "output"
    _write_assisted_cli_task(root, "task-a", "A material")
    _write_assisted_cli_task(root, "task-b", "B material")
    snapshot = AssistedConnectionSnapshot(
        connection_name="fake",
        provider="fake-openai",
        endpoint_identity="https://example.test/v1",
        model="fake-1",
        adapter_revision="1",
    )
    plan_path = create_assisted_plan(
        root,
        ["task-a", "task-b"],
        writer=snapshot,
        reviewer=snapshot,
        max_role_calls=1,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )
    state_path = plan_path.parent / "state.json"
    plan_state = json.loads(state_path.read_text(encoding="utf-8"))
    first = plan_state["tasks"][0]
    first["status"] = "writer_failed"
    first["writer"].update(
        {"status": "failed", "actual_call_count": 1, "safe_summary": "prior"}
    )
    state_path.write_text(json.dumps(plan_state), encoding="utf-8")
    settings = load_settings(root).model_copy(update={"retries_per_role": 0})
    save_settings(root, ProviderSettings.model_validate(settings.model_dump()))

    with pytest.raises(RuntimeError, match="prior failure"):
        execute_direct_provider_call(
            root,
            "task-a",
            "writer",
            lambda: (_ for _ in ()).throw(RuntimeError("prior failure")),
        )

    class FakeProvider:
        name = "fake-openai"
        model = "fake-1"
        endpoint_identity = "https://example.test/v1"

        def __init__(self) -> None:
            self.calls = 0

        def write_markdown(self, dossier_json: str) -> str:
            self.calls += 1
            assert "B material" in dossier_json
            return "# task-b\n\nB completed."

    provider = FakeProvider()
    monkeypatch.setattr(
        cli, "_assisted_note_provider", lambda *args, **kwargs: (provider, None)
    )

    result = runner.invoke(
        app,
        [
            "assisted-note",
            "generate",
            str(plan_path),
            "--output-root",
            str(root),
        ],
    )

    b_fact = load_task_state(
        root, "manual-writer-task-b", settings_sha256(load_settings(root))
    )
    assert result.exit_code == 0, result.output
    assert provider.calls == 1
    assert load_assisted_state(plan_path).tasks[1].writer.status == "completed"
    assert b_fact is not None
    assert b_fact.attempts[0].status == "completed"


def test_assisted_cli_keeps_b_admission_identity_after_a_local_dossier_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    root = tmp_path / "output"
    _write_assisted_cli_task(root, "task-a", "A material")
    _write_assisted_cli_task(root, "task-b", "B material")
    snapshot = AssistedConnectionSnapshot(
        connection_name="fake",
        provider="fake-openai",
        endpoint_identity="https://example.test/v1",
        model="fake-1",
        adapter_revision="1",
    )
    plan_path = create_assisted_plan(
        root,
        ["task-a", "task-b"],
        writer=snapshot,
        reviewer=snapshot,
        max_role_calls=1,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )
    a_pack = root / "视频学习素材" / "task-a" / "content_pack.json"
    a_pack.write_text(a_pack.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    class FakeProvider:
        name = "fake-openai"
        model = "fake-1"
        endpoint_identity = "https://example.test/v1"

        def __init__(self) -> None:
            self.calls = 0

        def write_markdown(self, dossier_json: str) -> str:
            self.calls += 1
            assert "B material" in dossier_json
            return "# task-b\n\nB completed."

    provider = FakeProvider()
    monkeypatch.setattr(
        cli, "_assisted_note_provider", lambda *args, **kwargs: (provider, None)
    )

    result = runner.invoke(
        app,
        [
            "assisted-note",
            "generate",
            str(plan_path),
            "--output-root",
            str(root),
        ],
    )

    policy_sha = settings_sha256(load_settings(root))
    assert result.exit_code == 0, result.output
    assert provider.calls == 1
    assert load_task_state(root, "manual-writer-task-a", policy_sha) is None
    assert load_task_state(root, "manual-writer-task-b", policy_sha) is not None
    state = load_assisted_state(plan_path)
    assert state.tasks[0].status == "local_recovery_failed"
    assert state.tasks[1].writer.status == "completed"


def test_assisted_cli_reviewer_admits_b_after_a_has_no_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    root = tmp_path / "output"
    _write_assisted_cli_task(root, "task-a", "A material")
    _write_assisted_cli_task(root, "task-b", "B material")
    snapshot = AssistedConnectionSnapshot(
        connection_name="fake",
        provider="fake-openai",
        endpoint_identity="https://example.test/v1",
        model="fake-1",
        adapter_revision="1",
    )
    plan_path = create_assisted_plan(
        root,
        ["task-a", "task-b"],
        writer=snapshot,
        reviewer=snapshot,
        max_role_calls=1,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    class DraftProvider:
        name = "fake-openai"
        model = "fake-1"
        endpoint_identity = "https://example.test/v1"

        def write_markdown(self, dossier_json: str) -> str:
            return "# Draft\n\nReady for review."

        def review_markdown(self, dossier_json: str, candidate_markdown: str) -> str:
            assert "B material" in dossier_json
            assert "Ready for review" in candidate_markdown
            return "# Reviewed\n\nB reviewed."

    provider = DraftProvider()
    generate_assisted_plan(plan_path, root, provider)
    state_path = plan_path.parent / "state.json"
    plan_state = json.loads(state_path.read_text(encoding="utf-8"))
    first = plan_state["tasks"][0]
    first["status"] = "writer_failed"
    first["writer"].update(
        {"status": "failed", "actual_call_count": 1, "safe_summary": "no draft"}
    )
    state_path.write_text(json.dumps(plan_state), encoding="utf-8")
    settings = load_settings(root).model_copy(update={"retries_per_role": 0})
    save_settings(root, ProviderSettings.model_validate(settings.model_dump()))
    with pytest.raises(RuntimeError, match="prior review failure"):
        execute_direct_provider_call(
            root,
            "task-a",
            "reviewer",
            lambda: (_ for _ in ()).throw(RuntimeError("prior review failure")),
        )
    monkeypatch.setattr(
        cli, "_assisted_note_provider", lambda *args, **kwargs: (provider, None)
    )

    result = runner.invoke(
        app,
        [
            "assisted-note",
            "review",
            str(plan_path),
            "--output-root",
            str(root),
        ],
    )

    b_fact = load_task_state(
        root, "manual-reviewer-task-b", settings_sha256(load_settings(root))
    )
    assert result.exit_code == 0, result.output
    assert load_assisted_state(plan_path).tasks[1].reviewer.status == "completed"
    assert b_fact is not None
    assert b_fact.attempts[0].status == "completed"


def test_assisted_cli_rejects_duplicate_frozen_dossier_before_provider_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    root = tmp_path / "output"
    _write_assisted_cli_task(root, "task-a", "A material")
    _write_assisted_cli_task(root, "task-b", "B material")
    snapshot = AssistedConnectionSnapshot(
        connection_name="fake",
        provider="fake-openai",
        endpoint_identity="https://example.test/v1",
        model="fake-1",
        adapter_revision="1",
    )
    plan_path = create_assisted_plan(
        root,
        ["task-a", "task-b"],
        writer=snapshot,
        reviewer=snapshot,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["tasks"][1]["dossier_sha256"] = payload["tasks"][0]["dossier_sha256"]
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    constructions = 0

    def factory(*args: object, **kwargs: object) -> tuple[object, None]:
        nonlocal constructions
        constructions += 1
        return object(), None

    monkeypatch.setattr(cli, "_assisted_note_provider", factory)
    result = runner.invoke(
        app,
        [
            "assisted-note",
            "generate",
            str(plan_path),
            "--output-root",
            str(root),
        ],
    )

    assert result.exit_code == 1
    assert "duplicate dossier identity" in result.output
    assert constructions == 0


def test_process_uses_current_directory_as_the_default_output_root(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"video")
    recorded: dict[str, object] = {}

    def fake_process(video_path: Path, output_root: Path, profile: str) -> object:
        recorded.update(
            {"video_path": video_path, "output_root": output_root, "profile": profile}
        )
        return type("Task", (), {"task_id": "20260711-a1b2c3d4"})()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "process_video", fake_process)

    result = runner.invoke(app, ["process", str(video_path), "--profile", "note"])

    assert result.exit_code == 0
    assert recorded == {
        "video_path": video_path,
        "output_root": tmp_path,
        "profile": "note",
    }
    assert "20260711-a1b2c3d4" in result.output


def test_process_and_run_render_lock_contention_as_stable_user_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli
    from learnnest.locks import LockUnavailable

    video = tmp_path / "locked.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        cli,
        "process_video",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LockUnavailable("task lock unavailable: locked")
        ),
    )
    process_result = runner.invoke(
        app,
        ["process", str(video), "--output-root", str(tmp_path)],
    )
    _write_cli_task(tmp_path)
    monkeypatch.setattr(
        cli,
        "rerun_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LockUnavailable("task lock unavailable: locked")
        ),
    )
    run_result = runner.invoke(
        app,
        [
            "run",
            "20260711-a1b2c3d4",
            "--from",
            "ocr",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert process_result.exit_code == 1
    assert "task is still running" in process_result.output
    assert run_result.exit_code == 1
    assert "task is still running: 20260711-a1b2c3d4" in run_result.output


def test_process_dry_run_is_side_effect_free_and_skips_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"video")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "process_video",
        lambda *args, **kwargs: pytest.fail("pipeline must not run during dry-run"),
    )
    monkeypatch.setattr(
        cli,
        "preflight_sources",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {
                "ok": True,
                "items": [
                    type(
                        "Item",
                        (),
                        {
                            "task_id": "20260711-a1b2c3d4",
                            "planned_directory": "lesson--a1b2c3d4",
                            "duplicate": False,
                        },
                    )()
                ],
                "issues": [],
            },
        )(),
        raising=False,
    )
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    result = runner.invoke(app, ["process", str(video_path), "--dry-run"])

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert result.exit_code == 0
    assert "DRY-RUN" in result.output
    assert "20260711-a1b2c3d4" in result.output
    assert before == after


def test_process_rejects_multiple_input_modes(tmp_path: Path) -> None:
    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"video")
    tasks = tmp_path / "tasks.txt"
    tasks.write_text(str(video_path), encoding="utf-8")

    result = runner.invoke(
        app,
        ["process", str(video_path), "--tasks", str(tasks), "--dry-run"],
    )

    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_process_tasks_runs_serial_batch_and_reports_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    first.write_bytes(b"video")
    second.write_bytes(b"video")
    tasks = tmp_path / "tasks.txt"
    tasks.write_text(f"{first}\n{second}\n", encoding="utf-8")
    recorded: dict[str, object] = {}

    def fake_batch(sources, output_root, profile):
        recorded.update(
            {"sources": sources, "output_root": output_root, "profile": profile}
        )
        return type(
            "Manifest",
            (),
            {
                "batch_id": "20260711T120000Z-a1b2c3d4",
                "completed_count": 1,
                "failed_count": 1,
                "skipped_count": 0,
            },
        )()

    monkeypatch.setattr(cli, "run_batch", fake_batch, raising=False)

    result = runner.invoke(
        app,
        ["process", "--tasks", str(tasks), "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert len(recorded["sources"]) == 2
    assert recorded["output_root"] == tmp_path
    assert recorded["profile"] == "evidence"
    assert "completed=1" in result.output
    assert "failed=1" in result.output


def test_process_single_url_uses_source_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    recorded: dict[str, object] = {}

    def fake_process(source, output_root, profile):
        recorded.update(
            {"source": source, "output_root": output_root, "profile": profile}
        )
        return type("Task", (), {"task_id": "20260711-a1b2c3d4"})()

    monkeypatch.setattr(cli, "process_source", fake_process)

    result = runner.invoke(
        app,
        [
            "process",
            "https://example.com/watch?v=1",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert recorded["source"].input_type == "url"
    assert recorded["output_root"] == tmp_path


def test_process_passes_force_and_allow_duplicate_to_single_source_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    recorded: dict[str, object] = {}

    def fake_process(source, output_root, profile, *, force, allow_duplicate):
        recorded.update(
            {
                "source": source,
                "output_root": output_root,
                "profile": profile,
                "force": force,
                "allow_duplicate": allow_duplicate,
            }
        )
        return type("Task", (), {"task_id": "20260711-a1b2c3d4"})()

    monkeypatch.setattr(cli, "process_source", fake_process)

    result = runner.invoke(
        app,
        [
            "process",
            "https://example.com/watch?v=1",
            "--force",
            "--allow-duplicate",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert recorded["force"] is True
    assert recorded["allow_duplicate"] is True


def test_recover_dry_run_refuses_a_task_whose_os_lock_is_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from learnnest.locks import task_lock
    from learnnest.models import StageStatus
    from learnnest.task_store import create_task, write_task_atomic

    task = create_task(
        task_id="20260712-locked01",
        source_path=str(tmp_path / "locked.mp4"),
        source_fingerprint="locked-fingerprint",
        title="locked",
    ).model_copy(update={"stages": {"source": StageStatus.RUNNING}})
    task_dir = tmp_path / "视频学习素材" / "locked--locked01"
    write_task_atomic(task_dir, task)

    with task_lock(tmp_path, task.task_id, timeout=0):
        result = runner.invoke(
            app,
            [
                "recover",
                task.task_id,
                "--dry-run",
                "--output-root",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 1
    assert "task is still running" in result.output


def test_retry_planning_and_execution_refuse_a_held_task_lock(
    tmp_path: Path,
) -> None:
    from learnnest.locks import task_lock
    from learnnest.models import StageStatus
    from learnnest.task_store import create_task, write_task_atomic

    task = create_task(
        task_id="20260712-locked02",
        source_path=str(tmp_path / "locked-retry.mp4"),
        source_fingerprint="locked-retry-fingerprint",
        title="locked retry",
    ).model_copy(update={"stages": {"source": StageStatus.FAILED}})
    task_dir = tmp_path / "视频学习素材" / "locked-retry--locked02"
    write_task_atomic(task_dir, task)

    with task_lock(tmp_path, task.task_id, timeout=0):
        result = runner.invoke(
            app,
            [
                "retry",
                task.task_id,
                "--from",
                "source",
                "--output-root",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 1
    assert "task is still running" in result.output


@pytest.mark.parametrize("command", ["podcast", "tts"])
def test_paid_generation_commands_refuse_a_held_task_before_provider_access(
    tmp_path: Path, command: str
) -> None:
    from learnnest.locks import task_lock
    from learnnest.task_store import create_task, write_task_atomic

    task = create_task(
        task_id="20260712-paidlock",
        source_path=str(tmp_path / "paid.mp4"),
        source_fingerprint="paid-lock-fingerprint",
        title="paid lock",
    )
    task_dir = tmp_path / "视频学习素材" / "paid-lock--paidlock"
    write_task_atomic(task_dir, task)

    with task_lock(tmp_path, task.task_id, timeout=0):
        result = runner.invoke(
            app,
            [command, task.task_id, "--output-root", str(tmp_path)],
        )

    assert result.exit_code == 1
    assert "task is still running" in result.output


def test_index_rebuild_reports_an_existing_rebuild_owner(tmp_path: Path) -> None:
    from learnnest.locks import rebuild_lock

    with rebuild_lock(tmp_path, timeout=0):
        result = runner.invoke(
            app,
            ["index", "rebuild", "--output-root", str(tmp_path)],
        )

    assert result.exit_code == 1
    assert "index rebuild is already running" in result.output


def test_batch_resume_command_targets_persisted_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    batch_id = "20260712T120000Z-resume01"
    batch_dir = tmp_path / "视频学习批次" / batch_id
    batch_dir.mkdir(parents=True)
    (batch_dir / "batch.json").write_text("{}", encoding="utf-8")
    recorded: dict[str, object] = {}

    def fake_resume(path):
        recorded["path"] = path
        return type(
            "Manifest",
            (),
            {
                "batch_id": batch_id,
                "completed_count": 2,
                "failed_count": 0,
                "skipped_count": 1,
            },
        )()

    monkeypatch.setattr(cli, "resume_batch", fake_resume, raising=False)

    result = runner.invoke(
        app,
        ["batch", "resume", batch_id, "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert recorded["path"] == batch_dir
    assert "completed=2" in result.output


def test_run_uses_task_id_to_find_a_task_under_the_current_directory(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    task_dir = tmp_path / "视频学习素材" / "renamed-title--a1b2c3d4"
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": "20260711-a1b2c3d4",
                "source_path": "C:/videos/lesson.mp4",
                "source_fingerprint": "a1b2c3d4",
                "title": "renamed title",
            }
        ),
        encoding="utf-8",
    )
    recorded: dict[str, object] = {}

    def fake_rerun(task_dir: Path, from_stage: str) -> object:
        recorded.update({"task_dir": task_dir, "from_stage": from_stage})
        return type("Task", (), {"task_id": "20260711-a1b2c3d4"})()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "rerun_task", fake_rerun)

    result = runner.invoke(app, ["run", "20260711-a1b2c3d4", "--from", "ocr"])

    assert result.exit_code == 0
    assert recorded == {"task_dir": task_dir, "from_stage": "ocr"}


def test_invalid_profile_is_rejected_before_the_pipeline_runs(tmp_path: Path) -> None:
    video_path = tmp_path / "lesson.mp4"
    video_path.write_bytes(b"video")

    result = runner.invoke(app, ["process", str(video_path), "--profile", "unknown"])

    assert result.exit_code == 2
    assert "Invalid value for '--profile'" in result.output


def test_validate_returns_nonzero_when_contract_errors_are_found(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": "20260711-a1b2c3d4",
                "source_path": "C:/videos/lesson.mp4",
                "source_fingerprint": "a1b2c3d4",
                "title": "lesson",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli, "validate_task", lambda task_dir: ["artifact is missing: ocr.json"]
    )

    result = runner.invoke(app, ["validate", "20260711-a1b2c3d4"])

    assert result.exit_code == 1
    assert "artifact is missing: ocr.json" in result.output


def test_validate_reports_an_invalid_task_json_when_its_raw_task_id_matches(
    monkeypatch, tmp_path: Path
) -> None:
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": "20260711-a1b2c3d4",
                "source_path": "C:/videos/lesson.mp4",
                "source_fingerprint": "a1b2c3d4",
                "title": "lesson",
                "stages": {"unknown": "completed"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["validate", "20260711-a1b2c3d4"])

    assert result.exit_code == 1
    assert "invalid task.json" in result.output
    assert "task not found" not in result.output


def test_doctor_starts_each_provider_check_in_an_isolated_worker(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    monkeypatch.chdir(tmp_path)
    calls: list[list[str]] = []

    def fake_run_worker(args: list[str], **kwargs: object) -> CompletedProcess[str]:
        assert kwargs["environment"] == {"HF_HUB_OFFLINE": "1"}
        calls.append(args)
        payload = {
            "provider": "faster-whisper" if args[0] == "doctor-asr" else "paddleocr"
        }
        return CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(cli.providers, "run_worker", fake_run_worker)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert calls == [["doctor-asr"], ["doctor-ocr"]]


def test_readiness_stops_before_provider_checks_when_prerequisites_are_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli.providers,
        "run_worker",
        lambda *args, **kwargs: pytest.fail("provider checks must not start"),
    )
    monkeypatch.setattr(
        cli,
        "preflight_runtime",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {
                "ok": False,
                "issues": [
                    type(
                        "Issue",
                        (),
                        {
                            "severity": "error",
                            "code": "runtime_missing_mimo_api_key",
                            "message": "MIMO_API_KEY is required",
                        },
                    )()
                ],
            },
        )(),
    )

    result = runner.invoke(app, ["readiness", "--output-root", str(tmp_path)])

    assert result.exit_code == 1
    assert "runtime_missing_mimo_api_key" in result.output


def test_flow_requires_explicit_paid_confirmation_before_any_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    monkeypatch.setattr(
        cli,
        "run_schedule_once",
        lambda *args, **kwargs: pytest.fail("monitor must not start"),
    )

    result = runner.invoke(
        app,
        [
            "flow",
            "run",
            "douyin-favorites",
            "--with-podcast",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "--confirm-paid" in result.output


def test_flow_runs_one_named_monitor_and_its_pending_consumer_without_paid_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli
    from learnnest.download_queue import DownloadOutcome
    from learnnest.schedules import ScheduleOutcome

    observed: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "preflight_runtime",
        lambda *args, **kwargs: type("Result", (), {"ok": True, "issues": []})(),
    )
    monkeypatch.setattr(cli, "_verify_provider_workers", lambda *args: [])
    monkeypatch.setattr(cli, "_load_douyin_cookie", lambda _root=None: None)
    monkeypatch.setattr(
        cli,
        "load_schedule",
        lambda path: type("Schedule", (), {"profile": "evidence"})(),
    )
    monkeypatch.setattr(
        cli,
        "run_schedule_once",
        lambda root, schedule_id, **kwargs: (
            observed.update({"schedule_id": schedule_id, "schedule_kwargs": kwargs})
            or ScheduleOutcome(schedule_id, "completed", batch_id="discovery-batch")
        ),
    )
    monkeypatch.setattr(
        cli,
        "run_pending_downloads",
        lambda root, **kwargs: (
            observed.update({"download_kwargs": kwargs})
            or DownloadOutcome(None, 0, 0, 0)
        ),
    )

    result = runner.invoke(
        app,
        ["flow", "run", "douyin-favorites", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert observed["schedule_id"] == "douyin-favorites"
    assert observed["download_kwargs"] == {
        "max_items": 10,
        "profile": "evidence",
        "schedule_id": "douyin-favorites",
        "selection": "oldest",
        "douyin_cookie": None,
    }
    assert "Flow completed" in result.output


def test_status_reads_task_facts_without_requiring_the_sqlite_projection(
    tmp_path: Path,
) -> None:
    _write_cli_task(tmp_path)

    result = runner.invoke(
        app,
        ["status", "20260711-a1b2c3d4", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "active-artifacts: -" in result.output
    assert "attempts=0" in result.output


def test_layout_plan_is_non_destructive_and_migration_requires_confirmation(
    tmp_path: Path,
) -> None:
    (tmp_path / "视频学习笔记").mkdir()

    planned = runner.invoke(app, ["layout", "plan", "--output-root", str(tmp_path)])
    migrate = runner.invoke(app, ["layout", "migrate", "--output-root", str(tmp_path)])

    assert planned.exit_code == 0, planned.output
    assert "交付物" in planned.output
    assert migrate.exit_code == 1
    assert (tmp_path / "视频学习笔记").is_dir()


def _write_cli_task(tmp_path: Path) -> Path:
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": "20260711-a1b2c3d4",
                "source_path": "C:/videos/lesson.mp4",
                "source_fingerprint": "a1b2c3d4",
                "title": "lesson",
            }
        ),
        encoding="utf-8",
    )
    return task_dir


def test_quality_note_provider_maps_legacy_json_mode_to_writer_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import learnnest.cli as cli

    recorded: dict[str, object] = {}

    def fake_provider(config: object) -> object:
        recorded["config"] = config
        return object()

    monkeypatch.setattr(cli, "OpenAICompatibleQualityNoteProvider", fake_provider)

    cli._quality_note_provider(
        {
            "LEARNNEST_NOTE_API_KEY": "test-compatible-secret",
            "LEARNNEST_NOTE_BASE_URL": "https://example.invalid/v1",
            "LEARNNEST_NOTE_MODEL": "test-compatible-model",
            "LEARNNEST_NOTE_JSON_MODE": "json_schema",
        }
    )

    config = recorded["config"]
    assert config.json_response_mode == "json_schema"
    assert config.writer_strategy_override == "native_json_schema"


def test_note_safe_input_budget_defaults_to_sixty_four_thousand() -> None:
    import learnnest.cli as cli

    assert cli._note_safe_input_tokens({}) == 64_000


def test_podcast_external_script_does_not_require_mimo_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    _write_cli_task(tmp_path)
    external = tmp_path / "podcast.json"
    external.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "build_and_activate_external_podcast",
        lambda task_dir, raw_path, output_root: type(
            "Task", (), {"task_id": "20260711-a1b2c3d4"}
        )(),
        raising=False,
    )

    result = runner.invoke(
        app,
        [
            "podcast",
            "20260711-a1b2c3d4",
            "--external-script",
            str(external),
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0


def test_tts_command_uses_a_frozen_mimo_provider_and_output_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli
    from learnnest.provider_profiles import (
        connect,
        freeze_role_bindings,
        set_role_binding,
    )
    from learnnest.task_store import load_task, write_task_atomic

    task_dir = _write_cli_task(tmp_path)
    connection = connect(
        tmp_path, name="mimo-tts", preset="mimo-tts", secret_value="test-secret"
    )
    set_role_binding(tmp_path, role="tts", connection_name=connection.name)
    task = load_task(task_dir).model_copy(
        update={"provider_bindings": freeze_role_bindings(tmp_path)}
    )
    write_task_atomic(task_dir, task)
    recorded: dict[str, object] = {}

    class FakeProvider:
        billing = "paid"

    def fake_provider(root, binding):
        recorded["root"] = root
        recorded["binding"] = binding
        return FakeProvider()

    def fake_generate(task_dir, provider, output_root, *, style_instruction):
        recorded.update(
            {
                "task_dir": task_dir,
                "provider": provider,
                "output_root": output_root,
                "style": style_instruction,
            }
        )
        return type("Task", (), {"task_id": "20260711-a1b2c3d4"})()

    monkeypatch.setattr(cli, "tts_provider_from_snapshot", fake_provider)
    monkeypatch.setattr(cli, "generate_and_activate_tts", fake_generate, raising=False)

    result = runner.invoke(
        app,
        [
            "tts",
            "20260711-a1b2c3d4",
            "--style",
            "平静清晰",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "正在请求 MiMo TTS" in result.stdout
    assert recorded["binding"].model_dump(mode="json") == task.provider_bindings[
        "tts"
    ].model_dump(mode="json")
    assert recorded["output_root"] == tmp_path
    assert recorded["style"] == "平静清晰"


def test_tts_command_rejects_legacy_runtime_key_without_a_frozen_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_cli_task(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "MIMO_API_KEY=from-local-runtime-file\n", encoding="utf-8"
    )
    result = runner.invoke(
        app,
        ["tts", "20260711-a1b2c3d4", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "frozen tts provider binding" in result.output
    assert "from-local-runtime-file" not in result.output


def test_tts_command_windows_binding_bypasses_paid_admission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli
    from learnnest.provider_profiles import (
        connect,
        freeze_role_bindings,
        set_role_binding,
    )
    from learnnest.task_store import load_task, write_task_atomic

    task_dir = _write_cli_task(tmp_path)
    connection = connect(
        tmp_path,
        name="windows-voice",
        preset="windows-tts",
        voice="Huihui Desktop",
    )
    set_role_binding(tmp_path, role="tts", connection_name=connection.name)
    write_task_atomic(
        task_dir,
        load_task(task_dir).model_copy(
            update={"provider_bindings": freeze_role_bindings(tmp_path)}
        ),
    )
    monkeypatch.setattr(
        cli,
        "execute_direct_provider_call",
        lambda *args, **kwargs: pytest.fail("Windows TTS must bypass paid admission"),
    )
    monkeypatch.setattr(
        cli,
        "generate_and_activate_tts",
        lambda task_dir, provider, output_root, *, style_instruction: type(
            "Task", (), {"task_id": "20260711-a1b2c3d4"}
        )(),
    )

    result = runner.invoke(
        app, ["tts", "20260711-a1b2c3d4", "--output-root", str(tmp_path)]
    )

    assert result.exit_code == 0
    assert "Windows 本地语音" in result.stdout


def test_tts_command_mimo_zero_cap_blocks_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli
    from learnnest.provider_profiles import (
        ProviderSettings,
        connect,
        freeze_role_bindings,
        load_settings,
        save_settings,
        set_role_binding,
    )
    from learnnest.task_store import load_task, write_task_atomic

    task_dir = _write_cli_task(tmp_path)
    connection = connect(
        tmp_path, name="mimo-tts", preset="mimo-tts", secret_value="test-secret"
    )
    set_role_binding(tmp_path, role="tts", connection_name=connection.name)
    write_task_atomic(
        task_dir,
        load_task(task_dir).model_copy(
            update={"provider_bindings": freeze_role_bindings(tmp_path)}
        ),
    )
    settings = load_settings(tmp_path).model_copy(update={"global_calls_per_day": 0})
    save_settings(tmp_path, ProviderSettings.model_validate(settings.model_dump()))
    constructions = 0

    def fail_factory(*args, **kwargs):
        nonlocal constructions
        constructions += 1
        raise AssertionError("MiMo adapter must not be constructed at cap zero")

    monkeypatch.setattr(cli, "tts_provider_from_snapshot", fail_factory)
    result = runner.invoke(
        app, ["tts", "20260711-a1b2c3d4", "--output-root", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert constructions == 0


@pytest.mark.parametrize(
    ("command", "expected_resources"),
    [
        ("podcast", ["network", "llm"]),
        ("tts", ["network", "tts", "ffmpeg"]),
    ],
)
def test_paid_commands_acquire_cross_process_resource_slots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    expected_resources: list[str],
) -> None:
    from contextlib import contextmanager

    import learnnest.cli as cli

    _write_cli_task(tmp_path)
    monkeypatch.setenv("MIMO_API_KEY", "test-secret-not-real")
    events: list[str] = []

    class RecordingScheduler:
        def __init__(self, output_root):
            assert output_root == tmp_path

        @contextmanager
        def acquire(self, resource):
            events.append(resource)
            yield 0

    task = type("Task", (), {"task_id": "20260711-a1b2c3d4"})()
    monkeypatch.setattr(cli, "ResourceScheduler", RecordingScheduler, raising=False)
    if command == "podcast":
        monkeypatch.setattr(cli, "MimoPodcastProvider", lambda secret: object())
        monkeypatch.setattr(
            cli,
            "generate_and_activate_podcast",
            lambda task_dir, provider, output_root: task,
        )
    else:
        from learnnest.provider_profiles import (
            connect,
            freeze_role_bindings,
            set_role_binding,
        )
        from learnnest.task_store import load_task, write_task_atomic

        task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
        connection = connect(
            tmp_path, name="mimo-tts", preset="mimo-tts", secret_value="test-secret"
        )
        set_role_binding(tmp_path, role="tts", connection_name=connection.name)
        task_with_binding = load_task(task_dir).model_copy(
            update={"provider_bindings": freeze_role_bindings(tmp_path)}
        )
        write_task_atomic(task_dir, task_with_binding)

        class FakeProvider:
            billing = "paid"

        monkeypatch.setattr(
            cli, "tts_provider_from_snapshot", lambda root, binding: FakeProvider()
        )
        monkeypatch.setattr(
            cli,
            "generate_and_activate_tts",
            lambda task_dir, provider, output_root, *, style_instruction: task,
        )

    result = runner.invoke(
        app,
        [command, "20260711-a1b2c3d4", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert events == expected_resources


def test_recover_dry_run_reports_plan_without_rerunning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    _write_cli_task(tmp_path)
    monkeypatch.setattr(
        cli,
        "plan_recovery",
        lambda task_dir: type(
            "Plan",
            (),
            {
                "from_stage": "ocr",
                "requires_paid": False,
                "reasons": ["missing artifact: ocr.json"],
            },
        )(),
    )
    monkeypatch.setattr(
        cli,
        "rerun_task",
        lambda *args, **kwargs: pytest.fail("dry-run must not rerun"),
    )

    result = runner.invoke(
        app,
        [
            "recover",
            "20260711-a1b2c3d4",
            "--dry-run",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "from=ocr" in result.output
    assert "missing artifact" in result.output


def test_recover_refuses_to_trigger_a_paid_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    _write_cli_task(tmp_path)
    monkeypatch.setattr(
        cli,
        "plan_recovery",
        lambda task_dir: type(
            "Plan",
            (),
            {
                "from_stage": "tts",
                "requires_paid": True,
                "reasons": ["stage is failed: tts"],
            },
        )(),
    )
    monkeypatch.setattr(
        cli,
        "rerun_task",
        lambda *args, **kwargs: pytest.fail("paid stage must stay explicit"),
    )

    result = runner.invoke(
        app,
        ["recover", "20260711-a1b2c3d4", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "paid stage" in result.output


def test_retry_appends_an_explicit_retry_from_selected_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    task_dir = _write_cli_task(tmp_path)
    recorded: dict[str, object] = {}

    def fake_rerun(path, stage, *, reason, _task_lock_held):
        recorded.update(
            {
                "path": path,
                "stage": stage,
                "reason": reason,
                "task_lock_held": _task_lock_held,
            }
        )
        return type("Task", (), {"task_id": "20260711-a1b2c3d4"})()

    monkeypatch.setattr(cli, "rerun_task", fake_rerun)

    result = runner.invoke(
        app,
        [
            "retry",
            "20260711-a1b2c3d4",
            "--from",
            "ocr",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert recorded == {
        "path": task_dir,
        "stage": "ocr",
        "reason": "retry",
        "task_lock_held": True,
    }


def test_index_rebuild_status_history_and_empty_queue_commands(tmp_path: Path) -> None:
    _write_cli_task(tmp_path)

    rebuilt = runner.invoke(
        app,
        ["index", "rebuild", "--output-root", str(tmp_path)],
    )
    status = runner.invoke(
        app,
        ["index", "status", "--output-root", str(tmp_path)],
    )
    history = runner.invoke(
        app,
        [
            "history",
            "--task-id",
            "20260711-a1b2c3d4",
            "--output-root",
            str(tmp_path),
        ],
    )
    queue = runner.invoke(
        app,
        ["queue", "list", "--output-root", str(tmp_path)],
    )

    assert rebuilt.exit_code == 0
    assert "tasks=1" in rebuilt.output
    assert status.exit_code == 0
    assert "stale=false" in status.output
    assert history.exit_code == 0
    assert "20260711-a1b2c3d4" in history.output
    assert queue.exit_code == 0
    assert "empty" in queue.output


def test_history_falls_back_to_task_facts_when_index_is_missing(tmp_path: Path) -> None:
    _write_cli_task(tmp_path)

    result = runner.invoke(
        app,
        [
            "history",
            "--task-id",
            "20260711-a1b2c3d4",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "task facts" in result.output
    assert "20260711-a1b2c3d4" in result.output


def test_scan_folder_is_incremental_and_foreground_only(tmp_path: Path) -> None:
    folder = tmp_path / "videos"
    folder.mkdir()
    vault = tmp_path / "vault"

    first = runner.invoke(
        app,
        ["scan", "folder", str(folder), "--output-root", str(vault)],
    )
    second = runner.invoke(
        app,
        ["scan", "folder", str(folder), "--output-root", str(vault)],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "added=0" in first.output
    assert "added=0" in second.output
    assert not any("schtasks" in path.name.casefold() for path in vault.rglob("*"))


def test_foreground_schedule_code_has_no_windows_scheduler_call_path() -> None:
    source_root = Path(__file__).parents[1] / "src" / "learnnest"
    production = "\n".join(
        (source_root / name).read_text(encoding="utf-8").casefold()
        for name in ("cli.py", "schedules.py", "schedule_store.py")
    )

    for forbidden in (
        "schtasks",
        "scheduledtasks",
        "register-scheduledtask",
        "taskschd",
        "winreg",
    ):
        assert forbidden not in production


def test_schedule_add_list_tick_and_disable_are_file_backed(tmp_path: Path) -> None:
    folder = tmp_path / "scheduled-videos"
    folder.mkdir()
    vault = tmp_path / "vault"

    added = runner.invoke(
        app,
        [
            "schedule",
            "add-folder",
            "daily-lessons",
            str(folder),
            "--every-minutes",
            "5",
            "--output-root",
            str(vault),
        ],
    )
    listed = runner.invoke(
        app,
        ["schedule", "list", "--output-root", str(vault)],
    )
    ticked = runner.invoke(
        app,
        ["schedule", "tick", "--output-root", str(vault)],
    )
    disabled = runner.invoke(
        app,
        [
            "schedule",
            "disable",
            "daily-lessons",
            "--output-root",
            str(vault),
        ],
    )

    assert added.exit_code == 0, added.output
    assert listed.exit_code == 0
    assert "daily-lessons\tenabled\tfolder\tinterval" in listed.output
    assert ticked.exit_code == 0, ticked.output
    assert "daily-lessons: completed" in ticked.output
    assert disabled.exit_code == 0
    payload = json.loads(
        (vault / "视频学习批次" / "schedules" / "daily-lessons.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["status"] == "disabled"


def test_queue_run_forwards_worker_bounds_and_reports_partial_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli
    from learnnest.queue_runner import QueueRunResult

    recorded: dict[str, object] = {}

    def fake_run(root, *, workers, max_items):
        recorded.update(root=root, workers=workers, max_items=max_items)
        return [
            QueueRunResult("task-ok", "completed"),
            QueueRunResult(
                "task-paid", "failed", "paid stage requires explicit command"
            ),
        ]

    monkeypatch.setattr(cli, "run_failure_queue", fake_run)

    result = runner.invoke(
        app,
        [
            "queue",
            "run",
            "--workers",
            "2",
            "--max-items",
            "5",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert recorded == {"root": tmp_path, "workers": 2, "max_items": 5}
    assert "task-ok: completed" in result.output
    assert "task-paid: failed" in result.output
