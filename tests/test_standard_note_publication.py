from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from learnnest.models import ContentPack, StageStatus, TaskRecord
from learnnest.task_store import load_task, write_task_atomic


def _workspace(tmp_path: Path) -> tuple[Path, Path, TaskRecord]:
    output_root = tmp_path / "vault"
    task_dir = output_root / "视频学习素材" / "lesson--note-1234"
    task_dir.mkdir(parents=True)
    pack = ContentPack(
        task_id="task-note-1234",
        source_fingerprint="source-note-1234",
        evidence=[],
    )
    (task_dir / "content_pack.json").write_bytes(
        pack.model_dump_json(indent=2).encode("utf-8")
    )
    task = TaskRecord(
        task_id=pack.task_id,
        source_path="C:/videos/lesson.mp4",
        source_fingerprint=pack.source_fingerprint,
        title="标准笔记",
        profile="note",
        stages={"content_pack": StageStatus.COMPLETED},
        artifacts={"content_pack": ["content_pack.json"]},
    )
    write_task_atomic(task_dir, task)
    return output_root, task_dir, task


def _write_bundle(
    task_dir: Path,
    task: TaskRecord,
    *,
    body: str = "# 标准笔记\n\n正文。",
    bundle_name: str = "assisted-draft/reviewed",
) -> Path:
    from learnnest.standard_note_publication import write_standard_note_bundle

    bundle = task_dir / bundle_name
    write_standard_note_bundle(
        task_dir,
        bundle,
        task,
        body,
        route="assisted-note",
        status="model_reviewed",
    )
    return bundle


def test_standard_note_publishes_identity_bound_metadata_and_markdown(
    tmp_path: Path,
) -> None:
    from learnnest.standard_note_publication import publish_standard_note

    output_root, task_dir, task = _workspace(tmp_path)
    bundle = _write_bundle(task_dir, task)

    activated = publish_standard_note(
        task_dir,
        bundle,
        output_root,
        provider="fake-reviewer",
        model="fake-1",
    )

    metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    body_bytes = (bundle / "note.md").read_bytes()
    assert activated.stages["note"] is StageStatus.COMPLETED
    assert activated.stages["publish"] is StageStatus.COMPLETED
    assert activated.artifacts["note"] == [
        "assisted-draft/reviewed/metadata.json",
        "assisted-draft/reviewed/note.md",
    ]
    assert activated.artifacts["publish"] == ["assisted-draft/reviewed/note.md"]
    assert metadata == {
        "body_path": "note.md",
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "content_pack_sha256": hashlib.sha256(
            (task_dir / "content_pack.json").read_bytes()
        ).hexdigest(),
        "route": "assisted-note",
        "schema_version": "1.0",
        "source_fingerprint": task.source_fingerprint,
        "status": "model_reviewed",
        "task_id": task.task_id,
    }
    published = next((output_root / "视频学习笔记").glob("*.md"))
    assert published.read_bytes() == body_bytes
    assert load_task(task_dir) == activated


@pytest.mark.parametrize(
    "ambiguity",
    ["duplicate_metadata", "duplicate_note", "cross_bundle"],
)
def test_load_active_standard_note_rejects_ambiguous_task_record(
    tmp_path: Path, ambiguity: str
) -> None:
    from learnnest.standard_note_publication import load_active_standard_note

    _output_root, task_dir, task = _workspace(tmp_path)
    first = _write_bundle(
        task_dir,
        task,
        body="# 第一套\n",
        bundle_name="assisted-draft/plan-a/reviewed",
    )
    second = _write_bundle(
        task_dir,
        task,
        body="# 第二套\n",
        bundle_name="assisted-draft/plan-b/reviewed",
    )
    first_metadata = "assisted-draft/plan-a/reviewed/metadata.json"
    first_body = "assisted-draft/plan-a/reviewed/note.md"
    second_metadata = "assisted-draft/plan-b/reviewed/metadata.json"
    second_body = "assisted-draft/plan-b/reviewed/note.md"
    assert first.is_dir() and second.is_dir()
    note_artifacts = {
        "duplicate_metadata": [first_metadata, second_metadata, first_body],
        "duplicate_note": [first_metadata, first_body, second_body],
        "cross_bundle": [first_metadata, second_body],
    }[ambiguity]
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={"artifacts": {**task.artifacts, "note": note_artifacts}}
        ),
    )

    with pytest.raises(ValueError, match="ambiguous"):
        load_active_standard_note(task_dir)


