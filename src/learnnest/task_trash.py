"""Recoverable removal of one stopped task and its identity facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from learnnest.automation_models import AutomationTaskState
from learnnest.automation_store import find_intake
from learnnest.locks import LockUnavailable, task_lock
from learnnest.models import StageStatus
from learnnest.task_store import (
    find_task_by_id,
    find_task_by_identities,
    parse_task_bytes,
)


class TaskTrashError(ValueError):
    """A safe, human-readable task trash failure."""


@dataclass(frozen=True)
class TrashedTask:
    task_id: str
    trash_path: Path


@dataclass(frozen=True)
class TrashedTaskSummary:
    bundle_id: str
    task_id: str
    title: str
    trashed_at: datetime


@dataclass(frozen=True)
class RestoredTask:
    task_id: str


@dataclass(frozen=True)
class _TrashBundle:
    path: Path
    summary: TrashedTaskSummary
    task: object
    items: tuple[tuple[Path, Path], ...]


def trash_task(output_root: str | Path, task_id: str) -> TrashedTask:
    """Move one stopped task and its identity facts into an internal trash bundle."""
    root = Path(output_root).resolve()
    try:
        with task_lock(root, task_id, timeout=0):
            found = find_task_by_id(root, task_id)
            if found is None:
                raise KeyError(task_id)
            task_dir, task = found
            _require_stopped_task(root, task_id, task)
            targets = _trash_targets(root, task_dir, task_id)
            trash_path = _new_trash_path(root, task_id)
            _move_targets(root, trash_path, task_id, targets)
            return TrashedTask(task_id=task_id, trash_path=trash_path)
    except KeyError:
        raise
    except LockUnavailable as error:
        raise TaskTrashError("任务正在处理，暂时不能删除。") from error
    except TaskTrashError:
        raise
    except (OSError, ValueError) as error:
        raise TaskTrashError("任务未能移入回收区；现有内容已尽量保留。") from error


def list_trashed_tasks(output_root: str | Path) -> tuple[TrashedTaskSummary, ...]:
    """Return safe summaries for valid, completed trash bundles."""
    root = Path(output_root).resolve()
    trash_root = root / ".learnnest" / "trash" / "tasks"
    if not trash_root.is_dir():
        return ()
    items: list[TrashedTaskSummary] = []
    for path in trash_root.iterdir():
        if not path.is_dir():
            continue
        try:
            items.append(_read_bundle(root, path.name).summary)
        except (OSError, ValueError, TaskTrashError):
            continue
    items.sort(key=lambda item: (item.trashed_at, item.bundle_id), reverse=True)
    return tuple(items)


def restore_trashed_task(output_root: str | Path, bundle_id: str) -> RestoredTask:
    """Restore one valid trash bundle without overwriting current facts."""
    root = Path(output_root).resolve()
    bundle = _read_bundle(root, bundle_id)
    task_id = bundle.summary.task_id
    try:
        with task_lock(root, task_id, timeout=0):
            if find_task_by_id(root, task_id) is not None:
                raise TaskTrashError("当前任务列表已有同一任务，不能覆盖恢复。")
            task = bundle.task
            existing = find_task_by_identities(
                root,
                getattr(task, "identities"),
                getattr(task, "source_fingerprint"),
                include_derived=True,
            )
            if existing is not None:
                raise TaskTrashError("当前任务列表已有同一视频，请先处理现有任务。")
            if any(destination.exists() for _source, destination in bundle.items):
                raise TaskTrashError("当前任务事实与回收内容冲突，不能覆盖恢复。")
            _restore_items(bundle)
    except LockUnavailable as error:
        raise TaskTrashError("任务状态正在变化，请稍后再恢复。") from error
    except TaskTrashError:
        raise
    except (OSError, ValueError) as error:
        raise TaskTrashError("任务未能恢复；回收内容已尽量保留。") from error
    return RestoredTask(task_id=task_id)


def purge_trashed_task(output_root: str | Path, bundle_id: str) -> str:
    """Permanently remove one exact, validated trash bundle."""
    root = Path(output_root).resolve()
    bundle = _read_bundle(root, bundle_id)
    task_id = bundle.summary.task_id
    try:
        with task_lock(root, task_id, timeout=0):
            shutil.rmtree(bundle.path)
    except LockUnavailable as error:
        raise TaskTrashError("任务状态正在变化，请稍后再永久删除。") from error
    except OSError as error:
        raise TaskTrashError("回收内容未能永久删除，请稍后再试。") from error
    return task_id


def _require_stopped_task(root: Path, task_id: str, task: object) -> None:
    stages = getattr(task, "stages", {})
    if getattr(task, "active_attempt_id", None) is not None or any(
        status is StageStatus.RUNNING for status in stages.values()
    ):
        raise TaskTrashError("任务正在处理，暂时不能删除。")
    try:
        intake = find_intake(root, task_id)
    except ValueError:
        intake = None
    if intake is not None and intake.status == "claimed":
        raise TaskTrashError("任务正在处理，暂时不能删除。")
    state_dir = root / ".learnnest" / "automation" / "tasks" / task_id
    if state_dir.is_dir():
        for path in state_dir.glob("*.json"):
            try:
                state = AutomationTaskState.model_validate_json(path.read_bytes())
            except (OSError, ValueError):
                continue
            if any(attempt.status == "running" for attempt in state.attempts):
                raise TaskTrashError("任务正在处理，暂时不能删除。")


def _trash_targets(root: Path, task_dir: Path, task_id: str) -> list[tuple[Path, Path]]:
    task_root = (root / "视频学习素材").resolve()
    resolved_task = task_dir.resolve(strict=True)
    if (
        not resolved_task.is_relative_to(task_root)
        or resolved_task.parent != task_root
        or task_id != Path(task_id).name
    ):
        raise TaskTrashError("任务位置不安全，不能删除。")
    targets: list[tuple[Path, Path]] = []
    intake = root / ".learnnest" / "automation" / "intake" / f"{task_id}.json"
    states = root / ".learnnest" / "automation" / "tasks" / task_id
    if intake.is_file():
        targets.append((intake, Path("automation-intake.json")))
    if states.is_dir():
        targets.append((states, Path("automation-states")))
    jobs_root = root / ".learnnest" / "web" / "jobs"
    for source in jobs_root.glob("*.json"):
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("job_id") == source.stem
            and payload.get("task_id") == task_id
        ):
            targets.append((source, Path("source-jobs") / source.name))
    targets.append((resolved_task, Path("task")))
    return targets


def _new_trash_path(root: Path, task_id: str) -> Path:
    del task_id
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return root / ".learnnest" / "trash" / "tasks" / f"{stamp}-{uuid.uuid4().hex[:8]}"


def _read_bundle(root: Path, bundle_id: str) -> _TrashBundle:
    trash_root = (root / ".learnnest" / "trash" / "tasks").resolve()
    if not bundle_id or Path(bundle_id).name != bundle_id:
        raise KeyError(bundle_id)
    path = (trash_root / bundle_id).resolve()
    if path.parent != trash_root or not path.is_dir():
        raise KeyError(bundle_id)
    try:
        payload = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "1.0"
            or payload.get("status") != "completed"
        ):
            raise ValueError("trash manifest is invalid")
        task_id = str(payload["task_id"])
        if not task_id or Path(task_id).name != task_id:
            raise ValueError("trash task identity is invalid")
        trashed_at = datetime.fromisoformat(str(payload["trashed_at"]))
        if trashed_at.tzinfo is None:
            raise ValueError("trash timestamp is invalid")
        items_payload = payload["items"]
        if not isinstance(items_payload, list) or not items_payload:
            raise ValueError("trash items are invalid")
        items: list[tuple[Path, Path]] = []
        for item in items_payload:
            if not isinstance(item, dict):
                raise ValueError("trash item is invalid")
            original = _safe_original_path(root, str(item["original"]), task_id)
            trashed = _safe_trashed_path(path, str(item["trashed"]))
            if not trashed.exists():
                raise ValueError("trashed item is missing")
            items.append((trashed, original))
        task_dir = path / "task"
        task = parse_task_bytes(
            (task_dir / "task.json").read_bytes(), base_dir=task_dir
        )
        if task.task_id != task_id:
            raise ValueError("trashed task identity is inconsistent")
    except KeyError:
        raise
    except (
        OSError,
        UnicodeError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise TaskTrashError("回收内容无法安全读取。") from error
    return _TrashBundle(
        path=path,
        summary=TrashedTaskSummary(
            bundle_id=bundle_id,
            task_id=task_id,
            title=task.title,
            trashed_at=trashed_at,
        ),
        task=task,
        items=tuple(items),
    )


def _safe_original_path(root: Path, relative: str, task_id: str) -> Path:
    candidate_relative = Path(relative)
    if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
        raise ValueError("trash original path is unsafe")
    candidate = (root / candidate_relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("trash original path escapes the output root")
    normalized = candidate_relative.as_posix()
    allowed = (
        normalized.startswith("视频学习素材/")
        and len(candidate_relative.parts) == 2
        or normalized == f".learnnest/automation/intake/{task_id}.json"
        or normalized == f".learnnest/automation/tasks/{task_id}"
        or (
            normalized.startswith(".learnnest/web/jobs/")
            and len(candidate_relative.parts) == 4
            and candidate.suffix == ".json"
        )
    )
    if not allowed:
        raise ValueError("trash original path is not an owned task fact")
    return candidate


def _safe_trashed_path(bundle_path: Path, relative: str) -> Path:
    candidate_relative = Path(relative)
    if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
        raise ValueError("trashed path is unsafe")
    candidate = (bundle_path / candidate_relative).resolve()
    if not candidate.is_relative_to(bundle_path):
        raise ValueError("trashed path escapes its bundle")
    return candidate


def _restore_items(bundle: _TrashBundle) -> None:
    ordered = sorted(bundle.items, key=lambda item: item[0].name == "task")
    moved: list[tuple[Path, Path]] = []
    try:
        for source, destination in ordered:
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            moved.append((source, destination))
    except Exception:
        for source, destination in reversed(moved):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                destination.replace(source)
            except OSError:
                pass
        raise
    try:
        shutil.rmtree(bundle.path)
    except OSError:
        pass


def _move_targets(
    root: Path,
    trash_path: Path,
    task_id: str,
    targets: list[tuple[Path, Path]],
) -> None:
    trash_root = (root / ".learnnest" / "trash" / "tasks").resolve()
    resolved_trash = trash_path.resolve()
    if not resolved_trash.is_relative_to(trash_root):
        raise TaskTrashError("回收区位置不安全，不能删除。")
    trash_path.mkdir(parents=True, exist_ok=False)
    manifest_path = trash_path / "manifest.json"
    manifest = {
        "schema_version": "1.0",
        "task_id": task_id,
        "trashed_at": datetime.now(UTC).isoformat(),
        "status": "moving",
        "items": [
            {
                "original": source.resolve().relative_to(root).as_posix(),
                "trashed": destination.as_posix(),
            }
            for source, destination in targets
        ],
    }
    _write_manifest(manifest_path, manifest)
    moved: list[tuple[Path, Path]] = []
    try:
        for source, relative in targets:
            destination = trash_path / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            moved.append((source, destination))
        _write_manifest(manifest_path, {**manifest, "status": "completed"})
    except Exception:
        for source, destination in reversed(moved):
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                destination.replace(source)
            except OSError:
                pass
        raise


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
