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
from learnnest.note_audit import (
    NoteAudit,
    note_audit_statement_manifest,
    parse_note_audit,
    validate_note_audit,
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
from learnnest.note_models import AnyGeneratedNote, GeneratedNote, GeneratedNoteV4
from learnnest.note_providers import (
    CitationAuditor,
    DEFAULT_NOTE_SAFE_INPUT_TOKENS,
    NoteAuditor,
    NoteProvider,
    NoteProviderError,
    NoteReviewer,
    V4NoteProvider,
    build_draft_execution_json,
)
from learnnest.note_templates import (
    NoteTemplate,
    load_template_snapshot,
    template_snapshot_json,
    template_snapshot_sha256,
)
from learnnest.note_review import (
    NoteReview,
    build_statement_manifest,
    parse_note_review,
    validate_note_review,
)
from learnnest.note_types import ConcreteNoteType, normalize_note_type
from learnnest.note_validation import (
    iter_factual_statement_entries,
    iter_factual_statements,
    parse_generated_note,
    validate_generated_note,
    validate_v4_bundle_provenance,
    validate_v4_source_contract,
    validate_v4_template_presentation,
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


V4ReviewMode = Literal["none", "report", "gate"]


@dataclass(frozen=True)
class V4BundleResult:
    """A persisted V4 candidate plus the policy decision about activation."""

    bundle_path: Path
    review_status: Literal["not_requested", "passed", "flagged", "unavailable"]
    activation_allowed: bool


@dataclass(frozen=True)
class V4ActivationResult:
    """Result of a V4 explicit command; a gated candidate may remain inactive."""

    task: TaskRecord
    bundle_path: Path
    activated: bool
    review_status: Literal["not_requested", "passed", "flagged", "unavailable"]


def _recovered_v4_activation_result(
    task_dir: Path,
    task: TaskRecord,
) -> V4ActivationResult:
    """Return the already-active bundle after publish recovery without a new call."""
    root = task_dir.resolve()
    note_json = next(
        (
            Path(path)
            for path in task.artifacts.get("note", [])
            if Path(path).name == "note.json"
        ),
        None,
    )
    if note_json is None:
        raise RuntimeError("recovered publication has no active note.json")
    bundle_path = (root / note_json.parent).resolve()
    if not bundle_path.is_relative_to(root):
        raise RuntimeError("recovered note bundle is outside the task directory")

    review_status: Literal["not_requested", "passed", "flagged", "unavailable"] = (
        "not_requested"
    )
    try:
        metadata = json.loads(
            (bundle_path / "generation.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        metadata = None
    if isinstance(metadata, dict):
        review = metadata.get("review")
        status = review.get("status") if isinstance(review, dict) else None
        if status in {"not_requested", "passed", "flagged", "unavailable"}:
            review_status = cast(
                Literal["not_requested", "passed", "flagged", "unavailable"], status
            )
    return V4ActivationResult(
        task=task,
        bundle_path=bundle_path,
        activated=True,
        review_status=review_status,
    )


def _reconcile_pending_v4_auto_activation(
    task_dir: Path,
    output_root: Path,
) -> V4ActivationResult | None:
    """Finish an interrupted automatic V4 activation without another model call.

    Only a bundle created by the current persisted note attempt and already marked
    ``activation_decision=activate`` is eligible. Gated candidates therefore stay
    inactive, even after an interruption.
    """
    context = _load_context(task_dir)
    attempt_id = context.task.active_attempt_id
    if attempt_id is None:
        return None
    attempt = next(
        (item for item in context.task.attempts if item.attempt_id == attempt_id),
        None,
    )
    if attempt is None or attempt.from_stage != "note":
        return None

    bundles_root = context.task_dir / "generated_notes"
    if not bundles_root.is_dir():
        return None
    candidates: list[tuple[Path, dict[str, object]]] = []
    for bundle in bundles_root.iterdir():
        if not bundle.is_dir():
            continue
        try:
            metadata = json.loads((bundle / "generation.json").read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        if (
            metadata.get("note_schema_version") == "4.0"
            and metadata.get("attempt_id") == attempt_id
            and metadata.get("candidate_status") == "source_valid"
            and metadata.get("source_validation_status") == "source_valid"
            and metadata.get("activation_decision") == "activate"
        ):
            candidates.append((bundle.resolve(), metadata))
    if not candidates:
        return None
    if len(candidates) != 1:
        raise RuntimeError(
            "multiple source-valid V4 candidates belong to the interrupted attempt"
        )

    bundle, metadata = candidates[0]
    errors: list[str] = []
    try:
        template = load_template_snapshot(bundle / "template.json")
        note = parse_generated_note((bundle / "note.json").read_text("utf-8"))
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        raise RuntimeError(
            "interrupted V4 candidate could not be read for safe activation"
        ) from error
    if not isinstance(note, GeneratedNoteV4):
        errors.append("interrupted candidate is not GeneratedNote 4.0")
    else:
        errors.extend(
            validate_v4_source_contract(note, context.content_pack, template=template)
        )
        errors.extend(
            validate_v4_bundle_provenance(
                bundle,
                note,
                template,
                content_pack_sha256=context.content_pack_sha256,
            )
        )
        if not errors:
            try:
                rendered = render_generated_note(
                    _without_derived_artifacts(context.task),
                    context.content_pack,
                    note,
                    asset_prefix=context.asset_prefix,
                    template=template,
                )
                note_markdown = (bundle / "note.md").read_text("utf-8")
            except (OSError, UnicodeError, ValueError) as error:
                raise RuntimeError(
                    "interrupted V4 candidate could not be rendered for safe activation"
                ) from error
            if note_markdown != rendered:
                errors.append(
                    "interrupted V4 candidate note.md differs from canonical render"
                )
            errors.extend(
                validate_note_markdown_text(
                    context.task_dir, rendered, context.content_pack
                )
            )
    if errors:
        raise RuntimeError(
            "interrupted V4 candidate cannot be safely activated: " + "; ".join(errors)
        )

    provider = metadata.get("provider")
    model = metadata.get("model")
    if not isinstance(provider, str) or not provider:
        raise RuntimeError("interrupted V4 candidate has an invalid provider")
    if not isinstance(model, str) or not model:
        raise RuntimeError("interrupted V4 candidate has an invalid model")
    activate_note_bundle(
        context.task_dir,
        bundle,
        context.task,
        output_root=output_root,
        provider=provider,
        model=model,
    )
    completed = complete_persisted_attempt(context.task_dir, now=datetime.now(UTC))
    review = metadata.get("review")
    review_status = review.get("status") if isinstance(review, dict) else None
    if review_status not in {"not_requested", "passed", "flagged", "unavailable"}:
        raise RuntimeError("interrupted V4 candidate has an invalid review status")
    return V4ActivationResult(
        task=completed,
        bundle_path=bundle,
        activated=True,
        review_status=cast(
            Literal["not_requested", "passed", "flagged", "unavailable"],
            review_status,
        ),
    )


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


def generate_and_activate_v4_note(
    task_dir: Path,
    provider: V4NoteProvider,
    output_root: Path,
    *,
    template: NoteTemplate,
    review_mode: V4ReviewMode = "none",
    auditor: NoteAuditor | None = None,
    retain_debug_artifacts: bool = False,
) -> V4ActivationResult:
    """Generate one V4 candidate and activate it only under the selected policy."""
    recovered = reconcile_pending_note_publication(task_dir, output_root)
    if recovered is not None:
        return _recovered_v4_activation_result(task_dir, recovered)
    recovered_auto_activation = _reconcile_pending_v4_auto_activation(
        task_dir, output_root
    )
    if recovered_auto_activation is not None:
        return recovered_auto_activation
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
        bundle = generate_v4_note_bundle(
            task_dir,
            provider,
            template=template,
            review_mode=review_mode,
            auditor=auditor,
            retain_debug_artifacts=retain_debug_artifacts,
        )
        activated = bundle.activation_allowed
        if activated:
            activate_note_bundle(
                task_dir,
                bundle.bundle_path,
                load_task(task_dir),
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
    completed = complete_persisted_attempt(task_dir, now=datetime.now(UTC))
    return V4ActivationResult(
        task=completed,
        bundle_path=bundle.bundle_path,
        activated=activated,
        review_status=bundle.review_status,
    )


def generate_v4_note_bundle(
    task_dir: Path,
    provider: V4NoteProvider,
    *,
    template: NoteTemplate,
    review_mode: V4ReviewMode = "none",
    auditor: NoteAuditor | None = None,
    run_id: str | None = None,
    retain_debug_artifacts: bool = False,
) -> V4BundleResult:
    """Persist one source-valid V4 candidate; never issue an implicit retry."""
    if review_mode not in {"none", "report", "gate"}:
        raise ValueError("review_mode must be one of: none, report, gate")
    context = _load_context(task_dir)
    selected_run_id = run_id or _new_run_id(context.task)
    temporary, final = _prepare_bundle(context.task_dir, selected_run_id)
    snapshot_json = template_snapshot_json(template)
    _write_text(temporary / "template.json", snapshot_json)
    content_pack_json = context.content_pack.model_dump_json()
    model_call_count = 0

    safe_input_tokens = getattr(
        provider, "safe_input_tokens", DEFAULT_NOTE_SAFE_INPUT_TOKENS
    )
    if not isinstance(safe_input_tokens, int) or safe_input_tokens < 1:
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            provider.name,
            provider.model,
            template,
            review_mode,
            model_call_count,
            ("provider safe input token budget is invalid",),
            candidate_status="not_created",
        )
    estimated_tokens = _estimate_v4_input_tokens(content_pack_json, snapshot_json)
    if estimated_tokens > safe_input_tokens:
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            provider.name,
            provider.model,
            template,
            review_mode,
            model_call_count,
            (
                "content pack exceeds provider safe input budget: "
                f"estimated {estimated_tokens}, budget {safe_input_tokens}",
            ),
            candidate_status="not_created",
        )

    try:
        model_call_count = 1
        raw = provider.generate_v4(content_pack_json, snapshot_json)
    except Exception as error:
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            provider.name,
            provider.model,
            template,
            review_mode,
            model_call_count,
            (f"provider failed: {type(error).__name__}",),
            candidate_status="not_created",
        )
    if retain_debug_artifacts:
        _write_text(temporary / "attempt-1.raw.txt", raw)
    response_normalizations: list[str] = []
    try:
        parsed = parse_generated_note(raw)
    except ValidationError:
        repaired, response_normalizations = _repair_v4_provider_json(raw)
        if repaired is None:
            return _finish_v4_failed_bundle(
                temporary,
                final,
                context,
                selected_run_id,
                provider.name,
                provider.model,
                template,
                review_mode,
                model_call_count,
                ("generated note response failed schema validation",),
                candidate_status="not_created",
            )
        try:
            parsed = parse_generated_note(repaired)
        except ValidationError:
            return _finish_v4_failed_bundle(
                temporary,
                final,
                context,
                selected_run_id,
                provider.name,
                provider.model,
                template,
                review_mode,
                model_call_count,
                ("generated note response failed schema validation",),
                candidate_status="not_created",
                response_normalizations=response_normalizations,
            )
        if retain_debug_artifacts:
            _write_text(temporary / "attempt-1.repaired.json", repaired)
    if not isinstance(parsed, GeneratedNoteV4):
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            provider.name,
            provider.model,
            template,
            review_mode,
            model_call_count,
            ("new note generation requires GeneratedNote 4.0",),
            candidate_status="candidate",
            response_normalizations=response_normalizations,
        )
    normalized, evidence_normalizations = _normalize_provider_v4_ocr_parents(
        parsed, context.content_pack
    )
    errors = tuple(
        validate_v4_source_contract(normalized, context.content_pack, template=template)
    )
    template_warnings = tuple(validate_v4_template_presentation(normalized, template))
    if errors:
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            provider.name,
            provider.model,
            template,
            review_mode,
            model_call_count,
            errors,
            candidate_status="candidate",
            template_warnings=template_warnings,
            evidence_normalizations=evidence_normalizations,
            response_normalizations=response_normalizations,
        )

    return _finish_v4_source_valid_bundle(
        temporary,
        final,
        context,
        selected_run_id,
        provider.name,
        provider.model,
        template,
        normalized,
        review_mode=review_mode,
        auditor=auditor,
        model_call_count=model_call_count,
        content_pack_json=content_pack_json,
        template_json=snapshot_json,
        template_warnings=template_warnings,
        evidence_normalizations=evidence_normalizations,
        response_normalizations=response_normalizations,
        retain_debug_artifacts=retain_debug_artifacts,
    )


def build_and_activate_external_v4_note(
    task_dir: Path,
    raw_path: Path,
    output_root: Path,
    *,
    template: NoteTemplate,
    review_mode: V4ReviewMode = "none",
    auditor: NoteAuditor | None = None,
    retain_debug_artifacts: bool = False,
) -> V4ActivationResult:
    """Validate and optionally audit a new external V4 candidate before activation."""
    recovered = reconcile_pending_note_publication(task_dir, output_root)
    if recovered is not None:
        return _recovered_v4_activation_result(task_dir, recovered)
    recovered_auto_activation = _reconcile_pending_v4_auto_activation(
        task_dir, output_root
    )
    if recovered_auto_activation is not None:
        return recovered_auto_activation
    begin_persisted_attempt(
        task_dir,
        reason="retry",
        from_stage="note",
        now=datetime.now(UTC),
    )
    try:
        bundle = build_external_v4_note_bundle(
            task_dir,
            raw_path,
            template=template,
            review_mode=review_mode,
            auditor=auditor,
            retain_debug_artifacts=retain_debug_artifacts,
        )
        activated = bundle.activation_allowed
        if activated:
            activate_note_bundle(
                task_dir,
                bundle.bundle_path,
                load_task(task_dir),
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
    completed = complete_persisted_attempt(task_dir, now=datetime.now(UTC))
    return V4ActivationResult(
        task=completed,
        bundle_path=bundle.bundle_path,
        activated=activated,
        review_status=bundle.review_status,
    )


def build_external_v4_note_bundle(
    task_dir: Path,
    raw_path: Path,
    *,
    template: NoteTemplate,
    review_mode: V4ReviewMode = "none",
    auditor: NoteAuditor | None = None,
    run_id: str | None = None,
    retain_debug_artifacts: bool = False,
) -> V4BundleResult:
    """Persist a new external V4 bundle without reading any provider configuration."""
    if review_mode not in {"none", "report", "gate"}:
        raise ValueError("review_mode must be one of: none, report, gate")
    context = _load_context(task_dir)
    selected_run_id = run_id or _new_run_id(context.task)
    temporary, final = _prepare_bundle(context.task_dir, selected_run_id)
    snapshot_json = template_snapshot_json(template)
    _write_text(temporary / "template.json", snapshot_json)
    try:
        raw = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            "external-agent",
            "external",
            template,
            review_mode,
            0,
            (f"external note could not be read: {type(error).__name__}",),
            candidate_status="not_created",
            operation="external",
        )
    try:
        parsed = parse_generated_note(raw)
    except ValidationError:
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            "external-agent",
            "external",
            template,
            review_mode,
            0,
            ("generated note response failed schema validation",),
            candidate_status="not_created",
            operation="external",
        )
    if not isinstance(parsed, GeneratedNoteV4):
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            "external-agent",
            "external",
            template,
            review_mode,
            0,
            ("new external note activation requires GeneratedNote 4.0",),
            candidate_status="candidate",
            operation="external",
        )
    errors = tuple(
        validate_v4_source_contract(parsed, context.content_pack, template=template)
    )
    template_warnings = tuple(validate_v4_template_presentation(parsed, template))
    if errors:
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            selected_run_id,
            "external-agent",
            "external",
            template,
            review_mode,
            0,
            errors,
            candidate_status="candidate",
            operation="external",
            template_warnings=template_warnings,
        )
    return _finish_v4_source_valid_bundle(
        temporary,
        final,
        context,
        selected_run_id,
        "external-agent",
        "external",
        template,
        parsed,
        review_mode=review_mode,
        auditor=auditor,
        model_call_count=0,
        content_pack_json=context.content_pack.model_dump_json(),
        template_json=snapshot_json,
        operation="external",
        template_warnings=template_warnings,
        retain_debug_artifacts=retain_debug_artifacts,
    )


