"""Immutable bundle generation for structured notes."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from learnnest.citation_audit import (
    CitationAuditV12,
    parse_citation_audit_v12,
    statement_citation_packet_sha256,
    validate_citation_audit_v12,
)
from learnnest.execution import (
    begin_persisted_attempt,
    complete_persisted_attempt,
    fail_persisted_attempt,
)
from learnnest.models import ContentPack, TaskRecord
from learnnest.path_budget import assert_stage_path_budget
from learnnest.note_coverage import (
    CoveragePlan,
    parse_coverage_plan,
    validate_candidate_source_coverage_against_plan,
    validate_candidate_visuals_against_plan,
    validate_coverage_plan,
    validate_review_against_plan,
)
from learnnest.note_evidence_scope import (
    DraftEvidenceScope,
    StatementCitationPacketV12,
    build_draft_evidence_scope,
    build_statement_citation_packet,
    validate_note_citations_against_scope,
)
from learnnest.note_models import AnyGeneratedNote, GeneratedNote
from learnnest.note_providers import (
    CitationAuditor,
    NoteProvider,
    NoteProviderError,
    NoteReviewer,
    build_draft_execution_json,
)
from learnnest.note_review import (
    NoteReview,
    build_statement_manifest,
    parse_note_review,
    validate_note_review,
)
from learnnest.note_types import ConcreteNoteType, normalize_note_type
from learnnest.note_validation import (
    iter_factual_statements,
    parse_generated_note,
    validate_generated_note,
)
from learnnest.publication import (
    activate_note_bundle,
    reconcile_pending_note_publication,
)
from learnnest.rendering import render_generated_note
from learnnest.task_store import load_task
from learnnest.validation import validate_note_markdown_text

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CITATION_AUDIT_FORMAT_RETRY = ("citation audit response failed schema validation",)
_CITATION_AUDIT_LOCAL_CONTRACT_RETRY = (
    "citation audit response failed local contract validation",
)
_COVERAGE_PLAN_LOCAL_CONTRACT_RETRY = (
    "coverage plan failed local source-only consistency checks. Return a fresh "
    "complete CoveragePlan 1.1 JSON object from the supplied content pack. Copy "
    "task_id and source_fingerprint verbatim from it. Do not reuse any prior "
    "coverage plan response.",
)
_DRAFT_SCHEMA_FORMAT_REQUIREMENTS = (
    "generated note response failed schema validation. Return exactly one complete "
    "GeneratedNote 3.0 JSON object with no text before or after it. Do not return a "
    "legacy note, JSON Schema, field definitions, $schema, or other unrecognized "
    "top-level fields. classification_evidence_ids must contain at most 12 distinct "
    "evidence IDs from the supplied draft evidence scope.",
)


class NoteGenerationError(RuntimeError):
    """A failed immutable generation bundle and its stable error list."""

    def __init__(self, bundle_path: Path, errors: tuple[str, ...]) -> None:
        self.bundle_path = bundle_path
        self.errors = errors
        super().__init__(f"structured note generation failed: {'; '.join(errors)}")


@dataclass(frozen=True)
class NoteTypeSelection:
    """One resolved note-type request shared by a complete generation run."""

    requested: ConcreteNoteType | None
    source: Literal["auto", "cli", "source", "external"]


def resolve_note_type_selection(
    task: TaskRecord,
    cli_value: str | None,
) -> NoteTypeSelection:
    """Resolve explicit auto, CLI concrete, stored source, then provider auto."""
    if cli_value == "auto":
        return NoteTypeSelection(None, "auto")
    if cli_value is not None:
        return NoteTypeSelection(normalize_note_type(cli_value), "cli")
    if task.note_type_override is not None:
        return NoteTypeSelection(task.note_type_override, "source")
    return NoteTypeSelection(None, "auto")


def generate_and_activate_note(
    task_dir: Path,
    provider: NoteProvider,
    output_root: Path,
    *,
    reviewer: NoteReviewer | None = None,
    note_type: str | None = None,
) -> TaskRecord:
    """Generate an immutable bundle and activate it through two-phase publish."""
    recovered = reconcile_pending_note_publication(task_dir, output_root)
    if recovered is not None:
        return recovered
    task = load_task(task_dir)
    assert_stage_path_budget(
        task_dir,
        output_root,
        stage="note",
        task_title=task.title,
        task_id=task.task_id,
    )
    begin_persisted_attempt(
        task_dir,
        reason="retry",
        from_stage="note",
        now=datetime.now(UTC),
    )
    try:
        bundle = generate_note_bundle(
            task_dir,
            provider,
            reviewer=reviewer,
            note_type=note_type,
        )
        task = load_task(task_dir)
        activate_note_bundle(
            task_dir,
            bundle,
            task,
            output_root=output_root,
            provider=provider.name,
            model=provider.model,
        )
    except Exception as error:
        fail_persisted_attempt(
            task_dir,
            error,
            failed_stage="note",
            now=datetime.now(UTC),
        )
        raise
    return complete_persisted_attempt(task_dir, now=datetime.now(UTC))


def build_and_activate_external_note(
    task_dir: Path,
    raw_path: Path,
    output_root: Path,
    *,
    note_type: str | None = None,
) -> TaskRecord:
    """Build and activate one external Agent note without reading API settings."""
    recovered = reconcile_pending_note_publication(task_dir, output_root)
    if recovered is not None:
        return recovered
    begin_persisted_attempt(
        task_dir,
        reason="retry",
        from_stage="note",
        now=datetime.now(UTC),
    )
    try:
        bundle = build_external_note_bundle(
            task_dir,
            raw_path,
            note_type=note_type,
        )
        task = load_task(task_dir)
        activate_note_bundle(
            task_dir,
            bundle,
            task,
            output_root=output_root,
            provider="external-agent",
            model="external",
        )
    except Exception as error:
        fail_persisted_attempt(
            task_dir,
            error,
            failed_stage="note",
            now=datetime.now(UTC),
        )
        raise
    return complete_persisted_attempt(task_dir, now=datetime.now(UTC))


def rerender_and_activate_note(task_dir: Path, output_root: Path) -> TaskRecord:
    """Render the active validated note into a new bundle without a provider call."""
    recovered = reconcile_pending_note_publication(task_dir, output_root)
    if recovered is not None:
        return recovered
    context = _load_context(task_dir)
    provider = context.task.providers.get("note")
    model = context.task.models.get("note")
    if provider is None or model is None:
        raise ValueError("active note has no provider/model metadata")
    active_note_path = next(
        (
            context.task_dir / path
            for path in context.task.artifacts.get("note", [])
            if Path(path).name == "note.json"
        ),
        None,
    )
    if active_note_path is None:
        raise ValueError("task has no active note.json artifact")
    resolved = active_note_path.resolve()
    if not resolved.is_relative_to(context.task_dir) or not resolved.is_file():
        raise ValueError("active note.json artifact is missing or outside task")
    try:
        raw = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError("active note.json could not be read") from error
    try:
        parse_generated_note(raw)
    except ValidationError as error:
        raise ValueError("active note.json is invalid") from error
    note, errors = _parse_and_validate(raw, context.content_pack)
    if note is None or errors:
        raise ValueError(f"active note.json is invalid: {'; '.join(errors)}")
    selection = _active_note_type_selection(resolved, note)

    run_id = _new_run_id(context.task)
    temporary, final = _prepare_bundle(context.task_dir, run_id)
    bundle = _finish_success_bundle(
        temporary,
        final,
        context,
        run_id,
        provider,
        model,
        0,
        note,
        selection=selection,
        operation="rerender",
    )
    task = load_task(task_dir)
    return activate_note_bundle(
        task_dir,
        bundle,
        task,
        output_root=output_root,
        provider=provider,
        model=model,
        preserve_derived=True,
    )


def generate_note_bundle(
    task_dir: Path,
    provider: NoteProvider,
    *,
    reviewer: NoteReviewer | None = None,
    note_type: str | None = None,
    run_id: str | None = None,
) -> Path:
    """Plan, draft from scoped evidence, audit citations, then review one bundle."""
    context = _load_context(task_dir)
    selected_reviewer = reviewer or _provider_reviewer(provider)
    selection = resolve_note_type_selection(context.task, note_type)
    selected_run_id = run_id or _new_run_id(context.task)
    temporary, final = _prepare_bundle(context.task_dir, selected_run_id)
    content_pack_json = context.content_pack.model_dump_json()
    feedback: tuple[str, ...] = ()
    attempt_count = 0
    note: AnyGeneratedNote | None = None
    evidence_normalizations: list[dict[str, object]] = []
    frozen_plan_requirements: list[str] = []
    schema_format_requirements: tuple[str, ...] = ()

    coverage_plan, coverage_plan_json, review_metadata, plan_errors = (
        _run_coverage_plan(
            temporary,
            selected_reviewer,
            content_pack_json,
            context.content_pack,
        )
    )
    if coverage_plan is None or coverage_plan_json is None or plan_errors:
        return _finish_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            provider.name,
            provider.model,
            0,
            plan_errors or ("coverage plan is unavailable",),
            selection=selection,
            review_metadata=review_metadata,
            raise_error=True,
        )

    (
        draft_scope,
        draft_scope_json,
        draft_execution_json,
        review_metadata,
        scope_errors,
    ) = _prepare_draft_evidence_scope(
        temporary,
        context.content_pack,
        coverage_plan,
        review_metadata,
    )
    if (
        draft_scope is None
        or draft_scope_json is None
        or draft_execution_json is None
        or scope_errors
    ):
        return _finish_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            provider.name,
            provider.model,
            0,
            scope_errors or ("draft evidence scope is unavailable",),
            selection=selection,
            review_metadata=review_metadata,
            raise_error=True,
        )

    try:
        citation_auditor = _citation_auditor(selected_reviewer)
    except ValueError as error:
        capability_errors = (str(error),)
        review_metadata = {
            **review_metadata,
            "citation_audit": _citation_audit_metadata(
                selected_reviewer,
                status="unavailable",
                errors=capability_errors,
            ),
        }
        return _finish_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            provider.name,
            provider.model,
            0,
            capability_errors,
            selection=selection,
            review_metadata=review_metadata,
            raise_error=True,
        )

    for attempt in range(1, 4):
        attempt_count = attempt
        try:
            raw = provider.generate(
                draft_scope_json,
                feedback,
                draft_execution_json=draft_execution_json,
                requested_note_type=selection.requested,
            )
        except Exception as error:
            errors = (
                (str(error) if isinstance(error, NoteProviderError) else None)
                or f"provider failed: {type(error).__name__}",
            )
            return _finish_failed_bundle(
                temporary,
                final,
                context,
                selected_run_id,
                provider.name,
                provider.model,
                attempt_count,
                errors,
                selection=selection,
                evidence_normalizations=evidence_normalizations,
                review_metadata=review_metadata,
                raise_error=True,
            )
        _write_text(temporary / f"attempt-{attempt}.raw.txt", raw)
        note, errors, attempt_normalizations = _parse_provider_attempt(
            raw,
            context.content_pack,
            selection=selection,
        )
        if note is not None:
            scope_errors = (
                validate_note_citations_against_scope(note, draft_scope)
                if not errors
                else ()
            )
            errors = (
                *errors,
                *scope_errors,
                *validate_candidate_source_coverage_against_plan(note, coverage_plan),
                *validate_candidate_visuals_against_plan(note, coverage_plan),
            )
        evidence_normalizations.extend(
            {"attempt": attempt, **record} for record in attempt_normalizations
        )
        schema_failure = _is_draft_schema_validation_error(errors)
        if schema_failure:
            # This instruction is derived only from our public output contract. Keep
            # it across later semantic retries, but never retain malformed response
            # text or validator locations from a discarded candidate.
            schema_format_requirements = _DRAFT_SCHEMA_FORMAT_REQUIREMENTS
        if note is not None and not errors:
            try:
                packet = build_statement_citation_packet(note, context.content_pack)
            except Exception as error:
                packet_errors = (
                    f"statement citation packet failed: {type(error).__name__}",
                )
                review_metadata = {
                    **review_metadata,
                    "citation_audit": _citation_audit_metadata(
                        citation_auditor,
                        status="invalid",
                        errors=packet_errors,
                    ),
                }
                return _finish_failed_bundle(
                    temporary,
                    final,
                    context,
                    selected_run_id,
                    provider.name,
                    provider.model,
                    attempt_count,
                    packet_errors,
                    selection=selection,
                    note_type=_note_type(note),
                    evidence_normalizations=evidence_normalizations,
                    review_metadata=review_metadata,
                    raise_error=True,
                )
            review_metadata, citation_errors = _run_citation_audit(
                temporary,
                citation_auditor,
                packet,
                review_metadata,
            )
            if citation_errors:
                return _finish_failed_bundle(
                    temporary,
                    final,
                    context,
                    selected_run_id,
                    provider.name,
                    provider.model,
                    attempt_count,
                    citation_errors,
                    selection=selection,
                    note_type=_note_type(note),
                    evidence_normalizations=evidence_normalizations,
                    review_metadata=review_metadata,
                    raise_error=True,
                )
            review_metadata, review_errors = _run_quality_review(
                temporary,
                selected_reviewer,
                content_pack_json,
                context.content_pack,
                note,
                coverage_plan,
                coverage_plan_json,
                review_metadata,
            )
            if review_errors:
                return _finish_failed_bundle(
                    temporary,
                    final,
                    context,
                    selected_run_id,
                    provider.name,
                    provider.model,
                    attempt_count,
                    review_errors,
                    selection=selection,
                    note_type=_note_type(note),
                    evidence_normalizations=evidence_normalizations,
                    review_metadata=review_metadata,
                    raise_error=True,
                )
            return _finish_success_bundle(
                temporary,
                final,
                context,
                selected_run_id,
                provider.name,
                provider.model,
                attempt_count,
                note,
                selection=selection,
                evidence_normalizations=evidence_normalizations,
                review_metadata=review_metadata,
            )
        if note is not None:
            current_feedback = _draft_retry_feedback_errors(errors, coverage_plan)
            # Keep only instructions derived from the immutable plan. Current raw
            # errors describe the discarded candidate and must not become stale
            # requirements for later retries.
            for requirement in _coverage_plan_retry_feedback(errors, coverage_plan):
                if requirement not in frozen_plan_requirements:
                    frozen_plan_requirements.append(requirement)
            feedback = (
                *current_feedback,
                *(
                    requirement
                    for requirement in frozen_plan_requirements
                    if requirement not in current_feedback
                ),
                *(
                    requirement
                    for requirement in schema_format_requirements
                    if requirement not in current_feedback
                    and requirement not in frozen_plan_requirements
                ),
            )
        else:
            current_feedback = schema_format_requirements if schema_failure else errors
            feedback = (
                *current_feedback,
                *(
                    requirement
                    for requirement in frozen_plan_requirements
                    if requirement not in current_feedback
                ),
                *(
                    requirement
                    for requirement in schema_format_requirements
                    if requirement not in current_feedback
                    and requirement not in frozen_plan_requirements
                ),
            )
        # A second candidate gets one final repair only for frozen-plan placement or
        # coverage errors. A malformed second candidate must not consume that final
        # repair when a previous candidate already exposed a frozen-plan gate.
        if attempt == 2 and not (
            _has_retryable_frozen_plan_error(errors) or frozen_plan_requirements
        ):
            break

    return _finish_failed_bundle(
        temporary,
        final,
        context,
        selected_run_id,
        provider.name,
        provider.model,
        attempt_count,
        errors,
        selection=selection,
        note_type=_note_type(note) if note is not None else None,
        evidence_normalizations=evidence_normalizations,
        review_metadata=review_metadata,
        raise_error=True,
    )


def _coverage_plan_retry_feedback(
    errors: tuple[str, ...],
    plan: CoveragePlan,
) -> tuple[str, ...]:
    """Return deterministic repair instructions for failed frozen-plan gates."""
    feedback: list[str] = []
    for index, unit in enumerate(plan.units, start=1):
        source_error = (
            f"candidate does not cite coverage unit source evidence together: {index}"
        )
        if source_error in errors:
            feedback.append(
                "coverage plan unit "
                f"{index} requires all anchors together: "
                f"{', '.join(unit.source_evidence_ids)}"
            )
    for visual_index, visual in enumerate(plan.visuals, start=1):
        if visual.disposition == "omit":
            omission_error = (
                "candidate cites frame planned for omission: "
                f"{visual.frame_evidence_id}"
            )
            if omission_error in errors:
                feedback.append(
                    f"coverage plan visual {visual_index} must remain omitted"
                )
            continue
        if visual.unit_label is None:
            continue
        visual_errors = {
            "candidate does not cite planned visual evidence together: "
            f"{visual.frame_evidence_id}",
            "candidate does not cite a mapped unit anchor with planned visual "
            f"evidence: {visual.frame_evidence_id}",
        }
        if not visual_errors.intersection(errors):
            continue
        mapped_index = next(
            (
                index
                for index, unit in enumerate(plan.units, start=1)
                if unit.label == visual.unit_label
            ),
            None,
        )
        if mapped_index is None:
            continue
        anchors = plan.units[mapped_index - 1].source_evidence_ids
        evidence_ids = [
            visual.frame_evidence_id,
            *visual.supporting_ocr_evidence_ids,
        ]
        feedback.append(
            "coverage plan visual "
            f"{visual.frame_evidence_id} requires its frame and supporting OCR "
            "together at its first factual statement, plus one anchor from unit "
            f"{mapped_index}: {', '.join(anchors)}; required visual evidence: "
            f"{', '.join(evidence_ids)}"
        )
    return tuple(feedback)


def _draft_retry_feedback_errors(
    errors: tuple[str, ...],
    plan: CoveragePlan,
) -> tuple[str, ...]:
    """Keep omitted source IDs out of draft-provider retry feedback."""
    omission_replacements: dict[str, str] = {}
    omission_patterns: list[tuple[re.Pattern[str], str]] = []
    for visual_index, visual in enumerate(plan.visuals, start=1):
        if visual.disposition != "omit":
            continue
        omission_replacements[
            f"candidate cites frame planned for omission: {visual.frame_evidence_id}"
        ] = f"candidate cites coverage plan visual {visual_index} marked omit"
        omission_patterns.append(
            (
                re.compile(
                    rf"(?<![A-Za-z0-9_-]){re.escape(visual.frame_evidence_id)}"
                    r"(?![A-Za-z0-9_-])"
                ),
                f"coverage plan visual {visual_index}",
            )
        )

    feedback: list[str] = []
    for error in errors:
        if error.startswith("generated note cites evidence outside draft scope:"):
            feedback_error = (
                "generated note cites evidence outside draft scope; "
                "use only allowlisted evidence"
            )
        else:
            feedback_error = omission_replacements.get(error, error)
        for pattern, replacement in omission_patterns:
            feedback_error = pattern.sub(replacement, feedback_error)
        if feedback_error not in feedback:
            feedback.append(feedback_error)
    return tuple(feedback)


def _has_retryable_frozen_plan_error(errors: tuple[str, ...]) -> bool:
    """Allow one final retry only when immutable plan gates reject a candidate."""
    prefixes = (
        "candidate does not cite coverage unit source evidence together:",
        "candidate cites frame planned for omission:",
        "candidate does not cite planned visual evidence together:",
        "candidate does not cite a mapped unit anchor with planned visual evidence:",
    )
    return any(error.startswith(prefixes) for error in errors)


def _is_draft_schema_validation_error(errors: tuple[str, ...]) -> bool:
    """Recognize the intentionally generic error returned for an untrusted draft."""
    return errors == ("generated note response failed schema validation",)


def _coverage_plan_schema_validation_feedback(
    error: ValidationError,
) -> tuple[str, ...]:
    """Give one bounded corrective retry the specific structural failure."""
    feedback = [
        "coverage plan response failed schema validation. Return a complete "
        "CoveragePlan instance, never the JSON Schema itself or its $defs. "
        "Use schema_version 1.1; each unit has one to three source anchors, "
        "each use visual has zero to three supporting OCR IDs, and each omit "
        "visual has unit_label null and an empty supporting OCR list. Audit all "
        "visual entries against this invariant, not only a reported index."
    ]
    for detail in error.errors(include_url=False):
        location = detail.get("loc", ())
        message = str(detail.get("msg", ""))
        corrective_message: str | None = None
        if (
            len(location) == 3
            and location[0] == "units"
            and isinstance(location[1], int)
            and location[2] == "source_evidence_ids"
        ):
            corrective_message = (
                "coverage plan unit "
                f"{location[1] + 1} source_evidence_ids must contain one to three IDs"
            )
        elif (
            len(location) == 3
            and location[0] == "visuals"
            and isinstance(location[1], int)
            and location[2] == "supporting_ocr_evidence_ids"
        ):
            corrective_message = (
                "coverage plan visual "
                f"{location[1] + 1} supporting_ocr_evidence_ids must contain at most three IDs"
            )
        elif (
            len(location) == 2
            and location[0] == "visuals"
            and isinstance(location[1], int)
            and "omitted visual requires no supporting OCR evidence" in message
        ):
            corrective_message = (
                "coverage plan visual "
                f"{location[1] + 1} marked omit must have an empty "
                "supporting_ocr_evidence_ids list"
            )
        if corrective_message is not None and corrective_message not in feedback:
            feedback.append(corrective_message)
    return tuple(feedback)


def _provider_reviewer(provider: NoteProvider) -> NoteReviewer:
    plan = getattr(provider, "plan", None)
    review = getattr(provider, "review", None)
    if not callable(plan) or not callable(review):
        raise ValueError("provider-generated notes require an independent reviewer")
    return cast(NoteReviewer, provider)


def _prepare_draft_evidence_scope(
    temporary: Path,
    content_pack: ContentPack,
    coverage_plan: CoveragePlan,
    plan_metadata: dict[str, object],
) -> tuple[
    DraftEvidenceScope | None,
    str | None,
    str | None,
    dict[str, object],
    tuple[str, ...],
]:
    """Persist the exact draft boundary before any provider draft call."""
    metadata = dict(plan_metadata)
    try:
        scope = build_draft_evidence_scope(content_pack, coverage_plan)
    except Exception:
        errors = ("draft evidence scope is invalid",)
        metadata["draft_evidence_scope_status"] = "invalid"
        metadata["draft_evidence_scope_errors"] = list(errors)
        return None, None, None, metadata, errors

    scope_json = scope.model_dump_json(indent=2)
    scope_path = temporary / "draft-evidence-scope.json"
    _write_text(scope_path, scope_json)
    try:
        execution_json = build_draft_execution_json(scope_json)
    except Exception:
        errors = ("draft execution mapping is invalid",)
        metadata["draft_evidence_scope_status"] = "invalid"
        metadata["draft_evidence_scope_errors"] = list(errors)
        return None, None, None, metadata, errors

    metadata["draft_evidence_scope_status"] = "ready"
    metadata["draft_evidence_scope_sha256"] = _sha256_file(scope_path)
    metadata["draft_execution_sha256"] = hashlib.sha256(
        execution_json.encode("utf-8")
    ).hexdigest()
    return scope, scope_json, execution_json, metadata, ()


def _citation_auditor(reviewer: NoteReviewer) -> CitationAuditor:
    """Require the cited-only capability separately from coverage review."""
    audit_citations = getattr(reviewer, "audit_citations", None)
    if not callable(audit_citations):
        raise ValueError("selected reviewer has no citation auditor capability")
    return cast(CitationAuditor, reviewer)


def _citation_audit_metadata(
    auditor: CitationAuditor | NoteReviewer,
    *,
    status: str,
    errors: tuple[str, ...],
    call_count: int = 0,
    packet_sha256: str | None = None,
    packet_file_sha256: str | None = None,
    audit_sha256: str | None = None,
) -> dict[str, object]:
    """Keep CitationAudit state inside the existing generation review metadata."""
    metadata: dict[str, object] = {
        "provider": auditor.name,
        "model": auditor.model,
        "call_count": call_count,
        "status": status,
        "errors": list(errors),
    }
    if packet_sha256 is not None:
        metadata["statement_citation_packet_sha256"] = packet_sha256
    if packet_file_sha256 is not None:
        metadata["statement_citation_packet_file_sha256"] = packet_file_sha256
    if audit_sha256 is not None:
        metadata["citation_audit_sha256"] = audit_sha256
    return metadata


def _run_citation_audit(
    temporary: Path,
    auditor: CitationAuditor,
    packet: StatementCitationPacketV12,
    prior_metadata: dict[str, object],
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Audit one frozen packet with one stateless output-contract retry."""
    packet_json = packet.model_dump_json(indent=2)
    packet_path = temporary / "statement-citation-packet.json"
    _write_text(packet_path, packet_json)
    packet_sha256 = statement_citation_packet_sha256(packet)
    audit_metadata = _citation_audit_metadata(
        auditor,
        status="failed",
        errors=(),
        packet_sha256=packet_sha256,
        packet_file_sha256=_sha256_file(packet_path),
    )
    metadata = {**prior_metadata, "citation_audit": audit_metadata}
    feedback: tuple[str, ...] = ()

    for attempt in range(1, 3):
        audit_metadata["call_count"] = attempt
        try:
            raw = auditor.audit_citations(
                packet_json,
                validation_feedback=feedback,
            )
        except Exception as error:
            errors = (
                (str(error) if isinstance(error, NoteProviderError) else None)
                or f"citation auditor failed: {type(error).__name__}",
            )
            audit_metadata["status"] = "failed"
            audit_metadata["errors"] = list(errors)
            return metadata, errors

        _write_text(temporary / f"citation-audit-{attempt}.raw.txt", raw)
        try:
            audit = parse_citation_audit_v12(raw)
        except ValidationError:
            errors = _CITATION_AUDIT_FORMAT_RETRY
            if attempt == 1:
                feedback = _CITATION_AUDIT_FORMAT_RETRY
                continue
            audit_metadata["status"] = "invalid"
            audit_metadata["errors"] = list(errors)
            return metadata, errors

        audit_path = temporary / "citation-audit.json"
        _write_text(audit_path, audit.model_dump_json(indent=2) + "\n")
        audit_metadata["citation_audit_sha256"] = _sha256_file(audit_path)
        validation_errors = tuple(validate_citation_audit_v12(audit, packet))
        if validation_errors:
            if _is_semantic_citation_rejection(audit, validation_errors):
                audit_metadata["status"] = "rejected"
                audit_metadata["errors"] = list(validation_errors)
                return metadata, validation_errors
            errors = tuple(
                f"citation audit invalid: {error}" for error in validation_errors
            )
            if attempt == 1 and _is_retriable_citation_audit_contract_failure(
                validation_errors
            ):
                feedback = _CITATION_AUDIT_LOCAL_CONTRACT_RETRY
                continue
            audit_metadata["status"] = "invalid"
            audit_metadata["errors"] = list(errors)
            return metadata, errors

        audit_metadata["status"] = "approved"
        return metadata, ()

    raise AssertionError("citation audit retry loop exhausted without a result")