def test_same_task_can_replace_its_published_standard_note_without_body_marker(
    tmp_path: Path,
) -> None:
    from learnnest.standard_note_publication import (
        publish_standard_note,
        standard_published_note_path,
    )

    output_root, task_dir, task = _workspace(tmp_path)
    first_bundle = _write_bundle(
        task_dir,
        task,
        body="# 第一版\n\n旧正文。",
        bundle_name="assisted-draft/plan-a/reviewed",
    )
    publish_standard_note(
        task_dir,
        first_bundle,
        output_root,
        provider="fake-reviewer",
        model="fake-1",
    )

    second_bundle = _write_bundle(
        task_dir,
        task,
        body="# 第二版\n\n新正文。",
        bundle_name="assisted-draft/plan-b/reviewed",
    )
    activated = publish_standard_note(
        task_dir,
        second_bundle,
        output_root,
        provider="fake-reviewer",
        model="fake-1",
    )

    published = standard_published_note_path(task, output_root)
    marker = published.with_suffix(".learnnest.json")
    body_bytes = (second_bundle / "note.md").read_bytes()
    ownership = json.loads(marker.read_text(encoding="utf-8"))
    assert published.read_bytes() == body_bytes
    assert ownership == {
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "schema_version": "1.0",
        "source_fingerprint": task.source_fingerprint,
        "status": "completed",
        "task_id": task.task_id,
    }
    assert activated.artifacts["note"] == [
        "assisted-draft/plan-b/reviewed/metadata.json",
        "assisted-draft/plan-b/reviewed/note.md",
    ]
    assert activated.artifacts["publish"] == [
        "assisted-draft/plan-b/reviewed/note.md",
    ]


def test_standard_note_first_task_write_failure_leaves_destination_and_marker_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.standard_note_publication as publication

    output_root, task_dir, task = _workspace(tmp_path)
    bundle = _write_bundle(task_dir, task)
    destination = publication.standard_published_note_path(task, output_root)
    marker = publication.standard_note_ownership_marker_path(destination)
    original_task = (task_dir / "task.json").read_bytes()

    def fail_first_task_write(*_args: object, **_kwargs: object) -> Path:
        raise OSError("initial RUNNING task replace failed")

    monkeypatch.setattr(publication, "write_task_atomic", fail_first_task_write)
    with pytest.raises(OSError, match="initial RUNNING task replace failed"):
        publication.publish_standard_note(
            task_dir,
            bundle,
            output_root,
            provider="fake-reviewer",
            model="fake-1",
        )

    assert (task_dir / "task.json").read_bytes() == original_task
    assert not destination.exists()
    assert not marker.exists()