def validate_external_v4_note(
    task_dir: Path,
    raw_path: Path,
    *,
    template: NoteTemplate,
) -> list[str]:
    """Validate a V4 external candidate without creating a bundle or task attempt."""
    context = _load_context(task_dir)
    try:
        raw = raw_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return [f"external note could not be read: {type(error).__name__}"]
    try:
        parsed = parse_generated_note(raw)
    except ValidationError as error:
        return _safe_pydantic_errors(error)
    if not isinstance(parsed, GeneratedNoteV4):
        return ["new external note validation requires GeneratedNote 4.0"]
    return validate_v4_source_contract(parsed, context.content_pack, template=template)


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
        parsed = parse_generated_note(raw)
    except ValidationError as error:
        raise ValueError("active note.json is invalid") from error
    if isinstance(parsed, GeneratedNoteV4):
        return _rerender_v4_and_activate_note(
            task_dir,
            output_root,
            context,
            resolved,
            parsed,
            provider,
            model,
        )
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


def _rerender_v4_and_activate_note(
    task_dir: Path,
    output_root: Path,
    context: _GenerationContext,
    active_note_path: Path,
    note: GeneratedNoteV4,
    provider: str,
    model: str,
) -> TaskRecord:
    """Re-render V4 strictly from its historical template snapshot."""
    snapshot_path = (active_note_path.parent / "template.json").resolve()
    if (
        not snapshot_path.is_relative_to(context.task_dir)
        or not snapshot_path.is_file()
    ):
        raise ValueError("active GeneratedNote 4.0 has no template snapshot")
    try:
        template = load_template_snapshot(snapshot_path)
    except ValueError as error:
        raise ValueError(
            "active GeneratedNote 4.0 template snapshot is invalid"
        ) from error
    errors = validate_v4_source_contract(note, context.content_pack, template=template)
    errors.extend(
        validate_v4_bundle_provenance(
            active_note_path.parent,
            note,
            template,
            content_pack_sha256=context.content_pack_sha256,
        )
    )
    if errors:
        raise ValueError(f"active GeneratedNote 4.0 is invalid: {'; '.join(errors)}")
    markdown = render_generated_note(
        context.task,
        context.content_pack,
        note,
        asset_prefix=context.asset_prefix,
        template=template,
    )
    markdown_errors = validate_note_markdown_text(
        context.task_dir, markdown, context.content_pack
    )
    if markdown_errors:
        raise ValueError(
            "active GeneratedNote 4.0 cannot be rendered: " + "; ".join(markdown_errors)
        )
    bundle = active_note_path.parent
    _write_text(bundle / "note.md", markdown)
    metadata_path = bundle / "generation.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("active GeneratedNote 4.0 metadata is invalid") from error
    previous_count = metadata.get("render_count", 0)
    metadata["render_count"] = (
        previous_count + 1 if isinstance(previous_count, int) else 1
    )
    metadata["last_rendered_at"] = datetime.now(UTC).isoformat()
    _write_text(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2))
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