def _is_semantic_citation_rejection(
    audit: CitationAuditV12,
    errors: tuple[str, ...],
) -> bool:
    """Only a clean non-entailment finding receives rejected rather than invalid."""
    semantic_error_prefixes = (
        "citation audit rejected due to non-entailed clause:",
        "citation audit leaves semantic citation unused:",
    )
    return (
        audit.verdict == "reject"
        and bool(errors)
        and all(error.startswith(semantic_error_prefixes) for error in errors)
    )


def _is_retriable_citation_audit_contract_failure(
    errors: tuple[str, ...],
) -> bool:
    """Retry a model response once, but never retry an invalid local packet."""
    return not any(error.startswith("statement citation packet ") for error in errors)


def _run_coverage_plan(
    temporary: Path,
    reviewer: NoteReviewer,
    content_pack_json: str,
    content_pack: ContentPack,
) -> tuple[
    CoveragePlan | None,
    str | None,
    dict[str, object],
    tuple[str, ...],
]:
    metadata: dict[str, object] = {
        "provider": reviewer.name,
        "model": reviewer.model,
        "plan_call_count": 0,
        "call_count": 0,
        "status": "failed",
        "errors": [],
    }
    feedback: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    for attempt in range(1, 3):
        metadata["plan_call_count"] = attempt
        try:
            raw = reviewer.plan(
                content_pack_json,
                validation_feedback=feedback,
            )
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, NoteProviderError) and str(error).strip()
                else f"coverage planner failed: {type(error).__name__}"
            )
            errors = (message,)
            metadata["errors"] = list(errors)
            return None, None, metadata, errors

        _write_text(temporary / f"coverage-plan-{attempt}.raw.txt", raw)
        try:
            coverage_plan = parse_coverage_plan(raw)
        except ValidationError as error:
            errors = ("coverage plan response failed schema validation",)
            feedback = _coverage_plan_schema_validation_feedback(error)
        else:
            errors = tuple(
                f"coverage plan invalid: {error}"
                for error in validate_coverage_plan(coverage_plan, content_pack)
            )
            feedback = _COVERAGE_PLAN_LOCAL_CONTRACT_RETRY
            if not errors:
                canonical_json = coverage_plan.model_dump_json(indent=2)
                coverage_plan_path = temporary / "coverage-plan.json"
                _write_text(coverage_plan_path, canonical_json)
                metadata["coverage_plan_sha256"] = _sha256_file(coverage_plan_path)
                metadata["status"] = "planned"
                return coverage_plan, canonical_json, metadata, ()

    metadata["status"] = "invalid"
    metadata["errors"] = list(errors)
    return None, None, metadata, errors


