from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import learnnest.learning_workspace as learning_workspace
import learnnest.web_app as web_app
from learnnest.execution import RecoveryPlan
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.automation_models import (
    AutomationIntake,
    AutomationPolicy,
    AutomationTaskState,
)
from learnnest.automation_store import (
    authorize,
    create_intake,
    load_intake,
    load_status,
    load_task_state,
    save_intake,
    save_policy,
    save_task_state,
)
from learnnest.locks import LockUnavailable
from learnnest.models import StageStatus, TaskRecord
from learnnest.provider_profiles import (
    connect,
    freeze_role_bindings,
    load_settings,
    set_role_binding,
    settings_sha256,
)
from learnnest.task_store import create_task, write_task_atomic


def _task(
    root: Path,
    name: str,
    *,
    stages: dict[str, StageStatus] | None = None,
    artifacts: dict[str, list[str]] | None = None,
    error_summary: str | None = None,
) -> tuple[Path, TaskRecord]:
    task_dir = root / "视频学习素材" / name
    task = create_task(
        task_id=f"20260728-{name[-8:]}",
        source_path=f"C:/private/{name}.mp4",
        source_fingerprint=name,
        title=f"课程 {name}",
        profile="note",
    ).model_copy(
        update={
            "stages": stages or {},
            "artifacts": artifacts or {},
            "error_summary": error_summary,
        }
    )
    write_task_atomic(task_dir, task)
    return task_dir, task


def _published_task(root: Path, name: str = "ready-item") -> tuple[Path, TaskRecord]:
    task_dir, task = _task(
        root,
        name,
        stages={"publish": StageStatus.COMPLETED},
        artifacts={"publish": ["note.md"]},
    )
    (task_dir / "note.md").write_text(
        f"<!-- learnnest-task-id: {task.task_id} -->\n\n# 可读笔记\n",
        encoding="utf-8",
    )
    return task_dir, task


def _configured_root(root: Path, *, authorized: bool) -> None:
    connect(root, name="note", preset="mimo", secret_value="fake-key")
    set_role_binding(root, role="note_writer", connection_name="note")
    set_role_binding(root, role="note_reviewer", connection_name="note")
    snapshot = AssistedConnectionSnapshot(
        connection_name="note",
        connection_id="note",
        provider="xiaomi-mimo",
        endpoint_identity="https://api.xiaomimimo.com/v1",
        model="mimo-v2.5",
        adapter_revision="1",
    )
    save_policy(
        root,
        AutomationPolicy(
            writer=snapshot, reviewer=snapshot, default_output="complete_note"
        ),
    )
    if authorized:
        authorize(root)


