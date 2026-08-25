"""Human-facing projection over persisted LearnNest task facts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from ipaddress import ip_address
import json
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

from learnnest.automation_models import AutomationIntake
from learnnest.execution import plan_recovery
from learnnest.automation_store import find_intake
from learnnest.learning_state import (
    automation_budget_blocked,
    automation_budget_restart_is_due,
    automation_failure_reason,
    automation_failure_stage,
    automation_restart_is_safe,
    automation_readiness,
    automation_retry_is_due,
    automation_task_state,
)
from learnnest.locks import LockUnavailable, task_lock
from learnnest.models import StageStatus, TaskRecord
from learnnest.pipeline import PipelineError, process_source, process_video, rerun_task
from learnnest.publication import note_belongs_to_task, read_audio_ownership_marker
from learnnest.sources import SourceParseError, collect_sources
from learnnest.task_control import CONTROL_FILENAME, load_task_control, set_manual_pause
from learnnest.task_store import find_task_by_id, parse_task_bytes
from learnnest.tts_generation import probe_audio

DesiredOutput = Literal["readable_note", "materials_only"]
OutputGoal = Literal["complete_note", "complete_note_with_audio"]
LearningActionKind = Literal[
    "open_note",
    "open_settings",
    "open_automation",
    "open_sources",
    "open_single_video",
    "continue",
    "start_automation",
    "retry_automation",
    "resume_task",
]
LearningState = Literal[
    "materials_ready",
    "waiting_setup",
    "waiting_authorization",
    "queued",
    "organizing",
    "partial_ready",
    "needs_action",
    "ready",
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
    action_kind: LearningActionKind | None
    failure_reason: str | None = None
    output_goal: OutputGoal = "complete_note"
    manually_paused: bool = False
    can_pause: bool = True
    failure_stage: str | None = None
    failure_stage_code: str | None = None

    def __post_init__(self) -> None:
        if (self.action is None) != (self.action_kind is None):
            raise ValueError("learning action and action kind must be paired")


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
                            "open_settings",
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

    def pause_item(self, item_ref: str) -> LearningItem:
        """Persist a control fact that blocks only future task scheduling."""
        return self._set_manual_pause(item_ref, paused=True)

    def resume_item(self, item_ref: str) -> LearningItem:
        """Clear only the manual pause control without advancing task facts."""
        return self._set_manual_pause(item_ref, paused=False)

    def _set_manual_pause(self, item_ref: str, *, paused: bool) -> LearningItem:
        try:
            found = find_task_by_id(self.output_root, item_ref)
            if found is None:
                raise KeyError(item_ref)
            task_dir, task = found
            current = self._item(task_dir, task)
            if paused and not current.can_pause:
                raise LearningWorkspaceError("任务已经完成，不需要暂停。")
            set_manual_pause(
                self.output_root,
                task_dir,
                task.task_id,
                paused=paused,
            )
            return self._item(task_dir, task)
        except (KeyError, LearningWorkspaceError):
            raise
        except (LockUnavailable, OSError, ValueError) as error:
            raise _safe_workspace_error(error) from error

    def snapshot(self, known_revision: str | None = None) -> LearningSnapshot:
        """Read persisted task/intake facts only; this method never starts work."""
        entries, revision = self._task_entries_and_revision()
        if known_revision == revision:
            return LearningSnapshot(revision, True, (), (), ())
        inbox: list[LearningItem] = []
        processing: list[LearningItem] = []
        library: list[LearningItem] = []
        for task_dir, task in entries:
            item = self._item(task_dir, task)
            if item.state in {"ready", "partial_ready"}:
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

    def audio(self, item_ref: str) -> Path:
        """Return only this task's validated, ownership-bound published MP3."""
        found = find_task_by_id(self.output_root, item_ref)
        if found is None:
            raise KeyError(item_ref)
        task_dir, task = found
        if task.stages.get("tts") is not StageStatus.COMPLETED:
            raise LearningWorkspaceError("音频暂时不能播放；已保留现有内容。")
        metadata = _audio_metadata(task_dir, task)
        published = _safe_published_audio(self.output_root, metadata)
        try:
            marker = read_audio_ownership_marker(published)
            expected_sha = metadata["mp3_sha256"]
            if (
                marker is None
                or marker.get("task_id") != task.task_id
                or marker.get("mp3_sha256") != expected_sha
                or marker.get("status") != "completed"
                or sha256(published.read_bytes()).hexdigest() != expected_sha
                or not _is_decodable_mp3(probe_audio(published))
            ):
                raise ValueError("audio ownership is invalid")
        except (OSError, ValueError, TypeError) as error:
            raise LearningWorkspaceError(
                "音频暂时不能播放；已保留现有内容。"
            ) from error
        return published

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
        if task_root.is_dir():
            for task_json in sorted(
                task_root.glob("*/task.json"), key=lambda path: path.parent.name
            ):
                try:
                    raw = task_json.read_bytes()
                except OSError:
                    continue
                _digest_fact(digest, "task", task_json.parent.name, raw)
                control_path = task_json.parent / CONTROL_FILENAME
                if control_path.is_file():
                    try:
                        control_raw = control_path.read_bytes()
                    except OSError:
                        control_raw = b""
                    _digest_fact(
                        digest, "task-control", task_json.parent.name, control_raw
                    )
                try:
                    entries.append(
                        (
                            task_json.parent,
                            parse_task_bytes(raw, base_dir=task_json.parent),
                        )
                    )
                except (OSError, ValueError):
                    continue
        intake_root = self.output_root / ".learnnest" / "automation" / "intake"
        if intake_root.is_dir():
            for intake_json in sorted(
                intake_root.glob("*.json"), key=lambda path: path.name
            ):
                try:
                    raw = intake_json.read_bytes()
                except OSError:
                    continue
                _digest_fact(digest, "intake", intake_json.name, raw)
        state_root = self.output_root / ".learnnest" / "automation" / "tasks"
        if state_root.is_dir():
            for state_json in sorted(
                state_root.glob("*/*.json"), key=lambda path: path.as_posix()
            ):
                try:
                    raw = state_json.read_bytes()
                except OSError:
                    continue
                _digest_fact(
                    digest,
                    "automation-state",
                    state_json.relative_to(state_root).as_posix(),
                    raw,
                )
        entries.sort(key=lambda entry: entry[0].name, reverse=True)
        return entries, digest.hexdigest()

    def _item(self, task_dir: Path, task: TaskRecord) -> LearningItem:
        try:
            intake = find_intake(self.output_root, task.task_id)
            intake_invalid = False
        except ValueError:
            intake = None
            intake_invalid = True
        item = self._item_state(
            task_dir,
            task,
            intake=intake,
            intake_invalid=intake_invalid,
        )
        output_goal: OutputGoal = (
            intake.default_output
            if intake is not None
            else "complete_note_with_audio"
            if "tts" in task.stages
            else "complete_note"
        )
        item = replace(item, output_goal=output_goal)
        can_pause = item.state != "ready"
        try:
            manually_paused = load_task_control(task_dir, task.task_id).manually_paused
        except (OSError, ValueError):
            manually_paused = True
        if manually_paused and can_pause:
            return replace(
                item,
                message="任务已暂停；已经完成的内容会保留。",
                action="继续任务",
                action_kind="resume_task",
                manually_paused=True,
                can_pause=False,
            )
        return replace(item, manually_paused=False, can_pause=can_pause)

    def _item_state(
        self,
        task_dir: Path,
        task: TaskRecord,
        *,
        intake: AutomationIntake | None,
        intake_invalid: bool,
    ) -> LearningItem:
        note_path = _readable_note_path(task_dir, task)
        if note_path is not None:
            if (
                intake is not None
                and intake.default_output == "complete_note_with_audio"
                and task.stages.get("tts") is not StageStatus.COMPLETED
            ):
                retryable = automation_retry_is_due(self.output_root, task.task_id)
                return LearningItem(
                    task.task_id,
                    task.title,
                    _safe_source_label(task),
                    "partial_ready",
                    "笔记已完成，音频仍在处理中。",
                    "重试音频" if retryable else "打开笔记",
                    "retry_automation" if retryable else "open_note",
                )
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "ready",
                (
                    "可播放。"
                    if task.stages.get("tts") is StageStatus.COMPLETED
                    else "可阅读。"
                ),
                "打开笔记",
                "open_note",
            )
        if intake_invalid:
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "needs_action",
                "任务状态无法安全读取。请重新选择原视频处理。",
                "重新选择原视频",
                "open_single_video",
            )
        if automation_budget_blocked(self.output_root, task.task_id):
            readiness = (
                automation_readiness(self.output_root, intake)
                if intake is not None
                else "waiting_setup"
            )
            if readiness == "waiting_setup":
                return LearningItem(
                    task.task_id,
                    task.title,
                    _safe_source_label(task),
                    "needs_action",
                    "模型设置已经变化；请恢复原连接后再继续。",
                    "检查模型设置",
                    "open_settings",
                )
            if readiness == "waiting_authorization":
                return LearningItem(
                    task.task_id,
                    task.title,
                    _safe_source_label(task),
                    "waiting_authorization",
                    "调用额度已调整；重新开启付费整理许可后即可继续。",
                    "重新开启付费许可",
                    "open_automation",
                )
            if automation_budget_restart_is_due(self.output_root, task.task_id):
                note_only = (
                    intake is not None and intake.default_output == "complete_note"
                )
                return LearningItem(
                    task.task_id,
                    task.title,
                    _safe_source_label(task),
                    "needs_action",
                    (
                        "新的调用额度已经可用，可以继续生成笔记。"
                        if note_only
                        else "新的调用额度已经可用，可以继续生成笔记和音频。"
                    ),
                    "继续生成笔记" if note_only else "继续完整生产",
                    "start_automation",
                )
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "needs_action",
                "今日模型调用额度已用完；材料已经保存。",
                "调整调用额度",
                "open_settings",
            )
        execution_state = automation_task_state(self.output_root, task.task_id)
        if execution_state in {"invalid", "needs_attention", "completed"}:
            retryable = automation_retry_is_due(self.output_root, task.task_id)
            failure_reason = automation_failure_reason(self.output_root, task.task_id)
            failure_stage = automation_failure_stage(self.output_root, task.task_id)
            if retryable:
                return LearningItem(
                    task.task_id,
                    task.title,
                    _safe_source_label(task),
                    "needs_action",
                    "上次整理遇到临时问题，现在可以安全重试。",
                    "重试整理",
                    "retry_automation",
                    failure_reason,
                    failure_stage=failure_stage,
                )
            if execution_state == "needs_attention" and automation_restart_is_safe(
                self.output_root, task.task_id
            ):
                return LearningItem(
                    task.task_id,
                    task.title,
                    _safe_source_label(task),
                    "needs_action",
                    "模型调用尚未开始，已完成的材料仍然保留。可以直接重新开始整理。",
                    "重新开始整理",
                    "start_automation",
                    failure_reason,
                    failure_stage=failure_stage,
                )
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "needs_action",
                (
                    "整理已经结束，但没有可打开的标准笔记。请重新选择原视频处理。"
                    if execution_state == "completed"
                    else "任务状态无法安全读取。请重新选择原视频处理。"
                    if execution_state == "invalid"
                    else "上次整理已停止，不能安全重试。请重新选择原视频处理。"
                ),
                "重新选择原视频",
                "open_single_video",
                failure_reason,
                failure_stage=failure_stage,
            )
        if intake is not None and intake.status == "needs_attention":
            if automation_retry_is_due(self.output_root, task.task_id):
                failure_reason = automation_failure_reason(
                    self.output_root, task.task_id
                )
                return LearningItem(
                    task.task_id,
                    task.title,
                    _safe_source_label(task),
                    "needs_action",
                    "上次整理遇到临时问题，现在可以安全重试。",
                    "重试整理",
                    "retry_automation",
                    failure_reason,
                    failure_stage=(
                        automation_failure_stage(self.output_root, task.task_id)
                        if failure_reason is not None
                        else None
                    ),
                )
            if automation_restart_is_safe(self.output_root, task.task_id):
                return LearningItem(
                    task.task_id,
                    task.title,
                    _safe_source_label(task),
                    "needs_action",
                    "上次整理没有真正开始。当前设置已就绪，可以重新开始整理。",
                    "重新开始整理",
                    "start_automation",
                )
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "needs_action",
                "这项旧任务缺少可打开的标准笔记，不能在原任务上安全继续。"
                "请重新选择原视频处理。",
                "重新选择原视频",
                "open_single_video",
            )
        if intake is not None and intake.status == "claimed":
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "organizing",
                "正在整理内容。",
                None,
                None,
            )
        if intake is not None:
            readiness = automation_readiness(self.output_root, intake)
            if readiness == "waiting_setup":
                return LearningItem(
                    task.task_id,
                    task.title,
                    _safe_source_label(task),
                    "waiting_setup",
                    "请先完成整理设置。",
                    "完成设置",
                    "open_settings",
                )
            if readiness == "waiting_authorization":
                return LearningItem(
                    task.task_id,
                    task.title,
                    _safe_source_label(task),
                    "waiting_authorization",
                    "请开启付费整理许可后开始整理。",
                    "开启付费许可",
                    "open_automation",
                )
        if intake is not None and intake.status == "pending":
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "queued",
                "等待整理。",
                None,
                None,
            )
        if task.error_summary:
            (
                failure_stage_code,
                failure_stage,
                failure_reason,
                message,
                action,
                action_kind,
            ) = _task_failure_projection(task)
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "needs_action",
                message,
                action,
                action_kind,
                failure_reason=failure_reason,
                failure_stage=failure_stage,
                failure_stage_code=failure_stage_code,
            )
        if task.stages.get("content_pack") is StageStatus.COMPLETED:
            return LearningItem(
                task.task_id,
                task.title,
                _safe_source_label(task),
                "materials_ready",
                "材料已准备，可以开始整理。",
                "开始整理",
                "start_automation",
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
                None,
            )
        return LearningItem(
            task.task_id,
            task.title,
            _safe_source_label(task),
            "queued",
            "等待开始整理。",
            None,
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


def _digest_fact(digest: object, kind: str, name: str, raw: bytes) -> None:
    digest.update(kind.encode("utf-8"))  # type: ignore[attr-defined]
    digest.update(b"\0")  # type: ignore[attr-defined]
    digest.update(name.encode("utf-8"))  # type: ignore[attr-defined]
    digest.update(b"\0")  # type: ignore[attr-defined]
    digest.update(raw)  # type: ignore[attr-defined]
    digest.update(b"\0")  # type: ignore[attr-defined]


def _item_with_action(
    item: LearningItem,
    message: str,
    action: str,
    action_kind: LearningActionKind,
) -> LearningItem:
    return replace(
        item,
        state="needs_action",
        message=message,
        action=action,
        action_kind=action_kind,
        failure_reason=None,
        failure_stage=None,
        failure_stage_code=None,
    )


_PUBLIC_TASK_STAGE = {
    "source": "获取内容",
    "transcript": "识别语音",
    "frames": "提取画面",
    "ocr": "识别画面文字",
    "evidence": "整理材料",
    "content_pack": "生成材料包",
    "note": "生成笔记",
    "publish": "发布笔记",
    "podcast_script": "生成播客稿",
    "tts": "生成音频",
}
_UNSAFE_PUBLIC_FAILURE = re.compile(
    r"(?i)([A-Z]:[\\/]|\\\\|/(?:users|home|tmp|var)/|"
    r"\b[a-f0-9]{32,64}\b|settings.?sha|provider.?binding|task[_ -]?id|secret)"
)
_EXCEPTION_SUFFIX = re.compile(r"\s*\([A-Za-z_][A-Za-z0-9_.]*\)\s*$")


def _task_failure_projection(
    task: TaskRecord,
) -> tuple[str, str, str, str, str, LearningActionKind]:
    latest = next(
        (
            attempt
            for attempt in reversed(task.attempts)
            if attempt.status == "failed" and attempt.failed_stage is not None
        ),
        None,
    )
    failed_stage = (
        latest.failed_stage
        if latest is not None
        else next(
            (
                stage
                for stage, status in task.stages.items()
                if status is StageStatus.FAILED
            ),
            "source",
        )
    )
    stage = _PUBLIC_TASK_STAGE.get(failed_stage, "处理内容")
    failure = latest.failure if latest is not None else None
    summary = failure.safe_summary if failure is not None else task.error_summary or ""

    if failed_stage == "source" and "抖音下载需要有效登录状态" in summary:
        return (
            failed_stage,
            stage,
            "抖音下载需要有效登录状态；请在来源页确认已连接抖音后重试。",
            "内容尚未获取；请前往来源页确认抖音连接后重试。",
            "前往来源",
            "open_sources",
        )

    if failed_stage == "source" and (
        "URL 下载失败" in summary or "DownloadError" in summary
    ):
        return (
            failed_stage,
            stage,
            "来源链接的视频下载失败。",
            "来源链接没有成功下载；请选择本地视频重新处理。",
            "选择本地视频",
            "open_single_video",
        )

    disposition = failure.disposition if failure is not None else "manual"
    public_reason = _public_task_failure_reason(stage, summary, disposition)
    if disposition == "retryable":
        return (
            failed_stage,
            stage,
            public_reason,
            f"失败发生在“{stage}”；已完成的内容会保留，现在可以重试。",
            "继续处理",
            "continue",
        )
    if failed_stage == "source":
        return (
            failed_stage,
            stage,
            public_reason,
            "内容尚未成功获取；请选择可访问的本地视频重新处理。",
            "选择本地视频",
            "open_single_video",
        )
    return (
        failed_stage,
        stage,
        public_reason,
        f"失败发生在“{stage}”；已完成的内容会保留，请修正问题后继续。",
        "继续处理",
        "continue",
    )


def _public_task_failure_reason(stage: str, summary: str, disposition: str) -> str:
    cleaned = _EXCEPTION_SUFFIX.sub("", " ".join(summary.split())).strip(" 。；;")
    if cleaned and not _UNSAFE_PUBLIC_FAILURE.search(cleaned):
        return f"{cleaned}。"
    if disposition == "retryable":
        return f"{stage}遇到临时错误。"
    if disposition == "terminal":
        return f"{stage}遇到无法继续的错误。"
    return f"{stage}遇到需要人工处理的问题。"


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


def _audio_metadata(task_dir: Path, task: TaskRecord) -> dict[str, str]:
    root = task_dir.resolve()
    candidates = [
        (task_dir / relative).resolve()
        for relative in task.artifacts.get("tts", ())
        if Path(relative).name == "audio.json" and not Path(relative).is_absolute()
    ]
    if len(candidates) != 1 or not candidates[0].is_relative_to(root):
        raise LearningWorkspaceError("音频暂时不能播放；已保留现有内容。")
    try:
        payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LearningWorkspaceError("音频暂时不能播放；已保留现有内容。") from error
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), str)
        for key in ("published_path", "mp3_sha256")
    ):
        raise LearningWorkspaceError("音频暂时不能播放；已保留现有内容。")
    return {
        "published_path": str(payload["published_path"]),
        "mp3_sha256": str(payload["mp3_sha256"]),
    }


def _safe_published_audio(output_root: Path, metadata: dict[str, str]) -> Path:
    relative = Path(metadata["published_path"])
    if relative.is_absolute() or relative.suffix.lower() != ".mp3":
        raise LearningWorkspaceError("音频暂时不能播放；已保留现有内容。")
    candidate = (output_root.resolve() / relative).resolve()
    if not candidate.is_relative_to(output_root.resolve()) or not candidate.is_file():
        raise LearningWorkspaceError("音频暂时不能播放；已保留现有内容。")
    return candidate


def _is_decodable_mp3(probe: object) -> bool:
    if not isinstance(probe, dict):
        return False
    streams = probe.get("streams")
    if not isinstance(streams, list) or not any(
        isinstance(item, dict)
        and item.get("codec_type") == "audio"
        and item.get("codec_name") == "mp3"
        for item in streams
    ):
        return False
    try:
        return float(probe.get("format", {}).get("duration", 0)) > 0
    except (TypeError, ValueError, AttributeError):
        return False
