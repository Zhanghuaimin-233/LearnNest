"""Local plan/state execution for Markdown-only assisted-draft notes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from learnnest.assisted_note_models import (
    AssistedConnectionSnapshot,
    AssistedExecutionPlan,
    AssistedPlanState,
    AssistedRoleState,
    AssistedTaskPlan,
    AssistedTaskState,
)
from learnnest.models import ContentPack, StageStatus, TaskRecord
from learnnest.task_store import find_task_by_id

_SAFE_FILE_PART = re.compile(r"[^A-Za-z0-9._-]+")
_SENSITIVE = re.compile(
    r"(?i)(authorization\s*:|bearer\s+|api[_ -]?key\s*[:=]|cookie\s*:|\b(?:sk|tp)-[A-Za-z0-9_-]{8,})"
)


class AssistedNoteProvider(Protocol):
    name: str
    model: str
    endpoint_identity: str

    def write_markdown(self, dossier_json: str) -> str: ...

    def review_markdown(self, dossier_json: str, candidate_markdown: str) -> str: ...


@dataclass(frozen=True)
class _TaskContext:
    root: Path
    plan: AssistedExecutionPlan
    task_plan: AssistedTaskPlan
    task_dir: Path
    task: TaskRecord
    dossier_json: str
    bundle_dir: Path


def create_assisted_plan(
    output_root: str | Path,
    task_ids: list[str],
    *,
    writer: AssistedConnectionSnapshot,
    reviewer: AssistedConnectionSnapshot,
    max_role_calls: int = 1,
    now: datetime | None = None,
) -> Path:
    """Freeze task input and connection identities without a provider call."""
    root = Path(output_root).resolve()
    if not task_ids:
        raise ValueError("assisted plan requires at least one task ID")
    if not 1 <= max_role_calls <= 4:
        raise ValueError("assisted max_role_calls must be between 1 and 4")
    created_at = now or datetime.now(UTC)
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("assisted plan time must be timezone-aware")
    task_plans: list[AssistedTaskPlan] = []
    dossiers: dict[str, str] = {}
    for task_id in task_ids:
        found = find_task_by_id(root, task_id)
        if found is None:
            raise ValueError(f"task not found: {task_id}")
        task_dir, task = found
        if task.stages.get("content_pack") is not StageStatus.COMPLETED:
            raise ValueError(f"task has no completed content pack: {task_id}")
        content_pack_bytes = _content_pack_path(task_dir, task).read_bytes()
        content_pack = ContentPack.model_validate_json(content_pack_bytes)
        _assert_task_matches_pack(task, content_pack)
        dossier_json = build_reader_dossier(task, content_pack)
        task_plans.append(
            AssistedTaskPlan(
                task_id=task.task_id,
                task_dir=task_dir.resolve().relative_to(root).as_posix(),
                source_fingerprint=task.source_fingerprint,
                content_pack_sha256=_sha256_bytes(content_pack_bytes),
                dossier_sha256=_sha256_text(dossier_json),
                max_calls=2 * max_role_calls,
            )
        )
        dossiers[task.task_id] = dossier_json
    identity_json = json.dumps(
        {
            "tasks": [item.model_dump(mode="json") for item in task_plans],
            "writer": writer.model_dump(mode="json"),
            "reviewer": reviewer.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    plan_id = (
        f"assisted-{created_at.astimezone(UTC):%Y%m%dT%H%M%SZ}-"
        f"{_sha256_text(identity_json)[:12]}"
    )
    plan = AssistedExecutionPlan(
        plan_id=plan_id,
        created_at=created_at.astimezone(UTC),
        writer=writer,
        reviewer=reviewer,
        tasks=task_plans,
        total_max_calls=2 * max_role_calls * len(task_plans),
    )
    plan_dir = _plan_dir(root, plan.plan_id)
    if plan_dir.exists():
        raise FileExistsError(f"assisted plan already exists: {plan.plan_id}")
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "plan.json"
    _write_json_atomic(plan_path, plan.model_dump(mode="json"))
    for task_plan in plan.tasks:
        bundle_dir = root / Path(task_plan.task_dir) / "assisted-draft" / plan.plan_id
        _write_text_atomic(bundle_dir / "dossier.json", dossiers[task_plan.task_id])
    _write_json_atomic(
        _state_path(plan_path), _initial_state(plan).model_dump(mode="json")
    )
    return plan_path


def build_reader_dossier(task: TaskRecord, content_pack: ContentPack) -> str:
    """Create the deterministic, evidence-only projection shown to both roles."""
    _assert_task_matches_pack(task, content_pack)
    ordered_evidence = sorted(
        content_pack.evidence,
        key=lambda item: (item.start_ms is None, item.start_ms or 0, item.id),
    )
    transcript_segments: list[dict[str, object]] = []
    ocr_by_frame: dict[str, list[dict[str, str]]] = {}
    frame_ids = {item.id for item in ordered_evidence if item.kind == "frame"}
    orphaned_ocr: list[dict[str, str]] = []

    for evidence in ordered_evidence:
        if evidence.kind == "transcript":
            transcript_segments.append(
                {
                    "start_ms": evidence.start_ms,
                    "end_ms": evidence.end_ms,
                    "content": evidence.text,
                }
            )
        elif evidence.kind == "ocr":
            entry = {"content": evidence.text or ""}
            if evidence.frame_id in frame_ids:
                ocr_by_frame.setdefault(evidence.frame_id, []).append(entry)
            else:
                orphaned_ocr.append(entry)

    visual_frames: list[dict[str, object]] = []
    for evidence in ordered_evidence:
        if evidence.kind != "frame":
            continue
        visual_frames.append(
            {
                "start_ms": evidence.start_ms,
                "visual_context": (
                    "Only OCR extracted from this frame is supplied; frame pixels "
                    "are not included."
                ),
                "ocr": ocr_by_frame.get(evidence.id, []),
            }
        )
    if not transcript_segments and not visual_frames and not orphaned_ocr:
        raise ValueError("content pack has no source evidence")
    payload = {
        "schema_version": "1.1",
        "task": {
            "title": task.title,
            "source_type": task.source_type,
            "source_fingerprint": task.source_fingerprint,
        },
        "instructions": {
            "scope": "Use only this dossier. It may be incomplete.",
            "uncertainty": "State uncertainty rather than adding unsupported facts.",
            "source_separation": (
                "Transcript and frame OCR are separate source streams. Do not merge "
                "conflicting statements into a fact."
            ),
        },
        "source_material": {
            "transcript": {
                "description": "ASR transcript. It is not independently verified against frame OCR.",
                "segments": transcript_segments,
            },
            "visual_frames": visual_frames,
            "orphaned_ocr": orphaned_ocr,
        },
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def load_assisted_plan(path: str | Path) -> AssistedExecutionPlan:
    plan_path = _plan_json_path(path)
    try:
        return AssistedExecutionPlan.model_validate_json(plan_path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("assisted plan is missing or invalid") from error


def load_assisted_state(path: str | Path) -> AssistedPlanState:
    plan_path = _plan_json_path(path)
    plan = load_assisted_plan(plan_path)
    state_path = _state_path(plan_path)
    if not state_path.is_file():
        return _initial_state(plan)
    try:
        state = AssistedPlanState.model_validate_json(state_path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("assisted plan state is missing or invalid") from error
    if state.plan_id != plan.plan_id or [item.task_id for item in state.tasks] != [
        item.task_id for item in plan.tasks
    ]:
        raise ValueError("assisted plan state does not match plan")
    return state


def generate_assisted_plan(
    plan_path: str | Path,
    output_root: str | Path,
    provider: AssistedNoteProvider,
    *,
    retry_failed: bool = False,
) -> AssistedPlanState:
    """Perform at most one explicit Markdown Writer call per planned task."""
    plan_file = _plan_json_path(plan_path)
    plan = load_assisted_plan(plan_file)
    _assert_provider_matches(provider, plan.writer)
    state = load_assisted_state(plan_file)
    root = Path(output_root).resolve()
    for task_plan in plan.tasks:
        current = _task_state(state, task_plan.task_id)
        if current.writer.status == "completed":
            continue
        if current.writer.status == "failed":
            if (
                not retry_failed
                or current.writer.actual_call_count >= current.writer.max_calls
            ):
                continue
            _set_role(
                state,
                task_plan.task_id,
                "writer",
                status="pending",
                plan_path=plan_file,
            )
        if current.writer.status == "running":
            _fail_writer(
                state, task_plan.task_id, "Writer call was interrupted", plan_file
            )
            continue
        try:
            context = _load_context(root, plan, task_plan)
        except Exception as error:
            _fail_local(state, task_plan.task_id, error, plan_file)
            continue
        _set_role(
            state, task_plan.task_id, "writer", status="running", plan_path=plan_file
        )
        _increment_role_call(state, task_plan.task_id, "writer", plan_file)
        try:
            response = provider.write_markdown(context.dossier_json)
        except Exception as error:
            _fail_writer(state, task_plan.task_id, _safe_summary(error), plan_file)
            continue
        _write_text_atomic(
            context.bundle_dir / "writer" / "response.md",
            response,
        )
        try:
            candidate_path = _persist_note(
                context,
                raw_markdown=response,
                directory="candidate",
                status="draft_ready",
            )
        except Exception as error:
            _fail_local(state, task_plan.task_id, error, plan_file)
            continue
        _set_role(
            state,
            task_plan.task_id,
            "writer",
            status="completed",
            output_path=_relative_to_task(context, candidate_path),
            plan_path=plan_file,
        )
        _replace_task(
            state,
            task_plan.task_id,
            _task_state(state, task_plan.task_id).model_copy(
                update={
                    "status": "draft_ready",
                    "candidate_path": _relative_to_task(context, candidate_path),
                    "safe_summary": None,
                }
            ),
            plan_file,
        )
    return state


def review_assisted_plan(
    plan_path: str | Path,
    output_root: str | Path,
    provider: AssistedNoteProvider,
    *,
    retry_failed: bool = False,
) -> AssistedPlanState:
    """Perform at most one explicit Markdown Reviewer call per draft-ready task."""
    plan_file = _plan_json_path(plan_path)
    plan = load_assisted_plan(plan_file)
    _assert_provider_matches(provider, plan.reviewer)
    state = load_assisted_state(plan_file)
    root = Path(output_root).resolve()
    for task_plan in plan.tasks:
        current = _task_state(state, task_plan.task_id)
        if current.reviewer.status == "completed":
            continue
        if current.reviewer.status == "failed":
            if (
                not retry_failed
                or current.reviewer.actual_call_count >= current.reviewer.max_calls
            ):
                continue
            _set_role(
                state,
                task_plan.task_id,
                "reviewer",
                status="pending",
                plan_path=plan_file,
            )
        if current.writer.status != "completed" or current.status != "draft_ready":
            continue
        if current.reviewer.status == "running":
            _fail_reviewer(
                state, task_plan.task_id, "Reviewer call was interrupted", plan_file
            )
            continue
        try:
            context = _load_context(root, plan, task_plan)
            candidate_raw = (context.bundle_dir / "writer" / "response.md").read_text(
                encoding="utf-8"
            )
            _normalized_markdown(candidate_raw)
        except Exception as error:
            _fail_local(state, task_plan.task_id, error, plan_file)
            continue
        _set_role(
            state, task_plan.task_id, "reviewer", status="running", plan_path=plan_file
        )
        _increment_role_call(state, task_plan.task_id, "reviewer", plan_file)
        try:
            response = provider.review_markdown(context.dossier_json, candidate_raw)
        except Exception as error:
            _fail_reviewer(state, task_plan.task_id, _safe_summary(error), plan_file)
            continue
        _write_text_atomic(
            context.bundle_dir / "reviewer" / "response.md",
            response,
        )
        try:
            reviewed_path = _persist_note(
                context,
                raw_markdown=response,
                directory="reviewed",
                status="model_reviewed",
            )
        except Exception as error:
            _fail_local(state, task_plan.task_id, error, plan_file)
            continue
        _set_role(
            state,
            task_plan.task_id,
            "reviewer",
            status="completed",
            output_path=_relative_to_task(context, reviewed_path),
            plan_path=plan_file,
        )
        _replace_task(
            state,
            task_plan.task_id,
            _task_state(state, task_plan.task_id).model_copy(
                update={
                    "status": "model_reviewed",
                    "reviewed_path": _relative_to_task(context, reviewed_path),
                    "safe_summary": None,
                }
            ),
            plan_file,
        )
    return state


def recover_assisted_plan(
    plan_path: str | Path, output_root: str | Path
) -> AssistedPlanState:
    """Repair only persisted local files; never invoke a provider."""
    plan_file = _plan_json_path(plan_path)
    plan = load_assisted_plan(plan_file)
    state = load_assisted_state(plan_file)
    root = Path(output_root).resolve()
    for task_plan in plan.tasks:
        current = _task_state(state, task_plan.task_id)
        try:
            context = _load_context(root, plan, task_plan)
            if current.writer.status == "running":
                _recover_writer_response(state, context, plan_file)
                current = _task_state(state, task_plan.task_id)
            if current.reviewer.status == "running":
                _recover_reviewer_response(state, context, plan_file)
                current = _task_state(state, task_plan.task_id)
            if (
                current.writer.status == "completed"
                and not (context.bundle_dir / "candidate" / "note.md").is_file()
            ):
                raw = (context.bundle_dir / "writer" / "response.md").read_text(
                    encoding="utf-8"
                )
                _persist_note(
                    context,
                    raw_markdown=raw,
                    directory="candidate",
                    status="draft_ready",
                )
            if (
                current.reviewer.status == "completed"
                and not (context.bundle_dir / "reviewed" / "note.md").is_file()
            ):
                raw = (context.bundle_dir / "reviewer" / "response.md").read_text(
                    encoding="utf-8"
                )
                _persist_note(
                    context,
                    raw_markdown=raw,
                    directory="reviewed",
                    status="model_reviewed",
                )
        except Exception as error:
            _fail_local(state, task_plan.task_id, error, plan_file)
    return state


def _load_context(
    root: Path, plan: AssistedExecutionPlan, task_plan: AssistedTaskPlan
) -> _TaskContext:
    found = find_task_by_id(root, task_plan.task_id)
    if found is None:
        raise ValueError("planned task is missing")
    task_dir, task = found
    if task_dir.resolve().relative_to(root) != Path(task_plan.task_dir):
        raise ValueError("planned task directory changed")
    if task.source_fingerprint != task_plan.source_fingerprint:
        raise ValueError("planned task source changed")
    content_pack_bytes = _content_pack_path(task_dir, task).read_bytes()
    if _sha256_bytes(content_pack_bytes) != task_plan.content_pack_sha256:
        raise ValueError("planned content pack changed")
    content_pack = ContentPack.model_validate_json(content_pack_bytes)
    dossier_json = build_reader_dossier(task, content_pack)
    if _sha256_text(dossier_json) != task_plan.dossier_sha256:
        raise ValueError("planned reader dossier changed")
    bundle_dir = task_dir / "assisted-draft" / plan.plan_id
    persisted_dossier = bundle_dir / "dossier.json"
    if (
        not persisted_dossier.is_file()
        or persisted_dossier.read_text(encoding="utf-8") != dossier_json
    ):
        raise ValueError("persisted reader dossier changed")
    return _TaskContext(root, plan, task_plan, task_dir, task, dossier_json, bundle_dir)


def _persist_note(
    context: _TaskContext, *, raw_markdown: str, directory: str, status: str
) -> Path:
    body = _normalized_markdown(raw_markdown)
    note_dir = context.bundle_dir / directory
    note_path = note_dir / "note.md"
    metadata = {
        "schema_version": "1.0",
        "route": "assisted_draft",
        "status": status,
        "plan_id": context.plan.plan_id,
        "task_id": context.task.task_id,
        "content_pack_sha256": context.task_plan.content_pack_sha256,
        "dossier_sha256": context.task_plan.dossier_sha256,
        "writer": context.plan.writer.model_dump(mode="json"),
        "reviewer": context.plan.reviewer.model_dump(mode="json"),
        "notice": "Model-reviewed Markdown is not source_valid or human-reviewed.",
    }
    _write_json_atomic(note_dir / "metadata.json", metadata)
    _write_text_atomic(
        note_path,
        "<!-- LearnNest: assisted_draft; model_reviewed is not source_valid or human-reviewed. -->\n\n"
        + body,
    )
    return note_path


def _recover_writer_response(
    state: AssistedPlanState, context: _TaskContext, plan_path: Path
) -> None:
    current = _task_state(state, context.task_plan.task_id)
    response_path = context.bundle_dir / "writer" / "response.md"
    if current.writer.actual_call_count < 1 or not response_path.is_file():
        _fail_writer(
            state,
            context.task_plan.task_id,
            "Writer call was interrupted before a persisted response",
            plan_path,
        )
        return
    candidate_path = _persist_note(
        context,
        raw_markdown=response_path.read_text(encoding="utf-8"),
        directory="candidate",
        status="draft_ready",
    )
    _set_role(
        state,
        context.task_plan.task_id,
        "writer",
        status="completed",
        output_path=_relative_to_task(context, candidate_path),
        plan_path=plan_path,
    )
    _replace_task(
        state,
        context.task_plan.task_id,
        _task_state(state, context.task_plan.task_id).model_copy(
            update={
                "status": "draft_ready",
                "candidate_path": _relative_to_task(context, candidate_path),
                "safe_summary": None,
            }
        ),
        plan_path,
    )


def _recover_reviewer_response(
    state: AssistedPlanState, context: _TaskContext, plan_path: Path
) -> None:
    current = _task_state(state, context.task_plan.task_id)
    response_path = context.bundle_dir / "reviewer" / "response.md"
    if current.reviewer.actual_call_count < 1 or not response_path.is_file():
        _fail_reviewer(
            state,
            context.task_plan.task_id,
            "Reviewer call was interrupted before a persisted response",
            plan_path,
        )
        return
    reviewed_path = _persist_note(
        context,
        raw_markdown=response_path.read_text(encoding="utf-8"),
        directory="reviewed",
        status="model_reviewed",
    )
    _set_role(
        state,
        context.task_plan.task_id,
        "reviewer",
        status="completed",
        output_path=_relative_to_task(context, reviewed_path),
        plan_path=plan_path,
    )
    _replace_task(
        state,
        context.task_plan.task_id,
        _task_state(state, context.task_plan.task_id).model_copy(
            update={
                "status": "model_reviewed",
                "reviewed_path": _relative_to_task(context, reviewed_path),
                "safe_summary": None,
            }
        ),
        plan_path,
    )


def _initial_state(plan: AssistedExecutionPlan) -> AssistedPlanState:
    return AssistedPlanState(
        plan_id=plan.plan_id,
        tasks=[
            AssistedTaskState(
                task_id=item.task_id,
                writer=AssistedRoleState(role="writer", max_calls=item.max_calls // 2),
                reviewer=AssistedRoleState(
                    role="reviewer", max_calls=item.max_calls // 2
                ),
            )
            for item in plan.tasks
        ],
    )


def _task_state(state: AssistedPlanState, task_id: str) -> AssistedTaskState:
    for task in state.tasks:
        if task.task_id == task_id:
            return task
    raise ValueError("assisted plan state has no planned task")


def _replace_task(
    state: AssistedPlanState,
    task_id: str,
    task_state: AssistedTaskState,
    plan_path: Path,
) -> None:
    state.tasks[:] = [
        task_state if task.task_id == task_id else task for task in state.tasks
    ]
    _save_state(plan_path, state)


def _set_role(
    state: AssistedPlanState,
    task_id: str,
    role: str,
    *,
    status: str,
    plan_path: Path,
    output_path: str | None = None,
    safe_summary: str | None = None,
) -> None:
    task = _task_state(state, task_id)
    current = getattr(task, role)
    updated = current.model_copy(
        update={
            "status": status,
            "output_path": output_path,
            "safe_summary": safe_summary,
        }
    )
    _replace_task(state, task_id, task.model_copy(update={role: updated}), plan_path)


def _increment_role_call(
    state: AssistedPlanState, task_id: str, role: str, plan_path: Path
) -> None:
    task = _task_state(state, task_id)
    current = getattr(task, role)
    _replace_task(
        state,
        task_id,
        task.model_copy(
            update={
                role: current.model_copy(
                    update={"actual_call_count": current.actual_call_count + 1}
                )
            }
        ),
        plan_path,
    )


def _fail_writer(
    state: AssistedPlanState, task_id: str, summary: str, plan_path: Path
) -> None:
    _set_role(
        state,
        task_id,
        "writer",
        status="failed",
        safe_summary=summary,
        plan_path=plan_path,
    )
    _replace_task(
        state,
        task_id,
        _task_state(state, task_id).model_copy(
            update={"status": "writer_failed", "safe_summary": summary}
        ),
        plan_path,
    )


def _fail_reviewer(
    state: AssistedPlanState, task_id: str, summary: str, plan_path: Path
) -> None:
    _set_role(
        state,
        task_id,
        "reviewer",
        status="failed",
        safe_summary=summary,
        plan_path=plan_path,
    )
    _replace_task(
        state,
        task_id,
        _task_state(state, task_id).model_copy(
            update={"status": "review_failed", "safe_summary": summary}
        ),
        plan_path,
    )


def _fail_local(
    state: AssistedPlanState, task_id: str, error: Exception, plan_path: Path
) -> None:
    summary = _safe_summary(error)
    _replace_task(
        state,
        task_id,
        _task_state(state, task_id).model_copy(
            update={"status": "local_recovery_failed", "safe_summary": summary}
        ),
        plan_path,
    )


def _assert_provider_matches(
    provider: AssistedNoteProvider, snapshot: AssistedConnectionSnapshot
) -> None:
    if (
        provider.name != snapshot.provider
        or provider.model != snapshot.model
        or provider.endpoint_identity != snapshot.endpoint_identity
    ):
        raise ValueError("assisted provider does not match the immutable plan")


def _content_pack_path(task_dir: Path, task: TaskRecord) -> Path:
    paths = [
        task_dir / path
        for path in task.artifacts.get("content_pack", [])
        if Path(path).name == "content_pack.json"
    ]
    if len(paths) != 1:
        raise ValueError("task has no unique content_pack.json artifact")
    path = paths[0].resolve()
    if not path.is_relative_to(task_dir.resolve()) or not path.is_file():
        raise ValueError("content_pack.json artifact is missing or outside task")
    return path


def _assert_task_matches_pack(task: TaskRecord, content_pack: ContentPack) -> None:
    if (
        task.task_id != content_pack.task_id
        or task.source_fingerprint != content_pack.source_fingerprint
    ):
        raise ValueError("content pack does not match task identity")


def _relative_to_task(context: _TaskContext, path: Path) -> str:
    return path.resolve().relative_to(context.task_dir.resolve()).as_posix()


def _normalized_markdown(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("provider returned empty or unsafe Markdown")
    return value.strip() + "\n"


def _plan_json_path(path: str | Path) -> Path:
    candidate = Path(path)
    return (candidate / "plan.json" if candidate.is_dir() else candidate).resolve()


def _plan_dir(root: Path, plan_id: str) -> Path:
    return root / ".learnnest" / "assisted-draft" / "plans" / plan_id


def _state_path(plan_path: Path) -> Path:
    return plan_path.parent / "state.json"


def _save_state(plan_path: Path, state: AssistedPlanState) -> None:
    _write_json_atomic(_state_path(plan_path), state.model_dump(mode="json"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _safe_summary(error: Exception) -> str:
    message = " ".join(str(error).split())
    if not message or _SENSITIVE.search(message):
        return type(error).__name__
    return f"{type(error).__name__}: {message}"[:500]


def _write_json_atomic(path: Path, payload: object) -> None:
    _write_text_atomic(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