@pytest.mark.parametrize(
    (
        "case",
        "expected_state",
        "expected_message",
        "expected_action",
        "expected_action_kind",
    ),
    [
        (
            "materials",
            "materials_ready",
            "材料已准备，可以开始整理。",
            "开始整理",
            "start_automation",
        ),
        ("setup", "waiting_setup", "请先完成整理设置。", "完成设置", "open_settings"),
        (
            "authorization",
            "waiting_authorization",
            "请确认授权后开始整理。",
            "确认授权",
            "open_automation",
        ),
        ("queued", "queued", "等待整理。", None, None),
        ("organizing", "organizing", "正在整理内容。", None, None),
        (
            "partial",
            "partial_ready",
            "笔记已完成，音频仍在处理中。",
            "打开笔记",
            "open_note",
        ),
        (
            "attention",
            "needs_action",
            "上次整理没有真正开始。当前设置已就绪，可以重新开始整理。",
            "重新开始整理",
            "start_automation",
        ),
        ("ready", "ready", "可阅读。", "打开笔记", "open_note"),
    ],
)
def test_snapshot_projects_the_eight_public_learning_states(
    tmp_path: Path,
    case: str,
    expected_state: str,
    expected_message: str,
    expected_action: str | None,
    expected_action_kind: str | None,
) -> None:
    task_dir, task = _task(
        tmp_path,
        case,
        stages={"content_pack": StageStatus.COMPLETED},
        artifacts={"content_pack": ["content_pack.json"]},
    )
    if case in {
        "setup",
        "authorization",
        "queued",
        "organizing",
        "attention",
        "partial",
    }:
        create_intake(
            tmp_path,
            AutomationIntake(
                task_id=task.task_id,
                source_kind="local_video",
                default_output="complete_note_with_audio"
                if case == "partial"
                else "complete_note",
                created_at=datetime.now(UTC),
                status="claimed"
                if case == "organizing"
                else "needs_attention"
                if case == "attention"
                else "pending",
            ),
        )
    if case in {"authorization", "queued", "organizing", "attention", "partial"}:
        _configured_root(
            tmp_path,
            authorized=case in {"queued", "organizing", "attention", "partial"},
        )
    if case in {"partial", "ready"}:
        (task_dir / "note.md").write_text(
            f"<!-- learnnest-task-id: {task.task_id} -->\n\n# 笔记", encoding="utf-8"
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

    snapshot = learning_workspace.LearningWorkspace(tmp_path).snapshot()
    item = next(
        item
        for group in (snapshot.inbox, snapshot.processing, snapshot.library)
        for item in group
    )

    assert (item.state, item.message, item.action, item.action_kind) == (
        expected_state,
        expected_message,
        expected_action,
        expected_action_kind,
    )
    expected_output_goal = (
        "complete_note_with_audio" if case == "partial" else "complete_note"
    )
    assert item.output_goal == expected_output_goal
    payload = web_app._learning_item_payload(item)
    assert (
        payload["state"],
        payload["message"],
        payload["action"],
        payload["action_kind"],
    ) == (
        expected_state,
        expected_message,
        expected_action,
        expected_action_kind,
    )
    assert payload["output_goal"] == expected_output_goal


def test_snapshot_gives_an_explicit_reprocess_step_for_an_unrecoverable_old_task(
    tmp_path: Path,
) -> None:
    _task_dir, task = _task(
        tmp_path,
        "old-output",
        stages={
            "content_pack": StageStatus.COMPLETED,
            "note": StageStatus.COMPLETED,
            "publish": StageStatus.COMPLETED,
        },
        artifacts={"content_pack": ["content_pack.json"]},
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
    _configured_root(tmp_path, authorized=True)

    item = learning_workspace.LearningWorkspace(tmp_path).snapshot().processing[0]

    assert item.state == "needs_action"
    assert item.message == (
        "这项旧任务缺少可打开的标准笔记，不能在原任务上安全继续。请重新选择原视频处理。"
    )
    assert item.action == "重新选择原视频"
    assert item.action_kind == "open_single_video"


def test_snapshot_explains_and_can_restart_a_legacy_pre_provider_stop(
    tmp_path: Path,
) -> None:
    task_dir, task = _task(
        tmp_path,
        "legacy-authorization",
        stages={"content_pack": StageStatus.COMPLETED},
        artifacts={"content_pack": ["content_pack.json"]},
    )
    _configured_root(tmp_path, authorized=True)
    current_sha = settings_sha256(load_settings(tmp_path))
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "provider_bindings": freeze_role_bindings(tmp_path),
                "provider_settings_sha256": current_sha,
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
    status = load_status(tmp_path)
    assert status is not None
    assert status.policy.writer.settings_sha256 is None
    save_task_state(
        tmp_path,
        AutomationTaskState(
            task_id=task.task_id,
            policy_sha256=status.policy_sha256,
            default_output="complete_note",
            status="needs_attention",
            blocked_reason="non_retryable_failure",
        ),
    )

    item = learning_workspace.LearningWorkspace(tmp_path).snapshot().processing[0]
    payload = web_app._learning_item_payload(item)

    assert (
        item.message == "模型调用尚未开始，已完成的材料仍然保留。可以直接重新开始整理。"
    )
    assert item.failure_reason == (
        "生成笔记前的连接校验失败：任务使用的是旧授权记录，缺少当前版本要求的"
        "校验标记。系统没有发起模型调用。"
    )
    assert item.action == "重新开始整理"
    assert item.action_kind == "start_automation"
    assert payload["failure_reason"] == item.failure_reason

    web_app.WebService(tmp_path).start_automation(task.task_id)

    restarted = load_task_state(tmp_path, task.task_id, status.policy_sha256)
    assert load_intake(tmp_path, task.task_id).status == "pending"
    assert restarted is not None
    assert restarted.status == "pending"
    assert restarted.blocked_reason is None
    assert restarted.failure_summary is None


def test_snapshot_revision_observes_intake_changes_and_safely_projects_corruption(
    tmp_path: Path,
) -> None:
    _, task = _task(
        tmp_path,
        "revision-intake",
        stages={"content_pack": StageStatus.COMPLETED},
        artifacts={"content_pack": ["content_pack.json"]},
    )
    intake = create_intake(
        tmp_path,
        AutomationIntake(
            task_id=task.task_id,
            source_kind="local_video",
            default_output="complete_note",
            created_at=datetime.now(UTC),
        ),
    )
    workspace = learning_workspace.LearningWorkspace(tmp_path)
    first = workspace.snapshot()
    save_intake(tmp_path, intake.model_copy(update={"status": "claimed"}))

    claimed = workspace.snapshot(first.revision)
    assert claimed.unchanged is False
    assert claimed.revision != first.revision
    assert claimed.processing[0].state == "organizing"
    assert workspace.snapshot(claimed.revision).unchanged is True

    intake_path = (
        tmp_path / ".learnnest" / "automation" / "intake" / f"{task.task_id}.json"
    )
    intake_path.write_text('{"task_id":"wrong"}', encoding="utf-8")
    corrupt = workspace.snapshot(claimed.revision)

    assert corrupt.unchanged is False
    assert corrupt.processing[0].state == "needs_action"
    assert "wrong" not in corrupt.processing[0].message


def test_snapshot_projects_existing_tasks_and_isolates_corrupt_records(
    tmp_path: Path,
) -> None:
    _, ready = _published_task(tmp_path)
    _task(
        tmp_path,
        "materials",
        stages={"content_pack": StageStatus.COMPLETED},
        artifacts={"content_pack": ["content_pack.json", "trace.md"]},
    )
    _task(tmp_path, "broken-state", error_summary="private C:/secret/path")
    corrupt = tmp_path / "视频学习素材" / "corrupt" / "task.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{not json", encoding="utf-8")

    snapshot = learning_workspace.LearningWorkspace(tmp_path).snapshot()

    assert [item.item_ref for item in snapshot.library] == [ready.task_id]
    assert [item.state for item in snapshot.inbox] == ["materials_ready"]
    assert [item.state for item in snapshot.processing] == ["needs_action"]
    assert "C:/secret/path" not in snapshot.processing[0].message
    assert (
        learning_workspace.LearningWorkspace(tmp_path)
        .snapshot(snapshot.revision)
        .unchanged
    )


def test_snapshot_builds_revision_and_task_projection_from_one_read(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task = _task(
        tmp_path,
        "single-read",
        stages={"content_pack": StageStatus.COMPLETED},
        artifacts={"content_pack": ["content_pack.json"]},
    )
    import learnnest.learning_workspace as workspace_module

    reads = 0
    original = Path.read_bytes

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal reads
        if path == task_dir / "task.json":
            reads += 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    snapshot = workspace_module.LearningWorkspace(tmp_path).snapshot()

    assert reads == 1
    assert snapshot.inbox[0].item_ref == task.task_id


def test_snapshot_isolates_oserror_while_parsing_one_task(
    tmp_path: Path, monkeypatch: Any
) -> None:
    broken_dir, _ = _task(
        tmp_path,
        "parse-oserror",
        stages={"content_pack": StageStatus.COMPLETED},
        artifacts={"content_pack": ["content_pack.json"]},
    )
    _, healthy = _task(
        tmp_path,
        "healthy-task",
        stages={"content_pack": StageStatus.COMPLETED},
        artifacts={"content_pack": ["content_pack.json"]},
    )
    original = learning_workspace.parse_task_bytes

    def fail_one_parse(data: bytes, *, base_dir: Path) -> TaskRecord:
        if base_dir == broken_dir:
            raise OSError(f"cannot normalize {base_dir.name}")
        return original(data, base_dir=base_dir)

    monkeypatch.setattr(learning_workspace, "parse_task_bytes", fail_one_parse)

    snapshot = learning_workspace.LearningWorkspace(tmp_path).snapshot()

    assert snapshot.library == ()
    assert [item.item_ref for item in snapshot.inbox] == [healthy.task_id]
    assert snapshot.processing == ()


@pytest.mark.parametrize(
    ("desired_output", "expected_profile"),
    [("readable_note", "note"), ("materials_only", "evidence")],
)
def test_add_content_maps_human_output_to_existing_profile(
    tmp_path: Path,
    monkeypatch: Any,
    desired_output: str,
    expected_profile: str,
) -> None:
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video")
    _, task = _task(tmp_path, f"added-{expected_profile}")
    observed: dict[str, Any] = {}

    def fake_process(video: Path, root: Path, profile: str) -> TaskRecord:
        observed.update(video=video, root=root, profile=profile)
        return task

    monkeypatch.setattr(learning_workspace, "process_video", fake_process)

    item = learning_workspace.LearningWorkspace(tmp_path).add_content(
        str(source),
        desired_output,  # type: ignore[arg-type]
    )

    assert item.item_ref == task.task_id
    assert observed == {
        "video": source.resolve(),
        "root": tmp_path.resolve(),
        "profile": expected_profile,
    }


@pytest.mark.parametrize(
    "source",
    [
        "http://127.0.0.1:8000/private",
        "http://localhost/internal",
        "http://service.local/lesson",
        "http://192.168.1.10/lesson",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/private",
        "http://127.1/private",
    ],
)
def test_add_content_rejects_explicitly_non_public_urls(
    tmp_path: Path, monkeypatch: Any, source: str
) -> None:
    called = False

    def fail_process(*_: Any, **__: Any) -> TaskRecord:
        nonlocal called
        called = True
        raise AssertionError("private URL reached the pipeline")

    monkeypatch.setattr(learning_workspace, "process_source", fail_process)

    with pytest.raises(learning_workspace.LearningWorkspaceError, match="公开链接"):
        learning_workspace.LearningWorkspace(tmp_path).add_content(source)

    assert called is False


@pytest.mark.parametrize(
    "source",
    [
        "https://example.com/watch?v=1",
        "https://8.8.8.8/video",
        "https://[2001:4860:4860::8888]/video",
    ],
)
def test_public_web_url_validation_accepts_public_hosts(source: str) -> None:
    learning_workspace.validate_public_web_url(source)


def test_continue_item_runs_only_deterministic_recovery(
    tmp_path: Path, monkeypatch: Any
) -> None:
    task_dir, task = _task(tmp_path, "continue-item")
    observed: dict[str, Any] = {}

    monkeypatch.setattr(
        learning_workspace,
        "plan_recovery",
        lambda _: RecoveryPlan(
            task_id=task.task_id,
            from_stage="source",
            reasons=["missing source"],
            requires_paid=False,
        ),
    )

    def fake_rerun(*args: Any, **kwargs: Any) -> TaskRecord:
        observed.update(args=args, kwargs=kwargs)
        return task

    monkeypatch.setattr(learning_workspace, "rerun_task", fake_rerun)

    result = learning_workspace.LearningWorkspace(tmp_path).continue_item(task.task_id)

    assert task_dir.is_dir()
    assert result.outcome == "continued"
    assert observed["args"][1] == "source"
    assert observed["kwargs"]["_task_lock_held"] is True


def test_continue_item_rejects_paid_recovery_without_calling_pipeline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _, task = _task(tmp_path, "paid-item")
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

    result = learning_workspace.LearningWorkspace(tmp_path).continue_item(task.task_id)

    assert result.outcome == "needs_setup"
    assert result.item.message == "需要完成设置后才能继续。"
    assert called is False


def test_continue_item_reports_lock_without_internal_details(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _, task = _task(tmp_path, "locked-item")

    @contextmanager
    def unavailable(*_: Any, **__: Any):
        raise LockUnavailable("C:/private/lock")
        yield

    monkeypatch.setattr(learning_workspace, "task_lock", unavailable)

    with pytest.raises(learning_workspace.LearningWorkspaceError) as error:
        learning_workspace.LearningWorkspace(tmp_path).continue_item(task.task_id)

    assert "C:/private" not in str(error.value)
    assert "正在处理中" in str(error.value)


def test_note_requires_completed_publish_and_declared_task_internal_markdown(
    tmp_path: Path,
) -> None:
    task_dir, task = _published_task(tmp_path)
    workspace = learning_workspace.LearningWorkspace(tmp_path)

    note = workspace.note(task.task_id)

    assert note.note_path == task_dir / "note.md"
    assert "# 可读笔记" in note.markdown
    escaped = _task(
        tmp_path,
        "escaped-note",
        stages={"publish": StageStatus.COMPLETED},
        artifacts={"publish": ["../../outside.md"]},
    )[1]
    with pytest.raises(learning_workspace.LearningWorkspaceError):
        workspace.note(escaped.task_id)