def test_standard_note_recovery_uses_task_record_bundle_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    import learnnest.standard_note_publication as publication

    output_root, task_dir, task = _workspace(tmp_path)
    body = "# 同一正文\n\n可恢复。"
    first_bundle = _write_bundle(
        task_dir,
        task,
        body=body,
        bundle_name="assisted-draft/plan-a/reviewed",
    )
    second_bundle = _write_bundle(
        task_dir,
        task,
        body=body,
        bundle_name="assisted-draft/plan-b/reviewed",
    )
    assert first_bundle != second_bundle
    running = task.model_copy(
        update={
            "stages": {
                **task.stages,
                "note": StageStatus.COMPLETED,
                "publish": StageStatus.RUNNING,
            },
            "artifacts": {
                **task.artifacts,
                "note": [
                    "assisted-draft/plan-b/reviewed/metadata.json",
                    "assisted-draft/plan-b/reviewed/note.md",
                ],
            },
            "providers": {"note": "writer-reviewer-b"},
            "models": {"note": "model-b"},
        }
    )
    write_task_atomic(task_dir, running)
    destination = publication.standard_published_note_path(running, output_root)
    publication._write_standard_note_marker(
        destination,
        running,
        (second_bundle / "note.md").read_bytes(),
        "pending",
    )

    recovered = publication.reconcile_standard_note_publication(task_dir, output_root)

    assert recovered is not None
    assert recovered.artifacts["note"] == [
        "assisted-draft/plan-b/reviewed/metadata.json",
        "assisted-draft/plan-b/reviewed/note.md",
    ]
    assert recovered.providers["note"] == "writer-reviewer-b"
    assert recovered.models["note"] == "model-b"
    assert destination.read_bytes() == (second_bundle / "note.md").read_bytes()


@pytest.mark.parametrize("field", ["task_id", "source_fingerprint"])
def test_standard_note_rejects_foreign_ownership_marker_before_task_write(
    tmp_path: Path, field: str
) -> None:
    from learnnest.standard_note_publication import (
        publish_standard_note,
        standard_note_ownership_marker_path,
        standard_published_note_path,
    )

    output_root, task_dir, task = _workspace(tmp_path)
    destination = standard_published_note_path(task, output_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"foreign body\n")
    ownership = {
        "schema_version": "1.0",
        "task_id": task.task_id,
        "source_fingerprint": task.source_fingerprint,
        "body_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "status": "completed",
    }
    ownership[field] = "foreign-owner"
    marker = standard_note_ownership_marker_path(destination)
    marker.write_text(json.dumps(ownership), encoding="utf-8")
    original_task = (task_dir / "task.json").read_bytes()
    bundle = _write_bundle(task_dir, task)

    with pytest.raises(RuntimeError, match="another task"):
        publish_standard_note(
            task_dir,
            bundle,
            output_root,
            provider="fake-reviewer",
            model="fake-1",
        )

    assert (task_dir / "task.json").read_bytes() == original_task
    assert destination.read_bytes() == b"foreign body\n"


def test_standard_note_rejects_damaged_ownership_marker_before_task_write(
    tmp_path: Path,
) -> None:
    from learnnest.standard_note_publication import (
        publish_standard_note,
        standard_note_ownership_marker_path,
        standard_published_note_path,
    )

    output_root, task_dir, task = _workspace(tmp_path)
    destination = standard_published_note_path(task, output_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"unknown body\n")
    standard_note_ownership_marker_path(destination).write_text(
        "{not-json", encoding="utf-8"
    )
    original_task = (task_dir / "task.json").read_bytes()
    bundle = _write_bundle(task_dir, task)

    with pytest.raises(ValueError, match="ownership marker"):
        publish_standard_note(
            task_dir,
            bundle,
            output_root,
            provider="fake-reviewer",
            model="fake-1",
        )

    assert (task_dir / "task.json").read_bytes() == original_task


def test_standard_note_rejects_unmarked_unknown_destination(
    tmp_path: Path,
) -> None:
    from learnnest.standard_note_publication import (
        publish_standard_note,
        standard_published_note_path,
    )

    output_root, task_dir, task = _workspace(tmp_path)
    destination = standard_published_note_path(task, output_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"not the active note\n")
    bundle = _write_bundle(task_dir, task)

    with pytest.raises(RuntimeError, match="ownership is unknown"):
        publish_standard_note(
            task_dir,
            bundle,
            output_root,
            provider="fake-reviewer",
            model="fake-1",
        )