def _run_quality_review(
    temporary: Path,
    reviewer: NoteReviewer,
    content_pack_json: str,
    content_pack: ContentPack,
    note: AnyGeneratedNote,
    coverage_plan: CoveragePlan,
    coverage_plan_json: str,
    plan_metadata: dict[str, object],
) -> tuple[dict[str, object], tuple[str, ...]]:
    candidate_json = note.model_dump_json(indent=2) + "\n"
    candidate_path = temporary / "review-candidate.json"
    _write_text(candidate_path, candidate_json)
    statement_manifest_json = json.dumps(
        build_statement_manifest(note),
        ensure_ascii=False,
        indent=2,
    )
    statement_manifest_path = temporary / "review-statement-manifest.json"
    _write_text(statement_manifest_path, statement_manifest_json)
    metadata: dict[str, object] = {
        **plan_metadata,
        "call_count": 0,
        "status": "failed",
        "candidate_note_sha256": _sha256_file(candidate_path),
        "statement_manifest_sha256": _sha256_file(statement_manifest_path),
        "errors": [],
    }
    feedback: tuple[str, ...] = ()
    for attempt in range(1, 3):
        metadata["call_count"] = attempt
        try:
            raw = reviewer.review(
                content_pack_json,
                candidate_json,
                coverage_plan_json,
                statement_manifest_json,
                validation_feedback=feedback,
            )
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, NoteProviderError) and str(error).strip()
                else f"reviewer failed: {type(error).__name__}"
            )
            errors = (message,)
            metadata["errors"] = list(errors)
            return metadata, errors

        _write_text(temporary / f"review-{attempt}.raw.txt", raw)
        try:
            review = parse_note_review(raw)
        except ValidationError:
            errors = ("quality review response failed schema validation",)
            if attempt == 1:
                feedback = errors
                continue
            metadata["status"] = "invalid"
            metadata["errors"] = list(errors)
            return metadata, errors

        review_path = temporary / "review.json"
        _write_text(review_path, review.model_dump_json(indent=2) + "\n")
        metadata["review_sha256"] = _sha256_file(review_path)
        consistency_errors = tuple(
            f"quality review invalid: {error}"
            for error in (
                *validate_note_review(review, content_pack, note),
                *validate_review_against_plan(review, coverage_plan, note),
            )
        )
        if consistency_errors:
            metadata["status"] = "invalid"
            metadata["errors"] = list(consistency_errors)
            return metadata, consistency_errors
        if review.verdict == "reject":
            errors = (_review_rejection_error(review),)
            metadata["status"] = "rejected"
            metadata["errors"] = list(errors)
            return metadata, errors

        metadata["status"] = "approved"
        return metadata, ()

    raise AssertionError("review retry loop exhausted without a result")


