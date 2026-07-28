from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import learnnest.learning_workspace as learning_workspace
from learnnest.execution import RecoveryPlan
from learnnest.locks import LockUnavailable
from learnnest.models import StageStatus, TaskRecord
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
