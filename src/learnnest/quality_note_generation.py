"""Explicit, persisted execution for the quality-first note workflow."""

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

from pydantic import ValidationError

from learnnest.evidence_organization import (
    build_organization_from_responses,
    canonical_organization_json,
    canonical_writer_organization_json,
    organization_sha256,
)
from learnnest.evidence_unit_models import (
    EvidenceUnitOrganization,
    EvidenceUnitOrganizationResponse,
    EvidenceUnitShard,
)
from learnnest.evidence_units import (
    build_evidence_atoms,
    canonical_shard_json,
    plan_evidence_shards,
)
from learnnest.models import ContentPack, StageStatus, TaskRecord
from learnnest.note_models import (
    QualityNoteEnvelope,
    QualityReviewerResponse,
    ReaderDraft,
)
from learnnest.note_quality import analyze_quality
from learnnest.quality_execution_models import (
    QualityExecutionPlan,
    QualityFailureCode,
    QualityFailurePhase,
    QualityPlanState,
    QualityRetryability,
    QualityRole,
    QualityRoleState,
    QualityTaskPlan,
    QualityTaskState,
)
from learnnest.quality_note import (
    build_quality_note,
    build_quality_note_provenance,
    render_quality_note,
)
from learnnest.reader_templates import (
    ReaderTemplateManifest,
    parse_reader_template,
    reader_template_snapshot_json,
    reader_template_snapshot_sha256,
)
from learnnest.task_store import find_task_by_id, load_task

_SAFE_FILE_PART = re.compile(r"[^A-Za-z0-9._-]+")
_SENSITIVE = re.compile(
    r"(?i)(authorization\s*:|bearer\s+|api[_ -]?key\s*[:=]|cookie\s*:|\b(?:sk|tp)-[A-Za-z0-9_-]{8,})"
)


class QualityNoteProvider(Protocol):
    """One provider object with separately invoked quality roles."""

    name: str
    model: str

    def organize(self, shard_json: str) -> str: ...

    def write(self, organization_json: str, template_json: str) -> str: ...

    def review(self, note_json: str, organization_json: str) -> str: ...


class QualityNoteGenerationError(RuntimeError):
    """A persisted quality-first execution failure."""


@dataclass(frozen=True)
class _TaskContext:
    root: Path
    plan: QualityExecutionPlan
    task_plan: QualityTaskPlan
    task_dir: Path
    task: TaskRecord
    content_pack: ContentPack
    content_pack_bytes: bytes
    atoms: list
    shards: list[EvidenceUnitShard]
    template: ReaderTemplateManifest
    bundle_dir: Path