def _review_rejection_error(review: NoteReview) -> str:
    findings = (
        [unit.status for unit in review.coverage_units if unit.status != "covered"]
        + [
            "evidence_misalignment"
            for audit in review.statement_audits
            if audit.status == "misaligned"
        ]
        + [issue.category for issue in review.issues]
    )
    return "quality review rejected: " + ", ".join(dict.fromkeys(findings))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_external_note_bundle(
    task_dir: Path,
    raw_path: Path,
    *,
    note_type: str | None = None,
    run_id: str | None = None,
) -> Path:
    """Validate one external Agent response through the same immutable bundle path."""
    context = _load_context(task_dir)
    selection = _external_note_type_selection(
        resolve_note_type_selection(context.task, note_type)
    )
    selected_run_id = run_id or _new_run_id(context.task)
    temporary, final = _prepare_bundle(context.task_dir, selected_run_id)
    try:
        raw = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        errors = (f"external note could not be read: {type(error).__name__}",)
        return _finish_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            "external-agent",
            "external",
            0,
            errors,
            selection=selection,
            raise_error=True,
        )
    _write_text(temporary / "attempt-1.raw.txt", raw)
    note, errors = _parse_and_validate(
        raw,
        context.content_pack,
        selection=selection,
        require_v3=True,
    )
    if note is None or errors:
        return _finish_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            "external-agent",
            "external",
            1,
            errors,
            selection=selection,
            note_type=_note_type(note) if note is not None else None,
            raise_error=True,
        )
    return _finish_success_bundle(
        temporary,
        final,
        context,
        selected_run_id,
        "external-agent",
        "external",
        1,
        note,
        selection=selection,
    )