def _normalize_provider_v4_ocr_parents(
    note: GeneratedNoteV4,
    content_pack: ContentPack,
) -> tuple[GeneratedNoteV4, list[dict[str, object]]]:
    """Close only uniquely declared OCR-to-frame references from a provider response."""
    normalized = note.model_copy(deep=True)
    evidence_by_id = {item.id: item for item in content_pack.evidence}
    records: list[dict[str, object]] = []
    for candidate_path, statement in iter_factual_statement_entries(normalized):
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
            parent = evidence_by_id.get(evidence.frame_id)
            if parent is None or parent.kind != "frame":
                continue
            triggers_by_parent.setdefault(evidence.frame_id, []).append(evidence_id)

        for parent_frame_id, trigger_ids in triggers_by_parent.items():
            statement.evidence_ids.append(parent_frame_id)
            existing_ids.add(parent_frame_id)
            records.append(
                {
                    "kind": "v4_factual_ocr_parent",
                    "candidate_path": candidate_path,
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
    if require_v3 and isinstance(note, (GeneratedNote, GeneratedNoteV4)):
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
    if isinstance(note, (GeneratedNote, GeneratedNoteV4)):
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


_V4_AUDIT_DISCLAIMER = "模型辅助审验，不等于人工确认。"


def _estimate_v4_input_tokens(content_pack_json: str, template_json: str) -> int:
    """Use a deliberately conservative UTF-8 estimate before a paid request."""
    return _estimate_v4_payload_tokens(content_pack_json, template_json)


def _estimate_v4_payload_tokens(*payloads: str) -> int:
    """Apply the same conservative estimator to all paid V4 request bodies."""
    byte_count = sum(len(payload.encode("utf-8")) for payload in payloads) + 2_048
    return (byte_count + 1) // 2


def _repair_v4_provider_json(raw: str) -> tuple[str | None, list[str]]:
    """Repair one known model-only V4 syntax slip without changing any values.

    MiMo occasionally emits a bare object inside a semantic-block item, for example
    ``{"title": {...}, {"content": {...}}}``. This is a JSON syntax error, not a
    source-contract error. The repair only removes that anonymous wrapper (or the
    duplicated title-only wrapper), then the normal JSON/Pydantic/source validators
    still decide whether the candidate is eligible. The original response is always
    preserved separately by the caller.
    """
    if not re.search(r'"schema_version"\s*:\s*"4\.0"', raw[:512]):
        return None, []

    lines = raw.splitlines()
    normalizations: list[str] = []
    while True:
        changed = False
        for index in range(len(lines) - 1):
            closing = re.fullmatch(r"( *)},", lines[index])
            if closing is None or len(closing.group(1)) != 10:
                continue
            if lines[index + 1].strip() != "{":
                continue
            first_key_index = index + 2
            while first_key_index < len(lines) and not lines[first_key_index].strip():
                first_key_index += 1
            if first_key_index >= len(lines):
                continue
            first_key = re.fullmatch(
                r' {12}"(title|content)": \{', lines[first_key_index]
            )
            if first_key is None:
                continue

            anonymous_close: int | None = None
            brace_depth = 0
            for candidate_index in range(index + 1, len(lines)):
                brace_depth += _json_line_brace_delta(lines[candidate_index])
                if brace_depth == 0:
                    anonymous_close = candidate_index
                    break
            if anonymous_close is None:
                continue

            if first_key.group(1) == "title":
                property_start: int | None = None
                for candidate_index in range(index - 1, -1, -1):
                    if re.fullmatch(
                        r' {10}"(?:title|content)": \{', lines[candidate_index]
                    ):
                        property_start = candidate_index
                        break
                if property_start is None:
                    continue
                del lines[property_start : index + 1]
                anonymous_close -= index - property_start + 1
                del lines[anonymous_close]
                del lines[property_start]
                normalizations.append(
                    "merged anonymous V4 item object with duplicate title"
                )
            else:
                del lines[anonymous_close]
                del lines[index + 1]
                normalizations.append("merged anonymous V4 item object with content")
            changed = True
            break
        if not changed:
            break

    if not normalizations:
        return None, []
    return "\n".join(lines) + "\n", normalizations


def _json_line_brace_delta(line: str) -> int:
    """Count JSON object braces on one line while ignoring string contents."""
    delta = 0
    in_string = False
    escaped = False
    for character in line:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            delta += 1
        elif character == "}":
            delta -= 1
    return delta


def _run_v4_note_audit(
    directory: Path,
    review_mode: V4ReviewMode,
    auditor: NoteAuditor | None,
    content_pack: ContentPack,
    candidate: GeneratedNoteV4,
    content_pack_json: str,
    template_json: str,
    retain_debug_artifacts: bool = False,
) -> tuple[
    Literal["not_requested", "passed", "flagged", "unavailable"],
    dict[str, object] | None,
    int,
]:
    """Run at most one advisory audit and retain an honest local report."""
    if review_mode == "none":
        return "not_requested", None, 0
    if auditor is None:
        return (
            "unavailable",
            _v4_audit_report(
                "unavailable",
                provider=None,
                model=None,
                audit=None,
                errors=("NoteAudit provider is unavailable",),
            ),
            0,
        )
    candidate_json = candidate.model_dump_json()
    manifest_json = json.dumps(
        note_audit_statement_manifest(candidate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    safe_input_tokens = getattr(
        auditor, "safe_input_tokens", DEFAULT_NOTE_SAFE_INPUT_TOKENS
    )
    estimated_tokens = _estimate_v4_payload_tokens(
        content_pack_json,
        template_json,
        candidate_json,
        manifest_json,
    )
    if not isinstance(safe_input_tokens, int) or safe_input_tokens < 1:
        return (
            "unavailable",
            _v4_audit_report(
                "unavailable",
                provider=auditor.name,
                model=auditor.model,
                audit=None,
                errors=("NoteAudit safe input token budget is invalid",),
            ),
            0,
        )
    if estimated_tokens > safe_input_tokens:
        return (
            "unavailable",
            _v4_audit_report(
                "unavailable",
                provider=auditor.name,
                model=auditor.model,
                audit=None,
                errors=(
                    "NoteAudit input exceeds provider safe input budget: "
                    f"estimated {estimated_tokens}, budget {safe_input_tokens}",
                ),
            ),
            0,
        )
    try:
        raw = auditor.audit_note(
            content_pack_json,
            candidate_json,
            template_json,
            manifest_json,
        )
    except Exception as error:
        return (
            "unavailable",
            _v4_audit_report(
                "unavailable",
                provider=auditor.name,
                model=auditor.model,
                audit=None,
                errors=(f"NoteAudit failed: {type(error).__name__}",),
            ),
            1,
        )
    if retain_debug_artifacts:
        _write_text(directory / "note_audit.raw.txt", raw)
    try:
        audit = parse_note_audit(raw)
    except ValidationError:
        return (
            "unavailable",
            _v4_audit_report(
                "unavailable",
                provider=auditor.name,
                model=auditor.model,
                audit=None,
                errors=("NoteAudit response failed schema validation",),
            ),
            1,
        )
    audit_errors = tuple(validate_note_audit(audit, candidate))
    if audit_errors:
        return (
            "unavailable",
            _v4_audit_report(
                "unavailable",
                provider=auditor.name,
                model=auditor.model,
                audit=audit,
                errors=audit_errors,
            ),
            1,
        )
    status: Literal["passed", "flagged"] = audit.verdict
    return (
        status,
        _v4_audit_report(
            status,
            provider=auditor.name,
            model=auditor.model,
            audit=audit,
            errors=(),
        ),
        1,
    )


def _v4_audit_report(
    status: Literal["passed", "flagged", "unavailable"],
    *,
    provider: str | None,
    model: str | None,
    audit: NoteAudit | None,
    errors: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "status": status,
        "disclaimer": _V4_AUDIT_DISCLAIMER,
        "provider": provider,
        "model": model,
        "audit": audit.model_dump(mode="json") if audit is not None else None,
        "errors": list(errors),
    }


def _finish_v4_source_valid_bundle(
    temporary: Path,
    final: Path,
    context: _GenerationContext,
    run_id: str,
    provider: str,
    model: str,
    template: NoteTemplate,
    note: GeneratedNoteV4,
    *,
    review_mode: V4ReviewMode,
    auditor: NoteAuditor | None,
    model_call_count: int,
    content_pack_json: str,
    template_json: str,
    operation: Literal["generate", "external", "rerender"] = "generate",
    preserve_derived: bool = False,
    template_warnings: tuple[str, ...] = (),
    evidence_normalizations: list[dict[str, object]] | None = None,
    response_normalizations: list[str] | None = None,
    retain_debug_artifacts: bool = False,
) -> V4BundleResult:
    """Render and persist a deterministic source-valid candidate before activation."""
    render_task = (
        context.task if preserve_derived else _without_derived_artifacts(context.task)
    )
    try:
        markdown = render_generated_note(
            render_task,
            context.content_pack,
            note,
            asset_prefix=context.asset_prefix,
            template=template,
        )
    except Exception as error:
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            run_id,
            provider,
            model,
            template,
            review_mode,
            model_call_count,
            (f"V4 Markdown rendering failed: {type(error).__name__}",),
            candidate_status="candidate",
            operation=operation,
            template_warnings=template_warnings,
            evidence_normalizations=evidence_normalizations,
            response_normalizations=response_normalizations,
        )
    markdown_errors = tuple(
        validate_note_markdown_text(context.task_dir, markdown, context.content_pack)
    )
    if markdown_errors:
        return _finish_v4_failed_bundle(
            temporary,
            final,
            context,
            run_id,
            provider,
            model,
            template,
            review_mode,
            model_call_count,
            markdown_errors,
            candidate_status="candidate",
            operation=operation,
            template_warnings=template_warnings,
            evidence_normalizations=evidence_normalizations,
            response_normalizations=response_normalizations,
        )
    _write_text(temporary / "note.json", note.model_dump_json(indent=2))
    _write_text(temporary / "note.md", markdown)

    review_status, audit_report, audit_calls = _run_v4_note_audit(
        temporary,
        review_mode,
        auditor,
        context.content_pack,
        note,
        content_pack_json,
        template_json,
        retain_debug_artifacts,
    )
    model_call_count += audit_calls
    if audit_report is not None:
        _write_text(
            temporary / "note_audit.json",
            json.dumps(audit_report, ensure_ascii=False, indent=2) + "\n",
        )
    activation_allowed = review_mode != "gate" or (
        review_status == "passed" and not template_warnings
    )
    _write_v4_generation_metadata(
        temporary,
        context,
        run_id,
        provider,
        model,
        template,
        model_call_count=model_call_count,
        review_mode=review_mode,
        review_status=review_status,
        candidate_status="source_valid",
        source_validation_status="source_valid",
        activation_decision=("activate" if activation_allowed else "retain_candidate"),
        errors=(),
        operation=operation,
        template_warnings=template_warnings,
        evidence_normalizations=evidence_normalizations,
        response_normalizations=response_normalizations,
    )
    os.replace(temporary, final)
    return V4BundleResult(final, review_status, activation_allowed)


def _finish_v4_failed_bundle(
    temporary: Path,
    final: Path,
    context: _GenerationContext,
    run_id: str,
    provider: str,
    model: str,
    template: NoteTemplate,
    review_mode: V4ReviewMode,
    model_call_count: int,
    errors: tuple[str, ...],
    *,
    candidate_status: Literal["not_created", "candidate"],
    operation: Literal["generate", "external", "rerender"] = "generate",
    template_warnings: tuple[str, ...] | None = None,
    evidence_normalizations: list[dict[str, object]] | None = None,
    response_normalizations: list[str] | None = None,
) -> V4BundleResult:
    _write_v4_generation_metadata(
        temporary,
        context,
        run_id,
        provider,
        model,
        template,
        model_call_count=model_call_count,
        review_mode=review_mode,
        review_status="not_requested",
        candidate_status=candidate_status,
        source_validation_status="failed",
        activation_decision="not_eligible",
        errors=errors,
        operation=operation,
        template_warnings=template_warnings,
        evidence_normalizations=evidence_normalizations,
        response_normalizations=response_normalizations,
    )
    os.replace(temporary, final)
    raise NoteGenerationError(final, errors)


def _write_v4_generation_metadata(
    directory: Path,
    context: _GenerationContext,
    run_id: str,
    provider: str,
    model: str,
    template: NoteTemplate,
    *,
    model_call_count: int,
    review_mode: V4ReviewMode,
    review_status: Literal["not_requested", "passed", "flagged", "unavailable"],
    candidate_status: Literal["not_created", "candidate", "source_valid"],
    source_validation_status: Literal["failed", "source_valid"],
    activation_decision: Literal["not_eligible", "activate", "retain_candidate"],
    errors: tuple[str, ...],
    operation: Literal["generate", "external", "rerender"] = "generate",
    template_warnings: tuple[str, ...] | None = None,
    evidence_normalizations: list[dict[str, object]] | None = None,
    response_normalizations: list[str] | None = None,
) -> None:
    resolved_template_warnings = list(template_warnings or ())
    template_validation_status = (
        "not_evaluated"
        if template_warnings is None
        else ("flagged" if resolved_template_warnings else "passed")
    )
    payload = {
        "schema_version": "2.0",
        "note_schema_version": "4.0",
        "run_id": run_id,
        "attempt_id": context.task.active_attempt_id,
        "provider": provider,
        "model": model,
        "operation": operation,
        "content_pack_sha256": context.content_pack_sha256,
        "candidate_status": candidate_status,
        "source_validation_status": source_validation_status,
        "model_call_count": model_call_count,
        "template": {
            "template_id": template.template_id,
            "template_sha256": template_snapshot_sha256(template),
            "snapshot": "template.json",
        },
        "template_validation": {
            "status": template_validation_status,
            "warnings": resolved_template_warnings,
        },
        "review": {
            "mode": review_mode,
            "status": review_status,
            "disclaimer": _V4_AUDIT_DISCLAIMER,
        },
        "activation_decision": activation_decision,
        "errors": list(errors),
        "evidence_normalizations": evidence_normalizations or [],
        "response_normalizations": response_normalizations or [],
    }
    _write_text(
        directory / "generation.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _new_run_id(task: TaskRecord) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{task.task_id[-8:]}"
