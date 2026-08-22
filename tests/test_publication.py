from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from learnnest.models import StageStatus, TaskRecord
from learnnest.task_store import load_task, write_task_atomic


def _task() -> TaskRecord:
    return TaskRecord(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
        stages={"note": "completed", "publish": "completed"},
        artifacts={
            "note": ["note.json", "note.md"],
            "publish": ["published_note.md"],
        },
    )


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path, TaskRecord]:
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    task = _task()
    write_task_atomic(task_dir, task)
    (task_dir / "note.json").write_text("{}", encoding="utf-8")
    (task_dir / "note.md").write_text("old note", encoding="utf-8")
    (task_dir / "published_note.md").write_text("old published", encoding="utf-8")
    bundle_dir = task_dir / "generated_notes" / "run-0001"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "note.json").write_text('{"schema_version":"2.0"}', encoding="utf-8")
    (bundle_dir / "note.md").write_text(
        "<!-- learnnest-task-id: 20260711-a1b2c3d4 -->\n# new note\n",
        encoding="utf-8",
    )
    (bundle_dir / "published_note.md").write_text(
        "<!-- learnnest-task-id: 20260711-a1b2c3d4 -->\n# new note\n",
        encoding="utf-8",
    )
    notes_dir = tmp_path / "视频学习笔记"
    notes_dir.mkdir()
    published = notes_dir / "lesson.md"
    published.write_text(
        "<!-- learnnest-task-id: 20260711-a1b2c3d4 -->\n# old note\n",
        encoding="utf-8",
    )
    return task_dir, bundle_dir, published, task


def test_activate_note_bundle_completes_note_and_publish(tmp_path: Path) -> None:
    from learnnest.publication import activate_note_bundle

    task_dir, bundle_dir, published, task = _workspace(tmp_path)

    activated = activate_note_bundle(
        task_dir,
        bundle_dir,
        task,
        output_root=tmp_path,
        provider="xiaomi-mimo",
        model="mimo-v2.5",
    )

    assert activated.stages["note"] is StageStatus.COMPLETED
    assert activated.stages["publish"] is StageStatus.COMPLETED
    assert activated.artifacts["note"] == [
        "generated_notes/run-0001/note.json",
        "generated_notes/run-0001/note.md",
    ]
    assert activated.artifacts["publish"] == ["generated_notes/run-0001/note.md"]
    assert activated.providers["note"] == "xiaomi-mimo"
    assert activated.models["note"] == "mimo-v2.5"
    assert "# new note" in published.read_text(encoding="utf-8")
    assert not (task_dir / "note.json").exists()
    assert not (task_dir / "note.md").exists()
    assert not (task_dir / "published_note.md").exists()
    assert load_task(task_dir) == activated


def test_activate_note_bundle_accepts_legacy_task_markers(tmp_path: Path) -> None:
    from learnnest.publication import activate_note_bundle

    task_dir, bundle_dir, published, task = _workspace(tmp_path)
    for path in (bundle_dir / "note.md", bundle_dir / "published_note.md", published):
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "learnnest-task-id", "learnpipe-task-id"
            ),
            encoding="utf-8",
        )

    activated = activate_note_bundle(
        task_dir,
        bundle_dir,
        task,
        output_root=tmp_path,
        provider="fake",
        model="legacy-marker",
    )

    assert activated.stages["publish"] is StageStatus.COMPLETED
    assert "# new note" in published.read_text(encoding="utf-8")


def test_note_ownership_accepts_verified_standard_bundle_metadata(
    tmp_path: Path,
) -> None:
    from learnnest.publication import note_belongs_to_task

    note = tmp_path / "note.md"
    body = "# 标准笔记\n"
    note.write_text(body, encoding="utf-8")
    (tmp_path / "metadata.json").write_text(
        json.dumps(
            {
                "task_id": "20260711-a1b2c3d4",
                "body_path": "note.md",
                "body_sha256": sha256(note.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    assert note_belongs_to_task(note, "20260711-a1b2c3d4") is True
    note.write_text("# 已被修改\n", encoding="utf-8")
    assert note_belongs_to_task(note, "20260711-a1b2c3d4") is False


def test_note_pointer_failure_keeps_old_task_and_published_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.publication as publication

    task_dir, bundle_dir, published, task = _workspace(tmp_path)
    old_task_bytes = (task_dir / "task.json").read_bytes()
    old_published_bytes = published.read_bytes()
    monkeypatch.setattr(
        publication,
        "write_task_atomic",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("task replace failed")),
    )

    with pytest.raises(OSError, match="task replace failed"):
        publication.activate_note_bundle(
            task_dir,
            bundle_dir,
            task,
            output_root=tmp_path,
            provider="fake",
            model="queued",
        )

    assert (task_dir / "task.json").read_bytes() == old_task_bytes
    assert published.read_bytes() == old_published_bytes


def test_publish_replace_failure_keeps_old_vault_note_and_marks_publish_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.publication as publication

    task_dir, bundle_dir, published, task = _workspace(tmp_path)
    old_published_bytes = published.read_bytes()
    monkeypatch.setattr(
        publication,
        "atomic_replace_bytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("vault replace failed")),
    )

    with pytest.raises(OSError, match="vault replace failed"):
        publication.activate_note_bundle(
            task_dir,
            bundle_dir,
            task,
            output_root=tmp_path,
            provider="fake",
            model="queued",
        )

    failed = load_task(task_dir)
    assert failed.stages["note"] is StageStatus.COMPLETED
    assert failed.stages["publish"] is StageStatus.FAILED
    assert "publish" not in failed.artifacts
    assert failed.artifacts["note"][0].startswith("generated_notes/run-0001/")
    assert published.read_bytes() == old_published_bytes


def test_final_task_write_failure_leaves_recoverable_running_publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.publication as publication

    task_dir, bundle_dir, published, task = _workspace(tmp_path)
    real_write = publication.write_task_atomic
    calls = 0

    def fail_second_write(task_path: Path, updated: TaskRecord) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("final task replace failed")
        return real_write(task_path, updated)

    monkeypatch.setattr(publication, "write_task_atomic", fail_second_write)

    with pytest.raises(OSError, match="final task replace failed"):
        publication.activate_note_bundle(
            task_dir,
            bundle_dir,
            task,
            output_root=tmp_path,
            provider="fake",
            model="queued",
        )

    running = load_task(task_dir)
    assert running.stages["note"] is StageStatus.COMPLETED
    assert running.stages["publish"] is StageStatus.RUNNING
    assert "publish" not in running.artifacts
    assert "# new note" in published.read_text(encoding="utf-8")

    monkeypatch.setattr(publication, "write_task_atomic", real_write)
    recovered = publication.reconcile_pending_note_publication(task_dir, tmp_path)

    assert recovered is not None
    assert recovered.stages["publish"] is StageStatus.COMPLETED
    assert recovered.artifacts["publish"] == ["generated_notes/run-0001/note.md"]
    assert load_task(task_dir) == recovered


def test_atomic_replace_failure_preserves_existing_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.publication as publication

    destination = tmp_path / "note.md"
    destination.write_bytes(b"old bytes")
    monkeypatch.setattr(
        publication.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        publication.atomic_replace_bytes(destination, b"new bytes")

    assert destination.read_bytes() == b"old bytes"
    assert list(tmp_path.glob("*.tmp")) == []