def validate_external_note(
    task_dir: Path,
    raw_path: Path,
    *,
    note_type: str | None = None,
) -> list[str]:
    """Validate an external note without creating files or changing task state."""
    context = _load_context(task_dir)
    selection = _external_note_type_selection(
        resolve_note_type_selection(context.task, note_type)
    )
    try:
        raw = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return [f"external note could not be read: {type(error).__name__}"]
    _, errors = _parse_and_validate(
        raw,
        context.content_pack,
        selection=selection,
        require_v3=True,
    )
    return list(errors)


class _GenerationContext:
    def __init__(
        self,
        task_dir: Path,
        task: TaskRecord,
        content_pack: ContentPack,
        content_pack_bytes: bytes,
    ) -> None:
        self.task_dir = task_dir
        self.task = task
        self.content_pack = content_pack
        self.content_pack_sha256 = hashlib.sha256(content_pack_bytes).hexdigest()
        self.asset_prefix = task_dir.relative_to(task_dir.parents[1]).as_posix()


def _load_context(task_dir: Path) -> _GenerationContext:
    root = task_dir.resolve()
    task = load_task(root)
    content_pack_path = next(
        (
            root / path
            for path in task.artifacts.get("content_pack", [])
            if Path(path).name == "content_pack.json"
        ),
        None,
    )
    if content_pack_path is None:
        raise ValueError("task has no content_pack.json artifact")
    resolved = content_pack_path.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("content_pack.json artifact is missing or outside task")
    payload = resolved.read_bytes()
    content_pack = ContentPack.model_validate_json(payload)
    if content_pack.task_id != task.task_id:
        raise ValueError("content pack task_id does not match task.json")
    if content_pack.source_fingerprint != task.source_fingerprint:
        raise ValueError("content pack source_fingerprint does not match task.json")
    return _GenerationContext(root, task, content_pack, payload)


