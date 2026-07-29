from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from typer.testing import CliRunner

from learnnest.cli import app


runner = CliRunner()


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
        "note",
        "note-status",
        "validate-note",
        "template",
        "podcast",
        "tts",
        "scan",
        "schedule",
        "flow",
        "layout",
    ):
        assert command in result.output


def test_template_validate_accepts_a_constrained_manifest(tmp_path: Path) -> None:
    from learnnest.note_templates import builtin_template

    template_path = tmp_path / "my-template.json"
    template_path.write_text(
        builtin_template("concept-explanation").model_dump_json(), encoding="utf-8"
    )

    result = runner.invoke(app, ["template", "validate", str(template_path)])

    assert result.exit_code == 0
    assert "Valid template: concept-explanation" in result.output


def test_note_rejects_invalid_template_before_building_a_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    _write_cli_task(tmp_path)
    template_path = tmp_path / "invalid-template.json"
    template_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "template_id": "invalid-template",
                "display_name": "错误模板",
                "frontmatter": {"source_sha256": "forged"},
                "sections": [
                    {
                        "section_id": "facts",
                        "heading": "事实",
                        "semantic_block": "core_facts",
                        "required": True,
                        "min_items": 1,
                        "max_items": 2,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "MimoNoteProvider",
        lambda *args, **kwargs: pytest.fail("provider must not be constructed"),
    )

    result = runner.invoke(
        app,
        [
            "note",
            "20260711-a1b2c3d4",
            "--template",
            str(template_path),
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "frontmatter key is reserved" in result.output


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


@pytest.mark.parametrize("command", ["note", "podcast", "tts"])
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
            "--with-note",
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


def test_note_command_requires_mimo_key_before_building_a_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    _write_cli_task(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "MimoNoteProvider",
        lambda *args, **kwargs: pytest.fail("provider must not be constructed"),
    )
    monkeypatch.setattr(
        cli,
        "MimoNoteReviewer",
        lambda *args, **kwargs: pytest.fail("reviewer must not be constructed"),
    )

    result = runner.invoke(
        app,
        ["note", "20260711-a1b2c3d4", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "MIMO_API_KEY is missing" in result.output


def test_note_command_uses_complete_openai_compatible_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli
    from pydantic import SecretStr

    task_dir = _write_cli_task(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setenv("LEARNNEST_NOTE_API_KEY", "test-compatible-secret")
    monkeypatch.setenv("LEARNNEST_NOTE_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LEARNNEST_NOTE_MODEL", "test-compatible-model")
    monkeypatch.setenv("LEARNNEST_NOTE_PROVIDER", "test-compatible")
    monkeypatch.setenv("LEARNNEST_NOTE_JSON_MODE", "json_schema")
    recorded: dict[str, object] = {}
    provider = object()
    reviewer = object()

    def fake_provider(config: object) -> object:
        recorded["provider_config"] = config
        return provider

    def fake_reviewer(config: object) -> object:
        recorded["reviewer_config"] = config
        return reviewer

    def fake_generate(
        actual_task_dir: Path,
        actual_provider: object,
        output_root: Path,
        *,
        template: object,
        review_mode: str,
        auditor: object,
    ) -> object:
        recorded.update(
            {
                "task_dir": actual_task_dir,
                "provider": actual_provider,
                "auditor": auditor,
                "output_root": output_root,
                "template": template,
                "review_mode": review_mode,
            }
        )
        return type(
            "Result",
            (),
            {
                "task": type("Task", (), {"task_id": "20260711-a1b2c3d4"})(),
                "activated": True,
                "bundle_path": tmp_path / "bundle",
                "review_status": "not_requested",
            },
        )()

    monkeypatch.setattr(cli, "OpenAICompatibleNoteProvider", fake_provider)
    monkeypatch.setattr(cli, "OpenAICompatibleNoteReviewer", fake_reviewer)
    monkeypatch.setattr(cli, "generate_and_activate_v4_note", fake_generate)

    result = runner.invoke(
        app,
        ["note", "20260711-a1b2c3d4", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    config = recorded["provider_config"]
    assert config is recorded["reviewer_config"]
    assert config.provider_name == "test-compatible"
    assert config.model == "test-compatible-model"
    assert config.base_url == "https://example.invalid/v1"
    assert config.json_response_mode == "json_schema"
    assert isinstance(config.api_key, SecretStr)
    assert config.api_key.get_secret_value() == "test-compatible-secret"
    assert recorded == {
        "provider_config": config,
        "reviewer_config": config,
        "task_dir": task_dir,
        "provider": provider,
        "auditor": reviewer,
        "output_root": tmp_path,
        "template": recorded["template"],
        "review_mode": "none",
    }
    assert recorded["template"].template_id == "concept-explanation"


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


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        (
            {"LEARNNEST_NOTE_API_KEY": "test-compatible-secret"},
            "LEARNNEST_NOTE_API_KEY, LEARNNEST_NOTE_BASE_URL, and "
            "LEARNNEST_NOTE_MODEL must be set together",
        ),
        (
            {
                "LEARNNEST_NOTE_API_KEY": "test-compatible-secret",
                "LEARNNEST_NOTE_BASE_URL": "https://example.invalid/v1",
                "LEARNNEST_NOTE_MODEL": "test-compatible-model",
                "MIMO_API_KEY": "test-mimo-secret",
            },
            "generic note provider configuration cannot be combined with MIMO_API_KEY",
        ),
    ],
)
def test_note_command_rejects_incomplete_or_mixed_openai_compatible_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment: dict[str, str],
    expected: str,
) -> None:
    import learnnest.cli as cli

    _write_cli_task(tmp_path)
    for name in (
        "MIMO_API_KEY",
        "LEARNNEST_NOTE_API_KEY",
        "LEARNNEST_NOTE_BASE_URL",
        "LEARNNEST_NOTE_MODEL",
        "LEARNNEST_NOTE_PROVIDER",
        "LEARNNEST_NOTE_JSON_MODE",
        "LEARNNEST_NOTE_WRITER_STRATEGY",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        cli,
        "generate_and_activate_v4_note",
        lambda *args, **kwargs: pytest.fail("generation must not start"),
    )

    result = runner.invoke(
        app,
        ["note", "20260711-a1b2c3d4", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert expected in result.output
    for secret in environment.values():
        if "secret" in secret:
            assert secret not in result.output


def test_note_command_passes_a_secretstr_and_scrubs_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli
    from pydantic import SecretStr

    _write_cli_task(tmp_path)
    secret = "test-secret-not-real"
    recorded: dict[str, object] = {}

    provider = object()
    reviewer = object()

    def fake_provider(api_key: SecretStr) -> object:
        recorded["provider_api_key"] = api_key
        return provider

    def fake_reviewer(api_key: SecretStr, **kwargs: object) -> object:
        recorded["reviewer_api_key"] = api_key
        return reviewer

    def fail_generation(*args: object, **kwargs: object) -> object:
        recorded["template"] = kwargs.get("template")
        recorded["auditor"] = kwargs.get("auditor")
        raise RuntimeError(f"transport accidentally included {secret}")

    monkeypatch.setenv("MIMO_API_KEY", secret)
    monkeypatch.setattr(
        cli,
        "MimoNoteProvider",
        lambda api_key, **kwargs: fake_provider(api_key),
    )
    monkeypatch.setattr(cli, "MimoNoteReviewer", fake_reviewer)
    monkeypatch.setattr(cli, "generate_and_activate_v4_note", fail_generation)

    result = runner.invoke(
        app,
        ["note", "20260711-a1b2c3d4", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert isinstance(recorded["provider_api_key"], SecretStr)
    assert recorded["provider_api_key"].get_secret_value() == secret
    assert isinstance(recorded["reviewer_api_key"], SecretStr)
    assert recorded["reviewer_api_key"].get_secret_value() == secret
    assert recorded["auditor"] is reviewer
    assert recorded["template"].template_id == "concept-explanation"
    assert secret not in result.output


def test_note_command_passes_a_concrete_note_type_to_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    task_dir = _write_cli_task(tmp_path)
    recorded: dict[str, object] = {}
    provider = object()
    reviewer = object()
    monkeypatch.setenv("MIMO_API_KEY", "test-secret-not-real")
    monkeypatch.setattr(cli, "MimoNoteProvider", lambda secret, **kwargs: provider)
    monkeypatch.setattr(cli, "MimoNoteReviewer", lambda secret, **kwargs: reviewer)

    def fake_generate(
        actual_task_dir: Path,
        actual_provider: object,
        output_root: Path,
        *,
        template: object,
        review_mode: str,
        auditor: object,
    ) -> object:
        recorded.update(
            {
                "task_dir": actual_task_dir,
                "provider": actual_provider,
                "auditor": auditor,
                "output_root": output_root,
                "template": template,
                "review_mode": review_mode,
            }
        )
        return type(
            "Result",
            (),
            {
                "task": type("Task", (), {"task_id": "20260711-a1b2c3d4"})(),
                "activated": True,
                "bundle_path": tmp_path / "bundle",
                "review_status": "not_requested",
            },
        )()

    monkeypatch.setattr(cli, "generate_and_activate_v4_note", fake_generate)

    result = runner.invoke(
        app,
        [
            "note",
            "20260711-a1b2c3d4",
            "--note-type",
            "practical",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert recorded == {
        "task_dir": task_dir,
        "provider": provider,
        "auditor": reviewer,
        "output_root": tmp_path,
        "template": recorded["template"],
        "review_mode": "none",
    }
    assert recorded["template"].template_id == "practical-tutorial"


def test_note_provider_pair_applies_one_safe_budget_to_mimo_generation_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import learnnest.cli as cli

    captured: dict[str, int] = {}
    monkeypatch.setattr(
        cli,
        "MimoNoteProvider",
        lambda secret, *, safe_input_tokens: captured.setdefault(
            "provider", safe_input_tokens
        ),
    )
    monkeypatch.setattr(
        cli,
        "MimoNoteReviewer",
        lambda secret, *, safe_input_tokens: captured.setdefault(
            "auditor", safe_input_tokens
        ),
    )

    cli._note_provider_pair(
        {
            "MIMO_API_KEY": "test-secret-not-real",
            "LEARNNEST_NOTE_SAFE_INPUT_TOKENS": "1536",
        }
    )

    assert captured == {"provider": 1536, "auditor": 1536}


def test_note_safe_input_budget_defaults_to_sixty_four_thousand() -> None:
    import learnnest.cli as cli

    assert cli._note_safe_input_tokens({}) == 64_000


def test_external_note_command_does_not_read_or_construct_mimo_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    task_dir = _write_cli_task(tmp_path)
    external = tmp_path / "external.json"
    external.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    recorded: dict[str, object] = {}
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "MimoNoteProvider",
        lambda *args, **kwargs: pytest.fail("MiMo provider must not be constructed"),
    )
    monkeypatch.setattr(
        cli,
        "MimoNoteReviewer",
        lambda *args, **kwargs: pytest.fail("MiMo reviewer must not be constructed"),
    )

    def fake_activate(
        task_path: Path,
        raw_path: Path,
        output_root: Path,
        *,
        template: object,
    ) -> object:
        recorded.update(
            {
                "task_dir": task_path,
                "raw_path": raw_path,
                "output_root": output_root,
                "template": template,
            }
        )
        return type(
            "Result",
            (),
            {
                "task": type("Task", (), {"task_id": "20260711-a1b2c3d4"})(),
                "activated": True,
                "bundle_path": tmp_path / "bundle",
                "review_status": "not_requested",
            },
        )()

    monkeypatch.setattr(cli, "build_and_activate_external_v4_note", fake_activate)

    result = runner.invoke(
        app,
        [
            "note",
            "20260711-a1b2c3d4",
            "--external-note",
            str(external),
            "--note-type",
            "resource",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert recorded == {
        "task_dir": task_dir,
        "raw_path": external,
        "output_root": tmp_path,
        "template": recorded["template"],
    }
    assert recorded["template"].template_id == "resource-share"


def test_external_report_records_an_unavailable_audit_without_note_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    task_dir = _write_cli_task(tmp_path)
    external = tmp_path / "external.json"
    external.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    recorded: dict[str, object] = {}
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "MimoNoteProvider",
        lambda *args, **kwargs: pytest.fail("MiMo provider must not be constructed"),
    )
    monkeypatch.setattr(
        cli,
        "MimoNoteReviewer",
        lambda *args, **kwargs: pytest.fail("MiMo reviewer must not be constructed"),
    )

    def fake_activate(
        task_path: Path,
        raw_path: Path,
        output_root: Path,
        *,
        template: object,
        review_mode: str,
        auditor: object | None,
    ) -> object:
        recorded.update(
            {
                "task_dir": task_path,
                "raw_path": raw_path,
                "output_root": output_root,
                "template": template,
                "review_mode": review_mode,
                "auditor": auditor,
            }
        )
        return type(
            "Result",
            (),
            {
                "task": type("Task", (), {"task_id": "20260711-a1b2c3d4"})(),
                "activated": True,
                "bundle_path": tmp_path / "bundle",
                "review_status": "unavailable",
            },
        )()

    monkeypatch.setattr(cli, "build_and_activate_external_v4_note", fake_activate)

    result = runner.invoke(
        app,
        [
            "note",
            "20260711-a1b2c3d4",
            "--external-note",
            str(external),
            "--review-mode",
            "report",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert recorded["task_dir"] == task_dir
    assert recorded["review_mode"] == "report"
    assert recorded["auditor"] is None


def test_note_rerender_does_not_require_or_read_mimo_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    _write_cli_task(tmp_path)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "MimoNoteProvider",
        lambda *args, **kwargs: pytest.fail("MiMo provider must not be constructed"),
    )
    monkeypatch.setattr(
        cli,
        "MimoNoteReviewer",
        lambda *args, **kwargs: pytest.fail("MiMo reviewer must not be constructed"),
    )
    monkeypatch.setattr(
        cli,
        "rerender_and_activate_note",
        lambda task_dir, output_root: type(
            "Task", (), {"task_id": "20260711-a1b2c3d4"}
        )(),
        raising=False,
    )

    result = runner.invoke(
        app,
        [
            "note",
            "20260711-a1b2c3d4",
            "--rerender",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "20260711-a1b2c3d4" in result.output


def test_cli_rejects_rerender_with_any_note_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    _write_cli_task(tmp_path)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "rerender_and_activate_note",
        lambda *args, **kwargs: pytest.fail("rerender must not start"),
    )

    result = runner.invoke(
        app,
        [
            "note",
            "20260711-a1b2c3d4",
            "--rerender",
            "--note-type",
            "auto",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "--rerender and --note-type are mutually exclusive" in result.stderr


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


def test_tts_command_passes_secret_provider_and_output_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli
    from pydantic import SecretStr

    _write_cli_task(tmp_path)
    recorded: dict[str, object] = {}
    monkeypatch.setenv("MIMO_API_KEY", "test-secret-not-real")

    def fake_provider(secret):
        recorded["secret"] = secret
        return object()

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

    monkeypatch.setattr(cli, "MimoTtsProvider", fake_provider, raising=False)
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
    assert isinstance(recorded["secret"], SecretStr)
    assert recorded["output_root"] == tmp_path
    assert recorded["style"] == "平静清晰"


def test_tts_command_loads_mimo_key_from_the_local_runtime_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli
    from pydantic import SecretStr

    _write_cli_task(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "MIMO_API_KEY=from-local-runtime-file\n", encoding="utf-8"
    )
    recorded: dict[str, object] = {}

    def fake_provider(secret: SecretStr) -> object:
        recorded["secret"] = secret
        return object()

    monkeypatch.setattr(cli, "MimoTtsProvider", fake_provider, raising=False)
    monkeypatch.setattr(
        cli,
        "generate_and_activate_tts",
        lambda task_dir, provider, output_root, *, style_instruction: type(
            "Task", (), {"task_id": "20260711-a1b2c3d4"}
        )(),
        raising=False,
    )

    result = runner.invoke(
        app,
        ["tts", "20260711-a1b2c3d4", "--output-root", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert recorded["secret"] == SecretStr("from-local-runtime-file")
    assert "from-local-runtime-file" not in result.output


@pytest.mark.parametrize(
    ("command", "expected_resources"),
    [
        ("note", ["network", "llm"]),
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
    if command == "note":
        monkeypatch.setattr(cli, "MimoNoteProvider", lambda secret, **kwargs: object())
        monkeypatch.setattr(cli, "MimoNoteReviewer", lambda secret, **kwargs: object())
        monkeypatch.setattr(
            cli,
            "generate_and_activate_v4_note",
            lambda task_dir, provider, output_root, *, template, review_mode, auditor: (
                type(
                    "Result",
                    (),
                    {
                        "task": task,
                        "activated": True,
                        "bundle_path": tmp_path / "bundle",
                        "review_status": "not_requested",
                    },
                )()
            ),
        )
    elif command == "podcast":
        monkeypatch.setattr(cli, "MimoPodcastProvider", lambda secret: object())
        monkeypatch.setattr(
            cli,
            "generate_and_activate_podcast",
            lambda task_dir, provider, output_root: task,
        )
    else:
        monkeypatch.setattr(cli, "MimoTtsProvider", lambda secret: object())
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


def test_validate_note_only_validates_the_external_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.cli as cli

    task_dir = _write_cli_task(tmp_path)
    external = tmp_path / "external.json"
    external.write_text("{}", encoding="utf-8")
    recorded: dict[str, object] = {}

    def fake_validate(
        task_path: Path,
        raw_path: Path,
        *,
        template: object,
    ) -> list[str]:
        recorded.update(
            {"task_dir": task_path, "raw_path": raw_path, "template": template}
        )
        return []

    monkeypatch.setattr(cli, "validate_external_v4_note", fake_validate)

    result = runner.invoke(
        app,
        [
            "validate-note",
            "20260711-a1b2c3d4",
            str(external),
            "--note-type",
            "auto",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert recorded == {
        "task_dir": task_dir,
        "raw_path": external,
        "template": recorded["template"],
    }
    assert recorded["template"].template_id == "concept-explanation"
    assert not (task_dir / "generated_notes").exists()


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
