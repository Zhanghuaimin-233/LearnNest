"""Atomic publication helpers for activated note bundles."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from learnnest.models import StageStatus, TaskRecord
from learnnest.task_store import load_task, write_task_atomic
from learnnest.util import safe_title


def select_published_note_path(
    task_dir: Path, task: TaskRecord, output_root: Path
) -> Path:
    """Choose a stable Vault note path without overwriting another owner."""
    del task_dir
    notes_dir = output_root.resolve() / "视频学习笔记"
    base = notes_dir / f"{safe_title(task.title)}.md"
    hashed = notes_dir / f"{safe_title(task.title)}--{task.task_id[-8:]}.md"
    base_owned = base.exists() and note_belongs_to_task(base, task.task_id)
    hashed_owned = hashed.exists() and note_belongs_to_task(hashed, task.task_id)
    if base_owned and hashed_owned:
        raise RuntimeError(
            f"duplicate published notes belong to task {task.task_id}; "
            "manual cleanup is required"
        )
    if base_owned:
        return base
    if not base.exists() and hashed_owned:
        return hashed
    if not base.exists():
        return base
    if not hashed.exists() or hashed_owned:
        return hashed
    raise RuntimeError(f"published note path belongs to another task: {hashed.name}")


def atomic_replace_bytes(path: Path, content: bytes) -> None:
    """Replace one file atomically using a temporary in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def activate_note_bundle(
    task_dir: Path,
    bundle_dir: Path,
    task: TaskRecord,
    *,
    output_root: Path,
    provider: str,
    model: str,
    preserve_derived: bool = False,
) -> TaskRecord:
    """Activate a note bundle, then atomically publish its Vault note."""
    root = task_dir.resolve()
    bundle = bundle_dir.resolve()
    if not bundle.is_relative_to(root):
        raise ValueError("note bundle must be inside the task directory")
    required = ("note.json", "note.md")
    for filename in required:
        if not (bundle / filename).is_file():
            raise ValueError(f"note bundle is missing: {filename}")

    destination = select_published_note_path(root, task, output_root)
    bundle_relative = bundle.relative_to(root)
    note_artifacts = [
        (bundle_relative / "note.json").as_posix(),
        (bundle_relative / "note.md").as_posix(),
    ]
    publish_artifact = (bundle_relative / "note.md").as_posix()
    invalidated_stages = {"publish"}
    if not preserve_derived:
        invalidated_stages.update({"podcast_script", "tts"})
    active_artifacts = {
        stage: paths
        for stage, paths in task.artifacts.items()
        if stage not in invalidated_stages
    }
    active_artifacts["note"] = note_artifacts
    stages = {
        **task.stages,
        "note": StageStatus.COMPLETED,
        "publish": StageStatus.RUNNING,
    }
    providers = {**task.providers, "note": provider}
    models = {**task.models, "note": model}
    if not preserve_derived:
        stages.update(
            {
                "podcast_script": StageStatus.PENDING,
                "tts": StageStatus.PENDING,
            }
        )
        providers = {
            stage: value
            for stage, value in providers.items()
            if stage not in {"podcast_script", "tts"}
        }
        models = {
            stage: value
            for stage, value in models.items()
            if stage not in {"podcast_script", "tts"}
        }
    active = task.model_copy(
        update={
            "stages": stages,
            "artifacts": active_artifacts,
            "providers": providers,
            "models": models,
            "error_summary": None,
        }
    )
    write_task_atomic(root, active)

    try:
        atomic_replace_bytes(destination, (bundle / "note.md").read_bytes())
    except Exception as error:
        failed = active.model_copy(
            update={
                "stages": {**active.stages, "publish": StageStatus.FAILED},
                "error_summary": f"note publish failed: {type(error).__name__}",
            }
        )
        write_task_atomic(root, failed)
        raise

    completed = active.model_copy(
        update={
            "stages": {**active.stages, "publish": StageStatus.COMPLETED},
            "artifacts": {**active.artifacts, "publish": [publish_artifact]},
        }
    )
    write_task_atomic(root, completed)
    _remove_superseded_root_note_copies(root, task)
    return completed


def reconcile_pending_note_publication(
    task_dir: Path, output_root: Path
) -> TaskRecord | None:
    """Finish a published v0.3 bundle after only the final task write failed."""
    root = task_dir.resolve()
    task = load_task(root)
    if task.stages.get("publish") is not StageStatus.RUNNING:
        return None

    note_json = next(
        (
            Path(path)
            for path in task.artifacts.get("note", [])
            if Path(path).name == "note.json"
        ),
        None,
    )
    if (
        note_json is None
        or not note_json.parts
        or note_json.parts[0] != "generated_notes"
    ):
        return None
    bundle = (root / note_json.parent).resolve()
    if not bundle.is_relative_to(root):
        raise RuntimeError("pending note bundle is outside the task directory")
    published_artifact = bundle / "note.md"
    if not published_artifact.is_file():
        raise RuntimeError("pending note bundle is missing: note.md")
    if not note_belongs_to_task(published_artifact, task.task_id):
        raise RuntimeError("pending note bundle has an invalid task marker")

    destination = select_published_note_path(root, task, output_root)
    desired = published_artifact.read_bytes()
    try:
        published = destination.read_bytes()
    except OSError as error:
        raise RuntimeError("pending published note could not be read") from error
    if published != desired or not note_belongs_to_task(destination, task.task_id):
        raise RuntimeError(
            "pending published note differs from the activated bundle; "
            "manual review is required"
        )

    publish_artifact = published_artifact.relative_to(root).as_posix()
    completed = task.model_copy(
        update={
            "stages": {**task.stages, "publish": StageStatus.COMPLETED},
            "artifacts": {**task.artifacts, "publish": [publish_artifact]},
            "error_summary": None,
        }
    )
    write_task_atomic(root, completed)
    _remove_superseded_root_note_copies(root, task)
    return completed


def _remove_superseded_root_note_copies(root: Path, task: TaskRecord) -> None:
    """Drop deterministic root copies after their generated bundle is active."""
    allowed_names = {"note.json", "note.md", "published_note.md"}
    declared = (*task.artifacts.get("note", ()), *task.artifacts.get("publish", ()))
    for artifact in declared:
        relative = Path(artifact)
        if relative.parent != Path(".") or relative.name not in allowed_names:
            continue
        try:
            (root / relative).unlink(missing_ok=True)
        except OSError:
            # The bundle is already active; cleanup must not compromise recovery.
            continue


def note_belongs_to_task(path: Path, task_id: str) -> bool:
    """Accept current and legacy renderer ownership comments without rewriting notes."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return (
        f"<!-- learnnest-task-id: {task_id} -->" in content
        or f"<!-- learnpipe-task-id: {task_id} -->" in content
        or f"- 任务：`{task_id}`" in content
    )


def read_audio_ownership_marker(destination: Path) -> dict[str, object] | None:
    """Read one canonical or legacy audio ownership marker, failing closed on conflict."""
    markers = (
        destination.with_suffix(".learnnest.json"),
        destination.with_suffix(".learnpipe.json"),
    )
    owners: list[dict[str, object]] = []
    for marker in markers:
        if not marker.exists():
            continue
        try:
            owner = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("audio destination ownership is unknown") from error
        if not isinstance(owner, dict):
            raise ValueError("audio destination ownership is unknown")
        owners.append(owner)
    if not owners:
        return None
    if len(owners) == 2 and owners[0] != owners[1]:
        raise ValueError("audio destination ownership markers conflict")
    return owners[0]