def _parse_and_validate(
    raw: str,
    content_pack: ContentPack,
    *,
    selection: NoteTypeSelection | None = None,
    require_v3: bool = False,
) -> tuple[AnyGeneratedNote | None, tuple[str, ...]]:
    try:
        note = parse_generated_note(raw)
    except ValidationError as error:
        return None, tuple(_safe_pydantic_errors(error))
    return note, _validate_parsed_note(
        note,
        content_pack,
        selection=selection,
        require_v3=require_v3,
    )


def _parse_provider_attempt(
    raw: str,
    content_pack: ContentPack,
    *,
    selection: NoteTypeSelection,
) -> tuple[
    AnyGeneratedNote | None,
    tuple[str, ...],
    list[dict[str, object]],
]:
    try:
        note = parse_generated_note(raw)
    except ValidationError:
        return None, ("generated note response failed schema validation",), []
    normalized, records = _normalize_provider_v3_ocr_parents(note, content_pack)
    return (
        normalized,
        _validate_parsed_note(
            normalized,
            content_pack,
            selection=selection,
            require_v3=True,
        ),
        records,
    )


def _normalize_provider_v3_ocr_parents(
    note: AnyGeneratedNote,
    content_pack: ContentPack,
) -> tuple[AnyGeneratedNote, list[dict[str, object]]]:
    if isinstance(note, GeneratedNote):
        return note, []

    normalized = note.model_copy(deep=True)
    evidence_by_id = {item.id: item for item in content_pack.evidence}
    records: list[dict[str, object]] = []
    for statement_index, statement in enumerate(
        iter_factual_statements(normalized),
        start=1,
    ):
        existing_ids = set(statement.evidence_ids)
        triggers_by_parent: dict[str, list[str]] = {}
        for evidence_id in statement.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if (
                evidence is None
                or evidence.kind != "ocr"
                or evidence.frame_id is None
                or evidence.frame_id in existing_ids
            ):
                continue
            trigger_ids = triggers_by_parent.setdefault(evidence.frame_id, [])
            if evidence_id not in trigger_ids:
                trigger_ids.append(evidence_id)

        for parent_frame_id, trigger_ids in triggers_by_parent.items():
            statement.evidence_ids.append(parent_frame_id)
            existing_ids.add(parent_frame_id)
            records.append(
                {
                    "kind": "v3_factual_ocr_parent",
                    "factual_statement_index": statement_index,
                    "trigger_ocr_evidence_ids": trigger_ids,
                    "added_parent_frame_id": parent_frame_id,
                }
            )
    return normalized, records


