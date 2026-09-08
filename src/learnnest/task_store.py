"""Persistent storage for the task state record."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from learnnest.execution_models import SourceIdentities
from learnnest.identities import identity_keys, normalize_local_source, stream_sha256
from learnnest.models import (
    ProviderBindingSnapshot,
    StageStatus,
    TaskProfile,
    TaskRecord,
)
from learnnest.note_types import ConcreteNoteType


_WINDOWS_REPLACE_RETRY_DELAYS = (0.02, 0.05, 0.1)
_IS_WINDOWS = os.name == "nt"


def _is_windows_replace_conflict(error: OSError) -> bool:
    if not _IS_WINDOWS:
        return False
    error_code = getattr(error, "winerror", None)
    if error_code is None:
        error_code = error.errno
    return error_code in {5, 32}


def _utc_stamp(value: datetime | None) -> datetime:
    """Return one trustworthy timezone-aware UTC instant from an injectable clock."""
    stamp = value if value is not None else datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("task lifecycle timestamps must be timezone-aware")
    return stamp.astimezone(UTC)


def create_task(
    *,
    task_id: str,
    source_path: str,
    source_input: str | None = None,
    source_type: str = "local_file",
    media_path: str | None = None,
    source_fingerprint: str,
    title: str,
    profile: TaskProfile = "evidence",
    note_type_override: ConcreteNoteType | None = None,
    provider_bindings: dict[str, ProviderBindingSnapshot] | None = None,
    now: datetime | None = None,
) -> TaskRecord:
    """Create a task record from stable source metadata.

    ``now`` is an injectable clock for deterministic tests; it defaults to the
    real UTC clock. The creation instant is frozen as ``created_at`` and must
    never be rewritten by later writes or re-runs.
    """
    frozen = _utc_stamp(now)
    return TaskRecord(
        task_id=task_id,
        source_path=source_path,
        source_input=source_input,
        source_type=source_type,
        media_path=media_path,
        source_fingerprint=source_fingerprint,
        title=title,
        profile=profile,
        note_type_override=note_type_override,
        provider_bindings={
            role: binding.model_dump(mode="python")
            for role, binding in (provider_bindings or {}).items()
        },
        created_at=frozen,
        updated_at=frozen,
    )


def complete_task_goal(task: TaskRecord, *, now: datetime | None = None) -> TaskRecord:
    """Stamp ``completed_at`` exactly once; later calls never overwrite it.

    The caller invokes this only after the frozen output goal's final artifact
    has been validated and atomically published, so the first stamp is the
    genuine completion instant.
    """
    if task.completed_at is not None:
        return task
    return task.model_copy(update={"completed_at": _utc_stamp(now)})


def _assert_lifecycle_facts_unchanged(
    previous: TaskRecord, incoming: TaskRecord
) -> None:
    """Enforce the frozen lifecycle time contract at the single write boundary.

    A task's first ``created_at``/``completed_at`` are frozen facts: any later
    write that rewrites them to a different instant is rejected. Omitted
    ``created_at`` on a later write reuses the already persisted creation
    instant (the task keeps its identity). Dropping or rewriting a frozen
    ``completed_at`` is rejected. ``updated_at`` is monotonic and must never
    move backwards against the already persisted fact. Tasks without persisted
    time facts (historical records, ruled out of scope) are left untouched.
    """
    if (
        previous.created_at is not None
        and incoming.created_at is not None
        and incoming.created_at != previous.created_at
    ):
        raise ValueError("created_at is frozen and must never change")
    if (
        previous.completed_at is not None
        and incoming.completed_at != previous.completed_at
    ):
        raise ValueError("completed_at is frozen and must never change")
    if (
        previous.updated_at is not None
        and incoming.updated_at is not None
        and incoming.updated_at < previous.updated_at
    ):
        raise ValueError("updated_at must never move backwards")


def write_task_atomic(
    task_dir: str | Path,
    task: TaskRecord,
    *,
    now: datetime | None = None,
) -> Path:
    """Write ``task.json`` through a temporary file and atomically replace it.

    Every successful write advances ``updated_at`` on both the persisted fact
    and the in-memory record. The frozen lifecycle facts are validated against
    the already persisted record before anything touches the disk. A brand-new
    task record without a ``created_at`` is frozen at the creation instant by
    the same injectable clock (``create_task`` already does this; this covers
    the first write of a bare record), so no persisted new task fact can lack
    a trustworthy creation time. A previous fact that is corrupt or
    momentarily unreadable fails the write instead of being overwritten.
    Failed or invalid writes never fabricate a success time.
    """
    stamp = _utc_stamp(now)
    stamped = task.model_copy(update={"updated_at": stamp})
    directory = Path(task_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "task.json"
    if destination.exists():
        validated = TaskRecord.model_validate(stamped.model_dump(mode="python"))
        previous = parse_task_bytes(destination.read_bytes(), base_dir=directory)
        _assert_lifecycle_facts_unchanged(previous, validated)
        if previous.created_at is not None and validated.created_at is None:
            # Reuse the already frozen creation instant when a later write omits it.
            stamped = stamped.model_copy(update={"created_at": previous.created_at})
            validated = TaskRecord.model_validate(stamped.model_dump(mode="python"))
    else:
        if stamped.created_at is None:
            stamped = stamped.model_copy(update={"created_at": stamp})
        validated = TaskRecord.model_validate(stamped.model_dump(mode="python"))
    serialized = json.dumps(
        validated.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True
    )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(f"{serialized}\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        for delay in (*_WINDOWS_REPLACE_RETRY_DELAYS, None):
            try:
                os.replace(temporary_path, destination)
                break
            except OSError as error:
                if delay is None or not _is_windows_replace_conflict(error):
                    raise
                time.sleep(delay)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    task.updated_at = validated.updated_at
    if validated.created_at is not None:
        # Keep the in-memory record aligned with the persisted fact, including
        # the first-write creation stamp for bare records (load == written).
        task.created_at = validated.created_at
    return destination


def load_task(path: str | Path) -> TaskRecord:
    """Load and validate a task record from a ``task.json`` path or directory."""
    task_path = Path(path)
    if task_path.is_dir():
        task_path /= "task.json"
    return parse_task_bytes(task_path.read_bytes(), base_dir=task_path.parent)


def parse_task_bytes(data: bytes, *, base_dir: Path) -> TaskRecord:
    """Parse one task fact snapshot already read from disk."""
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("task.json root must be an object")
    if payload.get("schema_version", "1.0") == "1.0":
        payload = _migrate_task_1_to_2(payload, base_dir=base_dir)
    return TaskRecord.model_validate(payload)


def _migrate_task_1_to_2(
    payload: dict[str, object], *, base_dir: Path
) -> dict[str, object]:
    migrated = dict(payload)
    source_path = migrated.get("source_path")
    source_input = migrated.get("source_input") or source_path
    migrated["schema_version"] = "2.0"
    normalized_source = source_input
    if migrated.get("source_type", "local_file") == "local_file" and isinstance(
        source_input, str
    ):
        source_path = Path(source_input).expanduser()
        if not source_path.is_absolute():
            source_path = base_dir / source_path
        normalized_source = normalize_local_source(source_path)
    migrated.setdefault("identities", {"normalized_source": normalized_source})
    migrated.setdefault("duplicate_of_task_id", None)
    migrated.setdefault("note_type_override", None)
    migrated.setdefault("active_attempt_id", None)
    migrated.setdefault("attempts", [])
    return migrated


def find_task_by_fingerprint(
    output_root: str | Path,
    source_fingerprint: str,
    *,
    require_completed_content_pack: bool = False,
) -> tuple[Path, TaskRecord] | None:
    """Find one persisted task by stable source identity without an index DB."""
    task_root = Path(output_root).resolve() / "视频学习素材"
    if not task_root.is_dir():
        return None
    for task_json in sorted(task_root.glob("*/task.json")):
        try:
            task = load_task(task_json)
        except (OSError, ValueError):
            continue
        if task.source_fingerprint != source_fingerprint:
            continue
        if require_completed_content_pack and (
            task.stages.get("content_pack") is not StageStatus.COMPLETED
        ):
            continue
        return task_json.parent, task
    return None


def find_task_by_identities(
    output_root: str | Path,
    identities: SourceIdentities,
    source_fingerprint: str,
    *,
    include_derived: bool = True,
    require_completed: bool = False,
    exclude_task_id: str | None = None,
) -> tuple[Path, TaskRecord] | None:
    """Find a task by the strongest available identity, then legacy fallback."""
    task_root = Path(output_root).resolve() / "视频学习素材"
    if not task_root.is_dir():
        return None
    candidates: list[tuple[Path, TaskRecord]] = []
    for task_json in sorted(task_root.glob("*/task.json")):
        try:
            task = load_task(task_json)
        except (OSError, ValueError):
            continue
        if task.task_id == exclude_task_id or task.identities is None:
            continue
        if require_completed and not _is_completed_task(task):
            continue
        candidates.append((task_json.parent, task))

    for kind, value in identity_keys(identities, source_fingerprint):
        if kind in {"content_sha256", "platform_id"} and not include_derived:
            continue
        for task_dir, task in candidates:
            candidate_values = dict(
                identity_keys(task.identities, task.source_fingerprint)
            )
            if kind == "content_sha256" and kind not in candidate_values:
                legacy_hash = _legacy_content_sha256(task_dir, task)
                if legacy_hash is not None:
                    candidate_values[kind] = legacy_hash
            if candidate_values.get(kind) == value:
                return task_dir, task
    return None


def _legacy_content_sha256(task_dir: Path, task: TaskRecord) -> str | None:
    if task.source_type != "local_file":
        return None
    candidate = Path(task.media_path or task.source_path).expanduser()
    if not candidate.is_absolute():
        candidate = task_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file():
        return None
    return stream_sha256(resolved)


def _is_completed_task(task: TaskRecord) -> bool:
    if task.active_attempt_id is not None:
        return False
    if task.attempts:
        return task.attempts[-1].status == "completed"
    return task.stages.get("content_pack") is StageStatus.COMPLETED


def find_task_by_id(
    output_root: str | Path,
    task_id: str,
) -> tuple[Path, TaskRecord] | None:
    """Find one task by its stable ID without trusting its directory name."""
    task_root = Path(output_root).resolve() / "视频学习素材"
    if not task_root.is_dir():
        return None
    for task_json in sorted(task_root.glob("*/task.json")):
        try:
            task = load_task(task_json)
        except (OSError, ValueError):
            continue
        if task.task_id == task_id:
            return task_json.parent, task
    return None
