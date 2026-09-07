"""Route-neutral publication contract for Markdown learning notes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from learnnest.automation_store import find_intake
from learnnest.models import ContentPack, StageStatus, TaskRecord
from learnnest.publication import (
    atomic_replace_bytes,
)
from learnnest.task_store import (
    complete_task_goal,
    load_task,
    write_task_atomic,
)

_SHA256 = r"^[0-9a-f]{64}$"


class StandardNoteMetadata(BaseModel):
    """Program-owned identity and upstream bindings for one Markdown body."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=1)
    route: str = Field(min_length=1)
    status: str = Field(min_length=1)
    body_path: str = Field(min_length=1)
    body_sha256: str = Field(pattern=_SHA256)
    content_pack_sha256: str = Field(pattern=_SHA256)
    plan_id: str | None = None
    dossier_sha256: str | None = None
    writer: dict[str, object] | None = None
    reviewer: dict[str, object] | None = None
    notice: str | None = None


class StandardNoteOwnershipMarker(BaseModel):
    """Program-owned sidecar for one published Markdown destination."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=1)
    body_sha256: str = Field(pattern=_SHA256)
    status: Literal["pending", "completed"]


@dataclass(frozen=True)
class StandardNoteBundle:
    """A validated standard note and its program-owned metadata."""

    task_dir: Path
    bundle_dir: Path
    metadata_path: Path
    body_path: Path
    metadata: StandardNoteMetadata
    body_bytes: bytes


def write_standard_note_bundle(
    task_dir: Path,
    bundle_dir: Path,
    task: TaskRecord,
    markdown: str,
    *,
    route: str,
    status: str,
    metadata_extras: Mapping[str, object] | None = None,
) -> StandardNoteBundle:
    """Atomically write the program-owned metadata and Markdown body."""
    root = task_dir.resolve()
    bundle = bundle_dir.resolve()
    if not bundle.is_relative_to(root):
        raise ValueError("standard note bundle must be inside the task directory")
    if not isinstance(markdown, str) or not markdown.strip() or "\x00" in markdown:
        raise ValueError("standard note Markdown is empty or unsafe")
    content_pack_bytes = _read_content_pack_bytes(root, task)
    body_bytes = markdown.encode("utf-8")
    metadata_payload: dict[str, object] = {
        "task_id": task.task_id,
        "source_fingerprint": task.source_fingerprint,
        "route": route,
        "status": status,
        "body_path": "note.md",
        "body_sha256": _sha256(body_bytes),
        "content_pack_sha256": _sha256(content_pack_bytes),
    }
    if metadata_extras:
        metadata_payload.update(metadata_extras)
    metadata = StandardNoteMetadata.model_validate(metadata_payload)
    atomic_replace_bytes(bundle / "note.md", body_bytes)
    atomic_replace_bytes(
        bundle / "metadata.json",
        _json_bytes(metadata),
    )
    return validate_standard_note_bundle(root, bundle)


def validate_standard_note_bundle(
    task_dir: Path,
    bundle_dir: Path,
    *,
    expected_route: str | None = None,
    expected_status: str | None = None,
) -> StandardNoteBundle:
    """Fail closed when note identity, upstream bytes, path, or body bytes differ."""
    root = task_dir.resolve()
    bundle = bundle_dir.resolve()
    if not bundle.is_relative_to(root):
        raise ValueError("standard note bundle must be inside the task directory")
    task = load_task(root)
    metadata_path = bundle / "metadata.json"
    try:
        metadata = StandardNoteMetadata.model_validate_json(metadata_path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("standard note metadata is missing or invalid") from error
    body_path = _resolve_relative(bundle, metadata.body_path, "body path")
    if body_path.name != "note.md":
        raise ValueError("standard note body path must name note.md")
    try:
        body_bytes = body_path.read_bytes()
    except OSError as error:
        raise ValueError("standard note body is missing") from error
    if metadata.task_id != task.task_id:
        raise ValueError("standard note task_id does not match task.json")
    if metadata.source_fingerprint != task.source_fingerprint:
        raise ValueError("standard note source_fingerprint does not match task.json")
    if expected_route is not None and metadata.route != expected_route:
        raise ValueError("standard note route does not match the requested route")
    if expected_status is not None and metadata.status != expected_status:
        raise ValueError("standard note status does not match the requested status")
    if metadata.body_sha256 != _sha256(body_bytes):
        raise ValueError("standard note body SHA does not match metadata")
    content_pack_bytes = _read_content_pack_bytes(root, task)
    if metadata.content_pack_sha256 != _sha256(content_pack_bytes):
        raise ValueError("standard note content pack SHA does not match metadata")
    content_pack = ContentPack.model_validate_json(content_pack_bytes)
    if (
        content_pack.task_id != task.task_id
        or content_pack.source_fingerprint != task.source_fingerprint
    ):
        raise ValueError("content pack does not match task identity")
    return StandardNoteBundle(
        task_dir=root,
        bundle_dir=bundle,
        metadata_path=metadata_path,
        body_path=body_path,
        metadata=metadata,
        body_bytes=body_bytes,
    )


def load_active_standard_note(task_dir: Path) -> StandardNoteBundle:
    """Load and validate the active note declared by a TaskRecord."""
    root = task_dir.resolve()
    task = load_task(root)
    try:
        return _load_task_record_standard_bundle(root, task)
    except RuntimeError as error:
        raise ValueError(
            "active standard note metadata/bundle is missing or ambiguous"
        ) from error


def _task_frozen_goal_is_audio(
    task_dir: Path, task: TaskRecord, output_root: Path
) -> bool:
    """Return whether the task's frozen output goal requires playable audio.

    A valid automation intake is authoritative; a task without any intake
    (manual notes and the CLI) freezes a note-only goal at creation. A corrupt
    or unverifiable intake is a failure, not a note-only fallback: completing
    an audio task at the note stage would freeze ``completed_at`` too early.
    """
    del task_dir
    try:
        intake = find_intake(output_root, task.task_id)
    except ValueError as error:
        raise ValueError(
            "task automation intake cannot be verified; its output goal is unknown"
        ) from error
    return intake is not None and intake.default_output == "complete_note_with_audio"


def _note_unless_audio_goal_stamp(
    task_dir: Path, task: TaskRecord, output_root: Path, updated: TaskRecord
) -> TaskRecord:
    """Stamp completion for note-only goals; audio goals freeze at TTS publish."""
    if _task_frozen_goal_is_audio(task_dir, task, output_root):
        return updated
    return complete_task_goal(updated)


def publish_standard_note(
    task_dir: Path,
    bundle_dir: Path,
    output_root: Path,
    *,
    provider: str,
    model: str,
    expected_route: str | None = None,
    expected_status: str | None = None,
) -> TaskRecord:
    """Activate one validated note and publish its Markdown copy atomically."""
    root = task_dir.resolve()
    task = load_task(root)
    bundle = validate_standard_note_bundle(
        root,
        bundle_dir,
        expected_route=expected_route,
        expected_status=expected_status,
    )
    relative = bundle.bundle_dir.relative_to(root)
    destination = standard_published_note_path(task, output_root)
    _check_standard_note_destination_ownership(root, task, destination)
    note_artifacts = [
        (relative / "metadata.json").as_posix(),
        (relative / "note.md").as_posix(),
    ]
    active_artifacts = {
        stage: paths
        for stage, paths in task.artifacts.items()
        if stage not in {"publish", "podcast_script", "tts", "note"}
    }
    active_artifacts["note"] = note_artifacts
    updated = task.model_copy(
        update={
            "stages": {
                **task.stages,
                "note": StageStatus.COMPLETED,
                "publish": StageStatus.RUNNING,
                "podcast_script": StageStatus.PENDING,
                "tts": StageStatus.PENDING,
            },
            "artifacts": active_artifacts,
            "providers": {
                **{
                    stage: value
                    for stage, value in task.providers.items()
                    if stage not in {"note", "podcast_script", "tts"}
                },
                "note": provider,
            },
            "models": {
                **{
                    stage: value
                    for stage, value in task.models.items()
                    if stage not in {"note", "podcast_script", "tts"}
                },
                "note": model,
            },
            "error_summary": None,
        }
    )
    # Persist provenance and the exact bundle before any Vault or marker side effect.
    write_task_atomic(root, updated)
    try:
        _publish_standard_note_destination(
            destination,
            updated,
            bundle.body_bytes,
        )
    except Exception as error:
        failed = updated.model_copy(
            update={
                "stages": {**updated.stages, "publish": StageStatus.FAILED},
                "error_summary": f"note publish failed: {type(error).__name__}",
            }
        )
        write_task_atomic(root, failed)
        raise
    completed = updated.model_copy(
        update={
            "stages": {**updated.stages, "publish": StageStatus.COMPLETED},
            "artifacts": {
                **updated.artifacts,
                "publish": [(relative / "note.md").as_posix()],
            },
        }
    )
    completed = _note_unless_audio_goal_stamp(root, updated, output_root, completed)
    write_task_atomic(root, completed)
    return completed


def reconcile_standard_note_publication(
    task_dir: Path, output_root: Path
) -> TaskRecord | None:
    """Finish a note publication after any sidecar or task write was interrupted."""
    root = task_dir.resolve()
    task = load_task(root)
    destination = standard_published_note_path(task, output_root)
    marker = read_standard_note_ownership_marker(destination)
    if marker is None:
        return None
    _validate_standard_note_marker_identity(marker, task)
    bundle = _load_task_record_standard_bundle(root, task)
    if bundle.metadata.body_sha256 != marker.body_sha256:
        raise RuntimeError(
            "standard note ownership marker does not match TaskRecord bundle"
        )
    if (
        task.stages.get("publish") is StageStatus.COMPLETED
        and marker.status == "completed"
    ):
        return None
    if marker.status == "completed":
        if (
            not destination.is_file()
            or _sha256(destination.read_bytes()) != marker.body_sha256
        ):
            raise RuntimeError("standard note ownership marker does not match body")
    else:
        if (
            not destination.is_file()
            or _sha256(destination.read_bytes()) != marker.body_sha256
        ):
            atomic_replace_bytes(destination, bundle.body_bytes)
        _write_standard_note_marker(destination, task, bundle.body_bytes, "completed")
    relative = bundle.bundle_dir.relative_to(root)
    completed = task.model_copy(
        update={
            "stages": {
                **task.stages,
                "note": StageStatus.COMPLETED,
                "publish": StageStatus.COMPLETED,
                "podcast_script": StageStatus.PENDING,
                "tts": StageStatus.PENDING,
            },
            "artifacts": {
                **{
                    stage: paths
                    for stage, paths in task.artifacts.items()
                    if stage not in {"note", "publish", "podcast_script", "tts"}
                },
                "note": [
                    (relative / "metadata.json").as_posix(),
                    (relative / "note.md").as_posix(),
                ],
                "publish": [(relative / "note.md").as_posix()],
            },
            "providers": {
                stage: value
                for stage, value in task.providers.items()
                if stage not in {"podcast_script", "tts"}
            },
            "models": {
                stage: value
                for stage, value in task.models.items()
                if stage not in {"podcast_script", "tts"}
            },
            "error_summary": None,
        }
    )
    completed = _note_unless_audio_goal_stamp(root, task, output_root, completed)
    write_task_atomic(root, completed)
    return completed


def _read_content_pack_bytes(task_dir: Path, task: TaskRecord) -> bytes:
    paths = [
        (task_dir / relative).resolve()
        for relative in task.artifacts.get("content_pack", [])
        if Path(relative).name == "content_pack.json"
    ]
    if (
        len(paths) != 1
        or not paths[0].is_relative_to(task_dir)
        or not paths[0].is_file()
    ):
        raise ValueError("task has no unique content_pack.json artifact")
    return paths[0].read_bytes()


def _resolve_relative(root: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or "\\" in value:
        raise ValueError(f"standard note {label} must be relative")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"standard note {label} escapes the bundle")
    return resolved


def standard_note_ownership_marker_path(destination: Path) -> Path:
    """Return the sidecar path without changing the Markdown body."""
    return destination.with_suffix(".learnnest.json")


def read_standard_note_ownership_marker(
    destination: Path,
) -> StandardNoteOwnershipMarker | None:
    """Read a standard-note sidecar, failing closed on malformed ownership."""
    marker_path = standard_note_ownership_marker_path(destination)
    if not marker_path.exists():
        return None
    try:
        return StandardNoteOwnershipMarker.model_validate_json(marker_path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(
            "standard note ownership marker is missing or invalid"
        ) from error


def _check_standard_note_destination_ownership(
    task_dir: Path,
    task: TaskRecord,
    destination: Path,
) -> None:
    marker = read_standard_note_ownership_marker(destination)
    if marker is not None:
        _validate_standard_note_marker_identity(marker, task)
        if marker.status == "completed" and destination.is_file():
            if _sha256(destination.read_bytes()) != marker.body_sha256:
                raise RuntimeError(
                    "standard note ownership marker does not match published body"
                )
        return
    if not destination.exists():
        return
    current = _try_load_active_standard_note(task_dir)
    if current is None or destination.read_bytes() != current.body_bytes:
        raise RuntimeError("standard note destination ownership is unknown")


def _publish_standard_note_destination(
    destination: Path,
    task: TaskRecord,
    body_bytes: bytes,
) -> None:
    _write_standard_note_marker(destination, task, body_bytes, "pending")
    if not destination.is_file() or _sha256(destination.read_bytes()) != _sha256(
        body_bytes
    ):
        atomic_replace_bytes(destination, body_bytes)
    _write_standard_note_marker(destination, task, body_bytes, "completed")


def _write_standard_note_marker(
    destination: Path,
    task: TaskRecord,
    body_bytes: bytes,
    status: Literal["pending", "completed"],
) -> None:
    marker = StandardNoteOwnershipMarker(
        task_id=task.task_id,
        source_fingerprint=task.source_fingerprint,
        body_sha256=_sha256(body_bytes),
        status=status,
    )
    atomic_replace_bytes(
        standard_note_ownership_marker_path(destination),
        _json_bytes(marker),
    )


def _validate_standard_note_marker_identity(
    marker: StandardNoteOwnershipMarker,
    task: TaskRecord,
) -> None:
    if (
        marker.task_id != task.task_id
        or marker.source_fingerprint != task.source_fingerprint
    ):
        raise RuntimeError("standard note destination belongs to another task")


def _try_load_active_standard_note(task_dir: Path) -> StandardNoteBundle | None:
    try:
        return load_active_standard_note(task_dir)
    except (OSError, UnicodeError, ValueError):
        return None


def _load_task_record_standard_bundle(
    task_dir: Path, task: TaskRecord
) -> StandardNoteBundle:
    root = task_dir.resolve()
    note_artifacts = task.artifacts.get("note", [])
    metadata_paths = [
        _resolve_task_artifact(root, relative, "standard note metadata")
        for relative in note_artifacts
        if Path(relative).name == "metadata.json"
    ]
    body_paths = [
        _resolve_task_artifact(root, relative, "standard note body")
        for relative in note_artifacts
        if Path(relative).name == "note.md"
    ]
    if len(metadata_paths) != 1 or len(body_paths) != 1:
        raise RuntimeError("TaskRecord has no unique standard note bundle")
    bundle = validate_standard_note_bundle(root, metadata_paths[0].parent)
    if bundle.body_path != body_paths[0]:
        raise RuntimeError("TaskRecord standard note body is not the metadata body")
    return bundle


def _resolve_task_artifact(root: Path, relative: str, label: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise RuntimeError(f"TaskRecord {label} path must be relative")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise RuntimeError(f"TaskRecord {label} path is missing or outside task")
    return resolved


def standard_published_note_path(task: TaskRecord, output_root: Path) -> Path:
    """Return the deterministic Vault path for one standard note."""
    from learnnest.util import safe_title

    return (
        output_root.resolve()
        / "视频学习笔记"
        / f"{safe_title(task.title)}--{task.task_id[-8:]}.md"
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(payload: object) -> bytes:
    if isinstance(payload, (StandardNoteMetadata, StandardNoteOwnershipMarker)):
        payload = payload.model_dump(mode="json", exclude_none=True)
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