def _validate_parsed_note(
    note: AnyGeneratedNote,
    content_pack: ContentPack,
    *,
    selection: NoteTypeSelection | None,
    require_v3: bool,
) -> tuple[str, ...]:
    if require_v3 and isinstance(note, GeneratedNote):
        return ("new note generation requires GeneratedNote 3.0",)
    return tuple(
        validate_generated_note(
            note,
            content_pack,
            requested_note_type=(
                selection.requested if selection is not None else None
            ),
            require_classification_evidence=(
                selection is not None and selection.requested is None
            ),
        )
    )


def _external_note_type_selection(
    selection: NoteTypeSelection,
) -> NoteTypeSelection:
    if selection.requested is None:
        return NoteTypeSelection(None, "external")
    return selection


def _active_note_type_selection(
    active_note_path: Path,
    note: AnyGeneratedNote,
) -> NoteTypeSelection | None:
    if isinstance(note, GeneratedNote):
        return None
    metadata_path = active_note_path.parent / "generation.json"
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(
            "active GeneratedNote 3.0 has no readable generation metadata"
        ) from error
    source = payload.get("note_type_source")
    if source not in {"auto", "cli", "source", "external"}:
        raise ValueError(
            "active GeneratedNote 3.0 has invalid note_type_source metadata"
        )
    return NoteTypeSelection(
        note.note_type,
        cast(Literal["auto", "cli", "source", "external"], source),
    )


