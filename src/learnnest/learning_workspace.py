"""Human-facing projection over persisted LearnNest task facts."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from learnnest.execution import plan_recovery
from learnnest.locks import LockUnavailable, task_lock
from learnnest.models import StageStatus, TaskRecord
from learnnest.pipeline import PipelineError, process_source, process_video, rerun_task
from learnnest.publication import note_belongs_to_task
from learnnest.sources import SourceParseError, collect_sources
from learnnest.task_store import find_task_by_id, load_task

DesiredOutput = Literal["readable_note", "materials_only"]
LearningState = Literal[
    "queued", "organizing", "materials_ready", "needs_action", "ready"
]
_NON_PUBLIC_HOST_SUFFIXES = (
    ".example",
    ".home",
    ".home.arpa",
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localhost",
    ".test",
)


class LearningWorkspaceError(ValueError):
    """A safe, human-readable workspace failure."""


@dataclass(frozen=True)
class LearningItem:
    """A non-persistent, human-readable view of one existing task."""

    item_ref: str
    title: str
    source: str
    state: LearningState
    message: str
    action: str | None


@dataclass(frozen=True)
class LearningSnapshot:
    """A revisioned projection for the inbox, processing list, and library."""

    revision: str
    unchanged: bool
    inbox: tuple[LearningItem, ...]
    processing: tuple[LearningItem, ...]
    library: tuple[LearningItem, ...]


@dataclass(frozen=True)
class LearningNote:
    """One verified task-internal Markdown note ready for safe rendering."""

    item: LearningItem
    task_dir: Path
    note_path: Path
    markdown: str


@dataclass(frozen=True)
class ContinueResult:
    """The outcome of one explicit request to continue deterministic work."""

    item: LearningItem
    outcome: Literal["continued", "needs_setup", "already_ready"]


class LearningWorkspace:
    """Use the existing pipeline without creating a second task model or store."""

    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root).resolve()

    def add_content(
        self, source: str, desired_output: DesiredOutput = "readable_note"
    ) -> LearningItem:
        """Process one source through the existing local pipeline profile."""
        profile = _profile_for(desired_output)
        source_item = self._source_item(source)
        try:
            if source_item.input_type == "local_file":
                task = process_video(Path(source_item.input), self.output_root, profile)
            else:
                task = process_source(source_item, self.output_root, profile)
        except (LockUnavailable, PipelineError, OSError, ValueError) as error:
            raise _safe_workspace_error(error) from error
        found = find_task_by_id(self.output_root, task.task_id)
        if found is None:
            raise LearningWorkspaceError("内容已处理，但暂时无法读取结果；请稍后查看。")
        task_dir, persisted = found
        return self._item(task_dir, persisted)

    def continue_item(self, item_ref: str) -> ContinueResult:
        """Continue only a safe deterministic recovery path."""
        found = find_task_by_id(self.output_root, item_ref)
        if found is None:
            raise KeyError(item_ref)
        task_dir, persisted = found
        try:
            with task_lock(self.output_root, persisted.task_id, timeout=0):
                recovery = plan_recovery(task_dir)
                if recovery.requires_paid:
                    return ContinueResult(
                        item=_item_with_action(
                            self._item(task_dir, persisted),
                            "需要完成设置后才能继续。",
                            "完成设置",
                        ),
                        outcome="needs_setup",
                    )
                if recovery.from_stage is None:
                    return ContinueResult(
                        item=self._item(task_dir, persisted), outcome="already_ready"
                    )
                task = rerun_task(
                    task_dir,
                    recovery.from_stage,
                    reason="resume",
                    _task_lock_held=True,
                )
        except (LockUnavailable, PipelineError, OSError, ValueError) as error:
            raise _safe_workspace_error(error) from error
        refreshed = find_task_by_id(self.output_root, task.task_id)
        if refreshed is None:
            raise LearningWorkspaceError("已继续处理，但暂时无法读取结果；请稍后查看。")
        refreshed_dir, refreshed_task = refreshed
        return ContinueResult(
            item=self._item(refreshed_dir, refreshed_task), outcome="continued"
        )

    def snapshot(self, known_revision: str | None = None) -> LearningSnapshot:
        """Read task JSON snapshots only; this method never starts work."""
        entries, revision = self._task_entries_and_revision()
        if known_revision == revision:
            return LearningSnapshot(revision, True, (), (), ())
        inbox: list[LearningItem] = []
        processing: list[LearningItem] = []
        library: list[LearningItem] = []
        for task_dir, task in entries:
            item = self._item(task_dir, task)
            if item.state == "ready":
                library.append(item)
            elif item.state in {"queued", "materials_ready"}:
                inbox.append(item)
            else:
                processing.append(item)
        return LearningSnapshot(
            revision,
            False,
            tuple(inbox),
            tuple(processing),
            tuple(library),
        )

    def item(self, item_ref: str) -> LearningItem:
        found = find_task_by_id(self.output_root, item_ref)
        if found is None:
            raise KeyError(item_ref)
        return self._item(*found)

    def note(self, item_ref: str) -> LearningNote:
        found = find_task_by_id(self.output_root, item_ref)
        if found is None:
            raise KeyError(item_ref)
        task_dir, task = found
        note_path = _readable_note_path(task_dir, task)
        if note_path is None:
            raise LearningWorkspaceError("笔记暂时不能打开；已保留现有内容。")
        try:
            markdown = note_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise LearningWorkspaceError(
                "笔记暂时不能读取；已保留现有内容。"
            ) from error
        return LearningNote(self._item(task_dir, task), task_dir, note_path, markdown)

    def _source_item(self, source: str):
        try:
            items = collect_sources(input_value=source)
        except SourceParseError as error:
            raise LearningWorkspaceError(
                "无法添加内容，请提供一个本地视频或公开链接。"
            ) from error
        if len(items) != 1:
            raise LearningWorkspaceError("一次只能添加一项内容。")
        if items[0].input_type == "url":
            validate_public_web_url(items[0].input)
        return items[0]

    def _task_entries_and_revision(self) -> tuple[list[tuple[Path, TaskRecord]], str]:
        task_root = self.output_root / "视频学习素材"
        digest = sha256()
        entries: list[tuple[Path, TaskRecord]] = []
        if not task_root.is_dir():
            return entries, digest.hexdigest()
        for task_json in sorted(
            task_root.glob("*/task.json"), key=lambda path: path.parent.name
        ):
            try:
                raw = task_json.read_bytes()
            except OSError:
                continue
            digest.update(task_json.parent.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(raw)
            digest.update(b"\0")
            try:
                entries.append((task_json.parent, load_task(task_json)))
            except (OSError, ValueError):
                continue
        entries.sort(key=lambda entry: entry[0].name, reverse=True)
        return entries, digest.hexdigest()

    def _item(self, task_dir: Path, task: TaskRecord) -> LearningItem:
        note_path = _readable_note_path(task_dir, task)
        if note_path is not None:
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "ready",
                "可阅读。",
                "打开笔记",
            )
        if task.error_summary:
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "needs_action",
                "处理需要继续；已保留完成的内容。",
                "继续处理",
            )
        if task.stages.get("content_pack") is StageStatus.COMPLETED:
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "materials_ready",
                "材料已整理，正在准备学习笔记。",
                None,
            )
        if task.active_attempt_id is not None or any(
            status is StageStatus.RUNNING for status in task.stages.values()
        ):
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "organizing",
                "正在整理内容。",
                None,
            )
        return LearningItem(
            task.task_id,
            task.title,
            _safe_source_label(task),
            "queued",
            "等待开始整理。",
            None,
        )


def _profile_for(desired_output: DesiredOutput) -> Literal["note", "evidence"]:
    if desired_output == "readable_note":
        return "note"
    if desired_output == "materials_only":
        return "evidence"
    raise LearningWorkspaceError("请选择要生成的学习结果。")


def validate_public_web_url(value: str) -> None:
    """Reject URL hosts that are explicitly local or non-public."""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError as error:
        raise LearningWorkspaceError(
            "无法添加内容，请提供一个本地视频或公开链接。"
        ) from error
    if parsed.scheme not in {"http", "https"} or not host:
        raise LearningWorkspaceError("无法添加内容，请提供一个本地视频或公开链接。")
    try:
        address = ip_address(host)
    except ValueError:
        if (
            "." not in host
            or all(character in "0123456789." for character in host)
            or any(
                host == suffix[1:] or host.endswith(suffix)
                for suffix in _NON_PUBLIC_HOST_SUFFIXES
            )
        ):
            raise LearningWorkspaceError("无法添加内容，请提供一个本地视频或公开链接。")
        return
    if not address.is_global or address.is_multicast:
        raise LearningWorkspaceError("无法添加内容，请提供一个本地视频或公开链接。")


def _readable_note_path(task_dir: Path, task: TaskRecord) -> Path | None:
    if task.stages.get("publish") is not StageStatus.COMPLETED:
        return None
    declared = task.artifacts.get("publish", ())
    root = task_dir.resolve()
    for relative in declared:
        candidate = (task_dir / relative).resolve()
        if (
            Path(relative).is_absolute()
            or not candidate.is_relative_to(root)
            or candidate.suffix.lower() != ".md"
            or not candidate.is_file()
            or not note_belongs_to_task(candidate, task.task_id)
        ):
            continue
        try:
            candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        return candidate
    return None


def _safe_source_label(task: TaskRecord) -> str:
    source = task.source_input or task.source_path
    if task.source_type == "url":
        parsed = urlsplit(source)
        host = parsed.hostname or "公开链接"
        return f"{host}{parsed.path}".strip("/") or host
    return Path(source).name or "本地内容"


def _item_with_action(item: LearningItem, message: str, action: str) -> LearningItem:
    return LearningItem(
        item.item_ref, item.title, item.source, "needs_action", message, action
    )


def _safe_workspace_error(error: Exception) -> LearningWorkspaceError:
    if isinstance(error, LockUnavailable):
        return LearningWorkspaceError("内容正在处理中；已保留现有内容，请稍后再试。")
    if isinstance(error, SourceParseError):
        return LearningWorkspaceError("无法添加内容，请提供一个本地视频或公开链接。")
    if isinstance(error, (PipelineError, OSError, ValueError)):
        return LearningWorkspaceError(
            "处理暂时无法继续；已保留完成的内容，请稍后再试。"
        )
    return LearningWorkspaceError("暂时无法完成操作；已保留现有内容，请稍后再试。")