def create_quality_plan(
    output_root: str | Path,
    task_ids: list[str],
    *,
    template: ReaderTemplateManifest,
    organizer_provider: str,
    organizer_model: str,
    writer_provider: str,
    writer_model: str,
    reviewer_provider: str | None = None,
    reviewer_model: str | None = None,
    review_mode: str = "none",
    shard_size: int = 80,
    reuse_organization_path: str | Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Create an immutable plan without constructing or calling a provider."""
    root = Path(output_root).resolve()
    if not task_ids:
        raise ValueError("quality plan requires at least one task ID")
    if review_mode not in {"none", "report", "gate"}:
        raise ValueError("quality review mode must be none, report, or gate")
    if shard_size < 1:
        raise ValueError("quality shard size must be at least 1")
    if reuse_organization_path is not None and len(task_ids) != 1:
        raise ValueError("organization reuse requires exactly one task")
    created_at = now or datetime.now(UTC)
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("quality plan time must be timezone-aware")
    template_sha = reader_template_snapshot_sha256(template)
    task_plans: list[QualityTaskPlan] = []
    reused_organizations: dict[str, EvidenceUnitOrganization] = {}

    for task_id in task_ids:
        found = find_task_by_id(root, task_id)
        if found is None:
            raise ValueError(f"task not found: {task_id}")
        task_dir, task = found
        if task.stages.get("content_pack") is not StageStatus.COMPLETED:
            raise ValueError(f"task has no completed content pack: {task_id}")
        content_pack_path = _content_pack_path(task_dir, task)
        content_pack_bytes = content_pack_path.read_bytes()
        content_pack = ContentPack.model_validate_json(content_pack_bytes)
        _assert_task_matches_pack(task, content_pack)
        atoms = build_evidence_atoms(content_pack)
        if not atoms:
            raise ValueError(f"content pack has no source evidence atoms: {task_id}")
        shards = plan_evidence_shards(atoms, max_atoms=shard_size)
        shard_plans = [_shard_plan(shard, atoms) for shard in shards]
        organization_input = json.dumps(
            [json.loads(canonical_shard_json(shard, atoms)) for shard in shards],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        relative_task_dir = task_dir.resolve().relative_to(root).as_posix()
        reused_path: str | None = None
        reused_sha: str | None = None
        if reuse_organization_path is not None:
            source_path = Path(reuse_organization_path).resolve()
            if not source_path.is_relative_to(root) or not source_path.is_file():
                raise ValueError(
                    "reused organization must be a file inside output root"
                )
            try:
                reused_organization = EvidenceUnitOrganization.model_validate_json(
                    source_path.read_bytes()
                )
            except (OSError, UnicodeError, ValueError) as error:
                raise ValueError("reused organization is invalid") from error
            if (
                reused_organization.task_id != task.task_id
                or reused_organization.source_fingerprint != task.source_fingerprint
                or reused_organization.content_pack_sha256
                != _sha256_bytes(content_pack_bytes)
            ):
                raise ValueError(
                    "reused organization is bound to different source data"
                )
            reused_path = source_path.relative_to(root).as_posix()
            reused_sha = organization_sha256(reused_organization)
            reused_organizations[task_id] = reused_organization
        task_plans.append(
            QualityTaskPlan(
                task_id=task_id,
                task_dir=relative_task_dir,
                content_pack_sha256=_sha256_bytes(content_pack_bytes),
                organization_input_sha256=_sha256_text(organization_input),
                template_snapshot=template.model_dump(mode="json"),
                template_sha256=template_sha,
                shard_size=shard_size,
                shards=shard_plans,
                organizer_provider=organizer_provider,
                organizer_model=organizer_model,
                reused_organization_path=reused_path,
                reused_organization_sha256=reused_sha,
                writer_provider=writer_provider,
                writer_model=writer_model,
                reviewer_provider=reviewer_provider,
                reviewer_model=reviewer_model,
                review_mode=review_mode,
                max_calls=(
                    (0 if reused_sha is not None else len(shards))
                    + 1
                    + (1 if review_mode != "none" else 0)
                ),
            )
        )

    plan_digest = _sha256_text(
        json.dumps(
            [task.model_dump(mode="json") for task in task_plans],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )[:12]
    plan_id = f"quality-{created_at.astimezone(UTC):%Y%m%dT%H%M%SZ}-{plan_digest}"
    plan = QualityExecutionPlan(
        plan_id=plan_id,
        created_at=created_at.astimezone(UTC),
        tasks=task_plans,
        total_max_calls=sum(task.max_calls for task in task_plans),
    )
    plan_dir = _plan_dir(root, plan.plan_id)
    if plan_dir.exists():
        raise FileExistsError(f"quality plan already exists: {plan.plan_id}")
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "plan.json"
    _write_json_atomic(plan_path, plan.model_dump(mode="json"))
    for task_plan in plan.tasks:
        reused_organization = reused_organizations.get(task_plan.task_id)
        if reused_organization is None:
            continue
        task_bundle_dir = (
            root / Path(task_plan.task_dir) / "quality-first" / plan.plan_id
        )
        _write_json_atomic(
            task_bundle_dir / "organization.json",
            reused_organization.model_dump(mode="json"),
        )
        _write_json_atomic(
            task_bundle_dir / "organization-reuse.json",
            {
                "schema_version": "1.0",
                "source_path": task_plan.reused_organization_path,
                "organization_sha256": task_plan.reused_organization_sha256,
            },
        )
    _write_json_atomic(
        _state_path(plan_path), _initial_state(plan).model_dump(mode="json")
    )
    return plan_path


def load_quality_plan(path: str | Path) -> QualityExecutionPlan:
    plan_path = _plan_json_path(path)
    try:
        return QualityExecutionPlan.model_validate_json(plan_path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("quality plan is missing or invalid") from error


def load_quality_state(path: str | Path) -> QualityPlanState:
    plan_path = _plan_json_path(path)
    plan = load_quality_plan(plan_path)
    state_path = _state_path(plan_path)
    if not state_path.is_file():
        return _initial_state(plan)
    try:
        state = QualityPlanState.model_validate_json(state_path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("quality plan state is missing or invalid") from error
    if state.plan_id != plan.plan_id:
        raise ValueError("quality plan state does not match plan")
    return state


def organize_quality_plan(
    plan_path: str | Path,
    output_root: str | Path,
    provider: QualityNoteProvider,
) -> QualityPlanState:
    """Execute only missing Organizer shards within the frozen call budget."""
    plan_file = _plan_json_path(plan_path)
    plan = load_quality_plan(plan_file)
    root = Path(output_root).resolve()
    state = load_quality_state(plan_file)
    for task_plan in plan.tasks:
        task_state = _task_state(state, task_plan.task_id)
        if task_state.organizer.status in {"completed", "failed"}:
            continue
        context = _load_context(root, plan, task_plan)
        if not _provider_matches(
            provider, task_plan.organizer_provider, task_plan.organizer_model
        ):
            raise ValueError("organizer provider does not match the immutable plan")
        context.bundle_dir.mkdir(parents=True, exist_ok=True)
        organizer_dir = context.bundle_dir / "organizer"
        organizer_dir.mkdir(parents=True, exist_ok=True)
        responses: list[tuple[EvidenceUnitShard, EvidenceUnitOrganizationResponse]] = []
        failed = False
        for shard, shard_plan in zip(context.shards, task_plan.shards, strict=True):
            response_path = organizer_dir / f"{shard.shard_id}.json"
            if response_path.is_file():
                responses.append((shard, _load_organizer_response(response_path)))
                continue
            if not _reserve_call(state, task_plan.task_id, "organizer", plan_file):
                failed = True
                break
            try:
                raw_response = provider.organize(
                    canonical_shard_json(shard, context.atoms)
                )
            except Exception as error:
                _fail_role(
                    state,
                    task_plan.task_id,
                    "organizer",
                    phase="organization",
                    code="provider_error",
                    retryability="requires_new_plan",
                    error=error,
                    plan_path=plan_file,
                )
                failed = True
                break
            _increment_call(state, task_plan.task_id, "organizer", plan_file)
            try:
                response = EvidenceUnitOrganizationResponse.model_validate_json(
                    raw_response
                )
            except (TypeError, ValueError, ValidationError) as error:
                _fail_role(
                    state,
                    task_plan.task_id,
                    "organizer",
                    phase="organization",
                    code="invalid_response",
                    retryability="requires_new_plan",
                    error=error,
                    plan_path=plan_file,
                )
                failed = True
                break
            _write_json_atomic(response_path, response.model_dump(mode="json"))
            responses.append((shard, response))
            _set_role_pending(state, task_plan.task_id, "organizer", plan_file)
        if failed:
            continue
        if len(responses) != len(context.shards):
            continue
        try:
            organization = build_organization_from_responses(
                context.content_pack,
                [
                    (
                        shard,
                        _load_organizer_response(
                            organizer_dir / f"{shard.shard_id}.json"
                        ),
                    )
                    for shard in context.shards
                ],
                content_pack_sha256=task_plan.content_pack_sha256,
            )
            _persist_organization(context, organization)
            _write_json_atomic(
                context.bundle_dir / "organization-run.json",
                {
                    "schema_version": "1.0",
                    "provider": task_plan.organizer_provider,
                    "model": task_plan.organizer_model,
                    "planned_call_count": len(context.shards),
                    "actual_call_count": _role_state(
                        state, task_plan.task_id, "organizer"
                    ).actual_call_count,
                    "content_pack_sha256": task_plan.content_pack_sha256,
                    "organization_sha256": organization_sha256(organization),
                    "normalizations": organization.normalizations,
                },
            )
        except Exception as error:
            _fail_role(
                state,
                task_plan.task_id,
                "organizer",
                phase="source_validation",
                code=_source_failure_code(error),
                retryability="requires_new_plan",
                error=error,
                plan_path=plan_file,
            )
            continue
        _complete_role(
            state,
            task_plan.task_id,
            "organizer",
            output_path=_relative_to_task(
                context, context.bundle_dir / "organization.json"
            ),
            plan_path=plan_file,
        )
        _replace_task(
            state,
            task_plan.task_id,
            task_state=_task_state(state, task_plan.task_id).model_copy(
                update={"status": "organized"}
            ),
            plan_path=plan_file,
        )
    return state


def generate_quality_note_plan(
    plan_path: str | Path,
    output_root: str | Path,
    provider: QualityNoteProvider,
) -> QualityPlanState:
    """Run exactly one Writer call per planned task after organization succeeds."""
    plan_file = _plan_json_path(plan_path)
    plan = load_quality_plan(plan_file)
    root = Path(output_root).resolve()
    state = load_quality_state(plan_file)
    for task_plan in plan.tasks:
        task_state = _task_state(state, task_plan.task_id)
        if task_state.writer.status in {"completed", "failed"}:
            continue
        if task_state.organizer.status != "completed":
            _fail_task(
                state,
                task_plan.task_id,
                phase="input",
                code="input_missing",
                retryability="local_recovery",
                summary="organization bundle is not complete",
                plan_path=plan_file,
            )
            continue
        context = _load_context(root, plan, task_plan)
        organization = _load_organization(context)
        if not _provider_matches(
            provider, task_plan.writer_provider, task_plan.writer_model
        ):
            raise ValueError("writer provider does not match the immutable plan")
        if not _reserve_call(state, task_plan.task_id, "writer", plan_file):
            continue
        try:
            raw_draft = provider.write(
                canonical_writer_organization_json(organization),
                reader_template_snapshot_json(context.template),
            )
        except Exception as error:
            _fail_role(
                state,
                task_plan.task_id,
                "writer",
                phase="writing",
                code="provider_error",
                retryability="requires_new_plan",
                error=error,
                plan_path=plan_file,
            )
            continue
        _increment_call(state, task_plan.task_id, "writer", plan_file)
        writer_response_path = context.bundle_dir / "writer" / "response.json"
        _write_text_atomic(
            writer_response_path,
            raw_draft if raw_draft.endswith("\n") else raw_draft + "\n",
        )
        try:
            _persist_writer_candidate_from_response(context, raw_draft)
        except (TypeError, ValueError, ValidationError) as error:
            _fail_role(
                state,
                task_plan.task_id,
                "writer",
                phase="source_validation"
                if isinstance(error, ValueError)
                else "writing",
                code=_source_failure_code(error),
                retryability="local_recovery",
                error=error,
                plan_path=plan_file,
            )
            continue
        _complete_role(
            state,
            task_plan.task_id,
            "writer",
            output_path=_relative_to_task(
                context, context.bundle_dir / "candidate" / "note.json"
            ),
            plan_path=plan_file,
        )
        _replace_task(
            state,
            task_plan.task_id,
            task_state=_task_state(state, task_plan.task_id).model_copy(
                update={
                    "status": "source_valid",
                    "candidate_path": _relative_to_task(
                        context, context.bundle_dir / "candidate" / "note.json"
                    ),
                }
            ),
            plan_path=plan_file,
        )
    return state


def _persist_writer_candidate_from_response(
    context: _TaskContext, raw_draft: str
) -> None:
    organization = _load_organization(context)
    normalized_draft, response_normalizations = _normalize_reader_draft_json(
        raw_draft,
        visual_unit_aliases={
            unit.visual_anchor_id: unit.unit_id
            for unit in organization.units
            if unit.visual_anchor_id is not None
        },
    )
    draft = ReaderDraft.model_validate_json(normalized_draft)
    note = build_quality_note(
        context.task,
        context.content_pack,
        organization,
        draft,
        template=context.template,
        content_pack_sha256=context.task_plan.content_pack_sha256,
    )
    _persist_candidate(
        context,
        note,
        response_normalizations=response_normalizations,
    )


def _normalize_reader_draft_json(
    raw_draft: str,
    *,
    visual_unit_aliases: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    """Apply narrow, recorded normalizations before strict ReaderDraft validation."""
    try:
        payload = json.loads(raw_draft)
    except (TypeError, json.JSONDecodeError):
        return raw_draft, []
    if not isinstance(payload, dict):
        return raw_draft, []

    normalizations: list[str] = []

    def normalize_field(container: dict[str, object], key: str, path: str) -> None:
        value = container.get(key)
        if not isinstance(value, str) or not any(
            character in value for character in "\r\n"
        ):
            return
        normalized = re.sub(r"[ \t]*[\r\n]+[ \t]*", " ", value)
        container[key] = normalized
        normalizations.append(f"flattened ReaderDraft line breaks at {path}")

    normalize_field(payload, "title", "title")
    selected_visual_unit_ids: set[str] = set()
    sections = payload.get("sections")
    if isinstance(sections, list):
        for section_index, section in enumerate(sections):
            if not isinstance(section, dict):
                continue
            items = section.get("items")
            if not isinstance(items, list):
                continue
            for item_index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                markdown = item.get("markdown")
                if isinstance(markdown, str):
                    normalized_markdown, replacements = re.subn(
                        r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$",
                        r"**\1**",
                        markdown,
                    )
                    if replacements:
                        item["markdown"] = normalized_markdown
                        normalizations.append(
                            "demoted ReaderDraft headings at "
                            f"sections[{section_index}].items[{item_index}].markdown"
                        )
                path = f"sections[{section_index}].items[{item_index}].visual_unit_id"
                visual_unit_id = item.get("visual_unit_id")
                evidence_unit_ids = item.get("evidence_unit_ids")
                if (
                    isinstance(visual_unit_id, str)
                    and visual_unit_aliases
                    and visual_unit_id in visual_unit_aliases
                    and isinstance(evidence_unit_ids, list)
                    and visual_unit_aliases[visual_unit_id] in evidence_unit_ids
                ):
                    normalized_unit_id = visual_unit_aliases[visual_unit_id]
                    item["visual_unit_id"] = normalized_unit_id
                    normalizations.append(
                        f"normalized visual anchor {visual_unit_id} -> "
                        f"{normalized_unit_id} at {path}"
                    )
                    visual_unit_id = normalized_unit_id
                if not isinstance(visual_unit_id, str):
                    continue
                if visual_unit_id in selected_visual_unit_ids:
                    item["visual_unit_id"] = None
                    normalizations.append(
                        f"removed duplicate visual selection {visual_unit_id} at {path}"
                    )
                    continue
                if len(selected_visual_unit_ids) >= 3:
                    item["visual_unit_id"] = None
                    normalizations.append(
                        f"removed over-budget visual selection {visual_unit_id} at {path}"
                    )
                    continue
                selected_visual_unit_ids.add(visual_unit_id)
    supplements = payload.get("ai_supplements")
    if isinstance(supplements, list):
        for item_index, item in enumerate(supplements):
            if isinstance(item, dict):
                normalize_field(
                    item,
                    "text",
                    f"ai_supplements[{item_index}].text",
                )
    return json.dumps(payload, ensure_ascii=False), normalizations


def review_quality_note_plan(
    plan_path: str | Path,
    output_root: str | Path,
    provider: QualityNoteProvider | None = None,
) -> QualityPlanState:
    """Run the planned Reviewer once, then apply report/gate activation semantics."""
    plan_file = _plan_json_path(plan_path)
    plan = load_quality_plan(plan_file)
    root = Path(output_root).resolve()
    state = load_quality_state(plan_file)
    for task_plan in plan.tasks:
        task_state = _task_state(state, task_plan.task_id)
        if task_state.writer.status != "completed":
            continue
        context = _load_context(root, plan, task_plan)
        candidate_path = context.bundle_dir / "candidate" / "note.json"
        report_path = context.bundle_dir / "candidate" / "quality_report.json"
        if report_path.is_file() and task_state.status in {
            "quality_reported",
            "quality_gate_passed",
            "active",
        }:
            continue
        note = QualityNoteEnvelope.model_validate_json(candidate_path.read_bytes())
        organization = _load_organization(context)
        reviewer_response: QualityReviewerResponse | None = None
        if task_plan.review_mode == "none":
            _replace_task(
                state,
                task_plan.task_id,
                task_state=task_state.model_copy(
                    update={
                        "reviewer": task_state.reviewer.model_copy(
                            update={"status": "skipped"}
                        )
                    }
                ),
                plan_path=plan_file,
            )
        elif task_state.reviewer.status == "completed":
            reviewer_response = _load_reviewer_response(
                context.bundle_dir / "candidate" / "reviewer.json"
            )
        else:
            if provider is None:
                raise ValueError("reviewer provider is required for report or gate")
            if not _provider_matches(
                provider,
                task_plan.reviewer_provider or "",
                task_plan.reviewer_model or "",
            ):
                raise ValueError("reviewer provider does not match the immutable plan")
            if not _reserve_call(state, task_plan.task_id, "reviewer", plan_file):
                continue
            try:
                raw_review = provider.review(
                    note.model_dump_json(),
                    canonical_organization_json(organization),
                )
            except Exception as error:
                _fail_role(
                    state,
                    task_plan.task_id,
                    "reviewer",
                    phase="quality",
                    code="provider_error",
                    retryability="requires_new_plan",
                    error=error,
                    plan_path=plan_file,
                )
                continue
            _increment_call(state, task_plan.task_id, "reviewer", plan_file)
            try:
                reviewer_response = QualityReviewerResponse.model_validate_json(
                    raw_review
                )
            except (TypeError, ValueError, ValidationError) as error:
                _fail_role(
                    state,
                    task_plan.task_id,
                    "reviewer",
                    phase="quality",
                    code="invalid_response",
                    retryability="requires_new_plan",
                    error=error,
                    plan_path=plan_file,
                )
                continue
            _write_json_atomic(
                context.bundle_dir / "candidate" / "reviewer.json",
                reviewer_response.model_dump(mode="json"),
            )
            _complete_role(
                state,
                task_plan.task_id,
                "reviewer",
                output_path=_relative_to_task(
                    context, context.bundle_dir / "candidate" / "reviewer.json"
                ),
                plan_path=plan_file,
            )
        report = analyze_quality(
            note,
            organization,
            context.content_pack,
            reviewer=reviewer_response,
        )
        _write_json_atomic(report_path, report.model_dump(mode="json"))
        _replace_task(
            state,
            task_plan.task_id,
            task_state=_task_state(state, task_plan.task_id).model_copy(
                update={
                    "status": "quality_reported",
                    "quality_report_path": _relative_to_task(context, report_path),
                }
            ),
            plan_path=plan_file,
        )
        current = _task_state(state, task_plan.task_id)
        if task_plan.review_mode == "gate":
            if report.status == "passed":
                _replace_task(
                    state,
                    task_plan.task_id,
                    task_state=current.model_copy(
                        update={
                            "status": "quality_gate_passed",
                            "activation_decision": "activate_quality_note",
                        }
                    ),
                    plan_path=plan_file,
                )
                _activate_for_task(state, context, task_plan, plan_file)
            else:
                _replace_task(
                    state,
                    task_plan.task_id,
                    task_state=current.model_copy(
                        update={"activation_decision": "retain_previous_active"}
                    ),
                    plan_path=plan_file,
                )
        elif task_plan.review_mode == "report" or report.status == "passed":
            _activate_for_task(state, context, task_plan, plan_file)
        else:
            _replace_task(
                state,
                task_plan.task_id,
                task_state=current.model_copy(
                    update={"activation_decision": "retain_previous_active"}
                ),
                plan_path=plan_file,
            )
    return state


def recover_quality_plan(
    plan_path: str | Path,
    output_root: str | Path,
) -> QualityPlanState:
    """Recover only local bundle writes/publication; never invoke a provider."""
    plan_file = _plan_json_path(plan_path)
    plan = load_quality_plan(plan_file)
    root = Path(output_root).resolve()
    state = load_quality_state(plan_file)
    for task_plan in plan.tasks:
        task_state = _task_state(state, task_plan.task_id)
        context = _load_context(root, plan, task_plan)
        interrupted_role = next(
            (
                role
                for role in ("organizer", "writer", "reviewer")
                if _role_state(state, task_plan.task_id, role).status == "running"
            ),
            None,
        )
        if interrupted_role is not None:
            _fail_role(
                state,
                task_plan.task_id,
                interrupted_role,
                phase="recovery",
                code="recovery_not_possible",
                retryability="requires_new_plan",
                error=RuntimeError(
                    f"{interrupted_role} call was interrupted before local completion"
                ),
                plan_path=plan_file,
            )
            continue
        writer_response_path = context.bundle_dir / "writer" / "response.json"
        if (
            task_state.writer.status == "failed"
            and task_state.writer.retryability == "local_recovery"
            and writer_response_path.is_file()
        ):
            try:
                _persist_writer_candidate_from_response(
                    context,
                    writer_response_path.read_text(encoding="utf-8"),
                )
            except (OSError, UnicodeError, TypeError, ValueError, ValidationError):
                continue
            _complete_role(
                state,
                task_plan.task_id,
                "writer",
                output_path=_relative_to_task(
                    context, context.bundle_dir / "candidate" / "note.json"
                ),
                plan_path=plan_file,
            )
            current = _task_state(state, task_plan.task_id)
            _replace_task(
                state,
                task_plan.task_id,
                task_state=current.model_copy(
                    update={
                        "status": "source_valid",
                        "candidate_path": _relative_to_task(
                            context,
                            context.bundle_dir / "candidate" / "note.json",
                        ),
                        "failure_phase": None,
                        "failure_code": None,
                        "retryability": None,
                        "safe_summary": None,
                    }
                ),
                plan_path=plan_file,
            )
            task_state = _task_state(state, task_plan.task_id)
        candidate = context.bundle_dir / "candidate" / "note.json"
        if not candidate.is_file():
            continue
        note = QualityNoteEnvelope.model_validate_json(candidate.read_bytes())
        organization = _load_organization(context)
        candidate_markdown_path = context.bundle_dir / "candidate" / "note.md"
        candidate_provenance_path = (
            context.bundle_dir / "candidate" / "note.provenance.json"
        )
        _write_text_atomic(
            candidate_markdown_path,
            render_quality_note(
                context.task,
                context.content_pack,
                note,
                organization,
                asset_prefix=_asset_prefix_for_output(context, candidate_markdown_path),
                provenance_path=candidate_provenance_path.name,
            ),
        )
        _write_json_atomic(
            candidate_provenance_path,
            build_quality_note_provenance(note, organization),
        )
        if task_state.status == "active":
            _activate_for_task(state, context, task_plan, plan_file)
            continue
        if task_plan.review_mode == "none" and task_state.status == "quality_reported":
            report_path = context.bundle_dir / "candidate" / "quality_report.json"
            if report_path.is_file():
                report_payload = json.loads(report_path.read_text(encoding="utf-8"))
                if report_payload.get("status") == "passed":
                    _activate_for_task(state, context, task_plan, plan_file)
        if (
            task_plan.review_mode == "report"
            and task_state.status == "quality_reported"
        ):
            _activate_for_task(state, context, task_plan, plan_file)
        if (
            task_plan.review_mode == "gate"
            and task_state.status == "quality_gate_passed"
        ):
            _activate_for_task(state, context, task_plan, plan_file)
    return state


def _load_context(
    root: Path, plan: QualityExecutionPlan, task_plan: QualityTaskPlan
) -> _TaskContext:
    task_dir = (root / Path(task_plan.task_dir)).resolve()
    if not task_dir.is_relative_to(root):
        raise ValueError("quality plan task directory escapes output root")
    task = load_task(task_dir)
    content_pack_path = _content_pack_path(task_dir, task)
    content_pack_bytes = content_pack_path.read_bytes()
    if _sha256_bytes(content_pack_bytes) != task_plan.content_pack_sha256:
        raise ValueError("content pack changed after quality plan creation")
    content_pack = ContentPack.model_validate_json(content_pack_bytes)
    _assert_task_matches_pack(task, content_pack)
    atoms = build_evidence_atoms(content_pack)
    shards = plan_evidence_shards(atoms, max_atoms=task_plan.shard_size)
    if [shard.shard_id for shard in shards] != [
        item.shard_id for item in task_plan.shards
    ]:
        raise ValueError("quality plan shard boundaries changed")
    for shard, shard_plan in zip(shards, task_plan.shards, strict=True):
        if (
            shard.atom_ids != shard_plan.atom_ids
            or _sha256_text(canonical_shard_json(shard, atoms))
            != shard_plan.input_sha256
        ):
            raise ValueError("quality plan shard input changed")
    organization_input = json.dumps(
        [json.loads(canonical_shard_json(shard, atoms)) for shard in shards],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if _sha256_text(organization_input) != task_plan.organization_input_sha256:
        raise ValueError("quality plan organization input changed")
    template = parse_reader_template(task_plan.template_snapshot)
    if reader_template_snapshot_sha256(template) != task_plan.template_sha256:
        raise ValueError("quality plan template snapshot changed")
    bundle_dir = task_dir / "quality-first" / plan.plan_id
    return _TaskContext(
        root=root,
        plan=plan,
        task_plan=task_plan,
        task_dir=task_dir,
        task=task,
        content_pack=content_pack,
        content_pack_bytes=content_pack_bytes,
        atoms=atoms,
        shards=shards,
        template=template,
        bundle_dir=bundle_dir,
    )


def _persist_organization(
    context: _TaskContext, organization: EvidenceUnitOrganization
) -> None:
    _write_json_atomic(
        context.bundle_dir / "evidence_units.json",
        {
            "schema_version": "2.0",
            "task_id": organization.task_id,
            "source_fingerprint": organization.source_fingerprint,
            "content_pack_sha256": organization.content_pack_sha256,
            "units": [unit.model_dump(mode="json") for unit in organization.units],
        },
    )
    _write_json_atomic(
        context.bundle_dir / "organization.json",
        organization.model_dump(mode="json"),
    )
    _write_json_atomic(
        context.bundle_dir / "organization.sha256.json",
        {"sha256": organization_sha256(organization)},
    )


def _load_organization(context: _TaskContext) -> EvidenceUnitOrganization:
    path = context.bundle_dir / "organization.json"
    try:
        organization = EvidenceUnitOrganization.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("organization bundle is missing or invalid") from error
    if organization.content_pack_sha256 != context.task_plan.content_pack_sha256:
        raise ValueError("organization bundle is bound to a different content pack")
    if (
        context.task_plan.reused_organization_sha256 is not None
        and organization_sha256(organization)
        != context.task_plan.reused_organization_sha256
    ):
        raise ValueError("reused organization changed after plan creation")
    return organization


def _load_reviewer_response(path: Path) -> QualityReviewerResponse:
    try:
        return QualityReviewerResponse.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, TypeError, ValueError, ValidationError) as error:
        raise ValueError("reviewer response is missing or invalid") from error


def _persist_candidate(
    context: _TaskContext,
    note: QualityNoteEnvelope,
    *,
    response_normalizations: list[str] | None = None,
) -> None:
    candidate_dir = context.bundle_dir / "candidate"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = candidate_dir / "note.md"
    provenance_path = candidate_dir / "note.provenance.json"
    organization = _load_organization(context)
    markdown = render_quality_note(
        context.task,
        context.content_pack,
        note,
        organization,
        asset_prefix=_asset_prefix_for_output(context, markdown_path),
        provenance_path=provenance_path.name,
    )
    _write_json_atomic(candidate_dir / "note.json", note.model_dump(mode="json"))
    _write_json_atomic(
        provenance_path,
        build_quality_note_provenance(note, organization),
    )
    _write_text_atomic(markdown_path, markdown)
    _write_json_atomic(
        candidate_dir / "quality-generation.json",
        {
            "schema_version": "1.0",
            "plan_id": context.plan.plan_id,
            "task_id": context.task.task_id,
            "content_pack_sha256": context.task_plan.content_pack_sha256,
            "template_sha256": context.task_plan.template_sha256,
            "status": "source_valid",
            "provider_fields": {
                "writer": {
                    "provider": context.task_plan.writer_provider,
                    "model": context.task_plan.writer_model,
                },
                "organizer": {
                    "provider": context.task_plan.organizer_provider,
                    "model": context.task_plan.organizer_model,
                },
            },
            "response_normalizations": list(response_normalizations or []),
        },
    )


def _activate_for_task(
    state: QualityPlanState,
    context: _TaskContext,
    task_plan: QualityTaskPlan,
    plan_path: Path,
) -> None:
    candidate_path = context.bundle_dir / "candidate" / "note.json"
    if not candidate_path.is_file():
        _fail_task(
            state,
            task_plan.task_id,
            phase="publication",
            code="publication_recovery_failed",
            retryability="local_recovery",
            summary="source-valid candidate note is missing",
            plan_path=plan_path,
        )
        return
    safe_task_id = _safe_file_part(context.task.task_id)
    active_dir = plan_path.parent / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    active_path = active_dir / f"{safe_task_id}.md"
    active_provenance_path = active_dir / f"{safe_task_id}.provenance.json"
    delivery_dir = context.root / "视频学习笔记" / "quality-first"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    delivery_path = delivery_dir / f"{safe_task_id}--{context.plan.plan_id}.md"
    delivery_provenance_path = delivery_path.with_suffix(".provenance.json")
    note = QualityNoteEnvelope.model_validate_json(candidate_path.read_bytes())
    organization = _load_organization(context)
    provenance = build_quality_note_provenance(note, organization)
    _write_text_atomic(
        active_path,
        render_quality_note(
            context.task,
            context.content_pack,
            note,
            organization,
            asset_prefix=_asset_prefix_for_output(context, active_path),
            provenance_path=active_provenance_path.name,
        ),
    )
    _write_json_atomic(active_provenance_path, provenance)
    _write_text_atomic(
        delivery_path,
        render_quality_note(
            context.task,
            context.content_pack,
            note,
            organization,
            asset_prefix=_asset_prefix_for_output(context, delivery_path),
            provenance_path=delivery_provenance_path.name,
        ),
    )
    _write_json_atomic(delivery_provenance_path, provenance)
    _write_json_atomic(
        context.bundle_dir / "candidate" / "active.json",
        {
            "schema_version": "1.0",
            "task_id": context.task.task_id,
            "plan_id": context.plan.plan_id,
            "active_path": delivery_path.relative_to(context.root).as_posix(),
        },
    )
    current = _task_state(state, task_plan.task_id)
    _replace_task(
        state,
        task_plan.task_id,
        task_state=current.model_copy(
            update={
                "status": "active",
                "activation_decision": "activate_quality_note",
            }
        ),
        plan_path=plan_path,
    )


def _initial_state(plan: QualityExecutionPlan) -> QualityPlanState:
    tasks: list[QualityTaskState] = []
    for task in plan.tasks:
        reused = task.reused_organization_sha256 is not None
        tasks.append(
            QualityTaskState(
                task_id=task.task_id,
                status="organized" if reused else "planned",
                organizer=QualityRoleState(
                    role="organizer",
                    max_calls=0 if reused else len(task.shards),
                    actual_call_count=0,
                    status="completed" if reused else "pending",
                    output_path=(
                        f"quality-first/{plan.plan_id}/organization.json"
                        if reused
                        else None
                    ),
                ),
                writer=QualityRoleState(
                    role="writer",
                    max_calls=1,
                    actual_call_count=0,
                    status="pending",
                ),
                reviewer=QualityRoleState(
                    role="reviewer",
                    max_calls=1 if task.review_mode != "none" else 0,
                    actual_call_count=0,
                    status="pending" if task.review_mode != "none" else "skipped",
                ),
            )
        )
    return QualityPlanState(plan_id=plan.plan_id, tasks=tasks)


def _task_state(state: QualityPlanState, task_id: str) -> QualityTaskState:
    for task in state.tasks:
        if task.task_id == task_id:
            return task
    raise ValueError(f"quality plan state has no task: {task_id}")


def _replace_task(
    state: QualityPlanState,
    task_id: str,
    *,
    task_state: QualityTaskState,
    plan_path: Path,
) -> None:
    state.tasks[:] = [
        task_state if task.task_id == task_id else task for task in state.tasks
    ]
    _save_state(plan_path, state)


def _role_state(
    state: QualityPlanState, task_id: str, role: QualityRole
) -> QualityRoleState:
    return getattr(_task_state(state, task_id), role)


def _replace_role(
    state: QualityPlanState,
    task_id: str,
    role: QualityRole,
    *,
    role_state: QualityRoleState,
    plan_path: Path,
) -> None:
    task_state = _task_state(state, task_id)
    _replace_task(
        state,
        task_id,
        task_state=task_state.model_copy(update={role: role_state}),
        plan_path=plan_path,
    )


def _reserve_call(
    state: QualityPlanState,
    task_id: str,
    role: QualityRole,
    plan_path: Path,
) -> bool:
    current = _role_state(state, task_id, role)
    if current.status in {"completed", "failed", "skipped"}:
        return False
    if current.status == "running":
        _fail_role(
            state,
            task_id,
            role,
            phase="recovery",
            code="recovery_not_possible",
            retryability="requires_new_plan",
            error=RuntimeError("quality provider call was interrupted"),
            plan_path=plan_path,
        )
        return False
    if current.actual_call_count >= current.max_calls:
        _fail_role(
            state,
            task_id,
            role,
            phase="input",
            code="call_budget_exhausted",
            retryability="requires_new_plan",
            error=RuntimeError("quality role call budget is exhausted"),
            plan_path=plan_path,
        )
        return False
    _replace_role(
        state,
        task_id,
        role,
        role_state=current.model_copy(update={"status": "running"}),
        plan_path=plan_path,
    )
    return True


def _increment_call(
    state: QualityPlanState, task_id: str, role: QualityRole, plan_path: Path
) -> None:
    current = _role_state(state, task_id, role)
    _replace_role(
        state,
        task_id,
        role,
        role_state=current.model_copy(
            update={"actual_call_count": current.actual_call_count + 1}
        ),
        plan_path=plan_path,
    )


def _set_role_pending(
    state: QualityPlanState, task_id: str, role: QualityRole, plan_path: Path
) -> None:
    current = _role_state(state, task_id, role)
    _replace_role(
        state,
        task_id,
        role,
        role_state=current.model_copy(update={"status": "pending"}),
        plan_path=plan_path,
    )


def _complete_role(
    state: QualityPlanState,
    task_id: str,
    role: QualityRole,
    *,
    output_path: str,
    plan_path: Path,
) -> None:
    current = _role_state(state, task_id, role)
    _replace_role(
        state,
        task_id,
        role,
        role_state=current.model_copy(
            update={
                "status": "completed",
                "output_path": output_path,
                "failure_phase": None,
                "failure_code": None,
                "retryability": None,
                "safe_summary": None,
            }
        ),
        plan_path=plan_path,
    )


def _fail_role(
    state: QualityPlanState,
    task_id: str,
    role: QualityRole,
    *,
    phase: QualityFailurePhase,
    code: QualityFailureCode,
    retryability: QualityRetryability,
    error: Exception,
    plan_path: Path,
) -> None:
    current = _role_state(state, task_id, role)
    updated = current.model_copy(
        update={
            "status": "failed",
            "failure_phase": phase,
            "failure_code": code,
            "retryability": retryability,
            "safe_summary": _safe_summary(error),
        }
    )
    _replace_role(state, task_id, role, role_state=updated, plan_path=plan_path)
    _fail_task(
        state,
        task_id,
        phase=phase,
        code=code,
        retryability=retryability,
        summary=_safe_summary(error),
        plan_path=plan_path,
    )


def _fail_task(
    state: QualityPlanState,
    task_id: str,
    *,
    phase: QualityFailurePhase,
    code: QualityFailureCode,
    retryability: QualityRetryability,
    summary: str,
    plan_path: Path,
) -> None:
    current = _task_state(state, task_id)
    _replace_task(
        state,
        task_id,
        task_state=current.model_copy(
            update={
                "status": "failed",
                "failure_phase": phase,
                "failure_code": code,
                "retryability": retryability,
                "safe_summary": summary[:500],
            }
        ),
        plan_path=plan_path,
    )


def _provider_matches(
    provider: QualityNoteProvider, expected_name: str, expected_model: str
) -> bool:
    return provider.name == expected_name and provider.model == expected_model


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
    if task.task_id != content_pack.task_id:
        raise ValueError("content pack task_id does not match task.json")
    if task.source_fingerprint != content_pack.source_fingerprint:
        raise ValueError("content pack source_fingerprint does not match task.json")


def _shard_plan(shard: EvidenceUnitShard, atoms: list) -> object:
    from learnnest.quality_execution_models import QualityShardPlan

    return QualityShardPlan(
        shard_id=shard.shard_id,
        atom_ids=shard.atom_ids,
        start_ms=shard.start_ms,
        end_ms=shard.end_ms,
        input_sha256=_sha256_text(canonical_shard_json(shard, atoms)),
    )


def _load_organizer_response(path: Path) -> EvidenceUnitOrganizationResponse:
    try:
        return EvidenceUnitOrganizationResponse.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("persisted organizer response is invalid") from error


def _relative_to_task(context: _TaskContext, path: Path) -> str:
    return path.resolve().relative_to(context.task_dir.resolve()).as_posix()


def _asset_prefix_for_output(context: _TaskContext, output_path: Path) -> str:
    return Path(
        os.path.relpath(context.task_dir.resolve(), output_path.parent.resolve())
    ).as_posix()


def _plan_json_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_dir():
        candidate /= "plan.json"
    return candidate.resolve()


def _plan_dir(root: Path, plan_id: str) -> Path:
    return root / ".learnnest" / "quality-first" / "plans" / plan_id


def _state_path(plan_path: Path) -> Path:
    return plan_path.parent / "state.json"


def _save_state(plan_path: Path, state: QualityPlanState) -> None:
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


def _source_failure_code(error: Exception) -> QualityFailureCode:
    message = str(error).lower()
    if "unknown evidence id" in message:
        return "unknown_evidence_id"
    if "outside the shard" in message or "cross" in message:
        return "cross_source_reference"
    if "response" in message:
        return "invalid_response"
    return "source_validation_failed"


def _safe_file_part(value: str) -> str:
    result = _SAFE_FILE_PART.sub("-", value).strip(".-")
    return result or "task"


def _write_json_atomic(path: Path, payload: object) -> None:
    content = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    _write_text_atomic(path, content)


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
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