def _note_type(note: AnyGeneratedNote) -> ConcreteNoteType | None:
    if isinstance(note, GeneratedNote):
        return None
    return note.note_type


def _safe_pydantic_errors(error: ValidationError) -> list[str]:
    messages: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "root"
        messages.append(f"{location}: {item['msg']}")
    return messages or ["generated note JSON failed schema validation"]


def _prepare_bundle(task_dir: Path, run_id: str) -> tuple[Path, Path]:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must contain only safe filename characters")
    parent = task_dir / "generated_notes"
    temporary = parent / f".{run_id}"
    final = parent / run_id
    if temporary.exists() or final.exists():
        raise FileExistsError(f"note bundle already exists: {run_id}")
    parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    return temporary, final


def _finish_success_bundle(
    temporary: Path,
    final: Path,
    context: _GenerationContext,
    run_id: str,
    provider: str,
    model: str,
    attempt_count: int,
    note: AnyGeneratedNote,
    *,
    selection: NoteTypeSelection | None = None,
    operation: str = "generate",
    evidence_normalizations: list[dict[str, object]] | None = None,
    review_metadata: dict[str, object] | None = None,
) -> Path:
    render_task = (
        context.task
        if operation == "rerender"
        else _without_derived_artifacts(context.task)
    )
    markdown = render_generated_note(
        render_task,
        context.content_pack,
        note,
        asset_prefix=context.asset_prefix,
    )
    markdown_errors = tuple(
        validate_note_markdown_text(context.task_dir, markdown, context.content_pack)
    )
    if markdown_errors:
        return _finish_failed_bundle(
            temporary,
            final,
            context,
            run_id,
            provider,
            model,
            attempt_count,
            markdown_errors,
            selection=selection,
            note_type=_note_type(note),
            evidence_normalizations=evidence_normalizations,
            review_metadata=review_metadata,
            raise_error=True,
        )
    _write_text(temporary / "note.json", note.model_dump_json(indent=2))
    _write_text(temporary / "note.md", markdown)
    _write_text(temporary / "published_note.md", markdown)
    _write_generation_metadata(
        temporary,
        context,
        run_id,
        provider,
        model,
        attempt_count,
        "completed",
        (),
        operation,
        selection=selection,
        note_type=_note_type(note),
        evidence_normalizations=evidence_normalizations,
        review_metadata=review_metadata,
    )
    os.replace(temporary, final)
    return final


def _without_derived_artifacts(task: TaskRecord) -> TaskRecord:
    return task.model_copy(
        update={
            "stages": {
                **task.stages,
                "podcast_script": "pending",
                "tts": "pending",
            },
            "artifacts": {
                stage: paths
                for stage, paths in task.artifacts.items()
                if stage not in {"podcast_script", "tts"}
            },
        }
    )


def _finish_failed_bundle(
    temporary: Path,
    final: Path,
    context: _GenerationContext,
    run_id: str,
    provider: str,
    model: str,
    attempt_count: int,
    errors: tuple[str, ...],
    *,
    selection: NoteTypeSelection | None = None,
    note_type: ConcreteNoteType | None = None,
    evidence_normalizations: list[dict[str, object]] | None = None,
    review_metadata: dict[str, object] | None = None,
    raise_error: bool,
) -> Path:
    _write_generation_metadata(
        temporary,
        context,
        run_id,
        provider,
        model,
        attempt_count,
        "failed",
        errors,
        selection=selection,
        note_type=note_type,
        evidence_normalizations=evidence_normalizations,
        review_metadata=review_metadata,
    )
    os.replace(temporary, final)
    if raise_error:
        raise NoteGenerationError(final, errors)
    return final


def _write_generation_metadata(
    directory: Path,
    context: _GenerationContext,
    run_id: str,
    provider: str,
    model: str,
    attempt_count: int,
    status: str,
    errors: tuple[str, ...],
    operation: str = "generate",
    *,
    selection: NoteTypeSelection | None = None,
    note_type: ConcreteNoteType | None = None,
    evidence_normalizations: list[dict[str, object]] | None = None,
    review_metadata: dict[str, object] | None = None,
) -> None:
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "provider": provider,
        "model": model,
        "attempt_count": attempt_count,
        "status": status,
        "operation": operation,
        "content_pack_sha256": context.content_pack_sha256,
        "errors": list(errors),
    }
    if selection is not None:
        payload["note_type"] = selection.requested or note_type
        payload["note_type_source"] = selection.source
    if evidence_normalizations:
        payload["evidence_normalizations"] = evidence_normalizations
    if review_metadata is not None:
        payload["review"] = review_metadata
    _write_text(
        directory / "generation.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _write_text(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        if not content.endswith("\n") and path.suffix != ".txt":
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _new_run_id(task: TaskRecord) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{task.task_id[-8:]}"