def test_validate_task_accepts_published_standard_note(tmp_path: Path) -> None:
    from learnnest.standard_note_publication import publish_standard_note
    from learnnest.validation import validate_task

    output_root, task_dir, task = _workspace(tmp_path)
    bundle = _write_bundle(task_dir, task)
    publish_standard_note(
        task_dir,
        bundle,
        output_root,
        provider="fake-reviewer",
        model="fake-1",
    )

    assert validate_task(task_dir) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "wrong-task"),
        ("source_fingerprint", "wrong-source"),
        ("content_pack_sha256", "0" * 64),
        ("body_sha256", "0" * 64),
    ],
)
def test_standard_note_rejects_identity_or_sha_tampering(
    tmp_path: Path, field: str, value: str
) -> None:
    from learnnest.standard_note_publication import validate_standard_note_bundle

    _output_root, task_dir, task = _workspace(tmp_path)
    bundle = _write_bundle(task_dir, task)
    metadata_path = bundle / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata[field] = value
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        validate_standard_note_bundle(task_dir, bundle)


def test_standard_note_rejects_body_path_escape_and_body_tampering(
    tmp_path: Path,
) -> None:
    from learnnest.standard_note_publication import validate_standard_note_bundle

    _output_root, task_dir, task = _workspace(tmp_path)
    bundle = _write_bundle(task_dir, task)
    metadata_path = bundle / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["body_path"] = "../outside.md"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="body path"):
        validate_standard_note_bundle(task_dir, bundle)

    _write_bundle(task_dir, task)
    (bundle / "note.md").write_text("被篡改。\n", encoding="utf-8")
    with pytest.raises(ValueError, match="body SHA"):
        validate_standard_note_bundle(task_dir, bundle)


def test_standard_note_recovery_finishes_after_final_task_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.standard_note_publication as publication

    output_root, task_dir, task = _workspace(tmp_path)
    bundle = _write_bundle(task_dir, task)
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
        publication.publish_standard_note(
            task_dir,
            bundle,
            output_root,
            provider="fake-reviewer",
            model="fake-1",
        )

    running = load_task(task_dir)
    assert running.stages["publish"] is StageStatus.RUNNING
    marker = publication.standard_note_ownership_marker_path(
        publication.standard_published_note_path(task, output_root)
    )
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "completed"
    monkeypatch.setattr(publication, "write_task_atomic", real_write)
    recovered = publication.reconcile_standard_note_publication(task_dir, output_root)
    assert recovered is not None
    assert recovered.stages["publish"] is StageStatus.COMPLETED
    assert recovered.providers["note"] == "fake-reviewer"
    assert recovered.models["note"] == "fake-1"
    assert (
        publication.reconcile_standard_note_publication(task_dir, output_root) is None
    )


def test_standard_note_recovers_after_completed_body_before_final_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.standard_note_publication as publication

    output_root, task_dir, task = _workspace(tmp_path)
    bundle = _write_bundle(task_dir, task)
    destination = publication.standard_published_note_path(task, output_root)
    marker = publication.standard_note_ownership_marker_path(destination)
    real_replace = publication.atomic_replace_bytes

    def fail_completed_marker(path: Path, content: bytes) -> None:
        if (
            path == marker
            and json.loads(content.decode("utf-8"))["status"] == "completed"
        ):
            raise OSError("completed marker write failed")
        real_replace(path, content)

    monkeypatch.setattr(publication, "atomic_replace_bytes", fail_completed_marker)
    with pytest.raises(OSError, match="completed marker write failed"):
        publication.publish_standard_note(
            task_dir,
            bundle,
            output_root,
            provider="fake-reviewer",
            model="fake-1",
        )

    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "pending"
    assert destination.read_bytes() == (bundle / "note.md").read_bytes()
    monkeypatch.setattr(publication, "atomic_replace_bytes", real_replace)
    recovered = publication.reconcile_standard_note_publication(task_dir, output_root)
    assert recovered is not None
    assert recovered.stages["publish"] is StageStatus.COMPLETED
    assert recovered.providers["note"] == "fake-reviewer"
    assert recovered.models["note"] == "fake-1"
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "completed"
