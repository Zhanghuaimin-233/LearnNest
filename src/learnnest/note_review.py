"""Independent semantic review contract for provider-generated notes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from learnnest.models import ContentPack
from learnnest.note_models import (
    AnyGeneratedNote,
    ConceptExplanationNote,
    GeneratedNote,
    NoteStatement,
    PracticalTutorialNote,
    ResourceShareNote,
)

NonEmptyString = Annotated[str, Field(min_length=1)]
JsonPointer = Annotated[
    str,
    Field(min_length=1, pattern=r"^(/([^~/]|~0|~1)*)+$"),
]
CoverageStatus = Literal["covered", "missing", "shallow"]
ReviewVerdict = Literal["approve", "reject"]
IssueCategory = Literal[
    "unsupported_or_noisy_claim",
    "evidence_misalignment",
    "semantic_duplication",
    "visual_omission_or_misplacement",
    "ai_supplement_repetition",
]
StatementAuditStatus = Literal["aligned", "misaligned"]


class _ReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CoverageUnit(_ReviewModel):
    """One independently useful central unit reconstructed from source evidence."""

    label: NonEmptyString
    source_evidence_ids: list[NonEmptyString] = Field(
        min_length=1,
        max_length=3,
        description=(
            "One to three exact frozen source anchors copied from CoveragePlan 1.1."
        ),
    )
    candidate_paths: list[JsonPointer]
    status: CoverageStatus
    rationale: NonEmptyString

    @model_validator(mode="after")
    def candidate_paths_match_status(self) -> CoverageUnit:
        if self.status != "missing" and not self.candidate_paths:
            raise ValueError(
                "covered or shallow coverage unit requires a candidate path"
            )
        return self


class ReviewIssue(_ReviewModel):
    """One grounded cross-unit quality problem in the candidate note."""

    category: IssueCategory
    candidate_paths: list[JsonPointer]
    source_evidence_ids: list[NonEmptyString] = Field(min_length=1)
    rationale: NonEmptyString


class StatementAudit(_ReviewModel):
    """One explicit evidence-alignment decision for a factual statement."""

    candidate_path: JsonPointer
    status: StatementAuditStatus
    rationale: NonEmptyString


class NoteReview(_ReviewModel):
    """Version 1.1 fail-closed semantic verdict for one frozen candidate note."""

    schema_version: Literal["1.1"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    verdict: ReviewVerdict
    coverage_units: list[CoverageUnit] = Field(min_length=1)
    statement_audits: list[StatementAudit] = Field(min_length=1)
    issues: list[ReviewIssue]

    @model_validator(mode="after")
    def verdict_matches_findings(self) -> NoteReview:
        has_failure = (
            bool(self.issues)
            or any(unit.status != "covered" for unit in self.coverage_units)
            or any(audit.status != "aligned" for audit in self.statement_audits)
        )
        if self.verdict == "approve" and has_failure:
            raise ValueError("approve review cannot contain failed findings")
        if self.verdict == "reject" and not has_failure:
            raise ValueError("reject review requires at least one failed finding")
        return self


NOTE_REVIEW_ADAPTER = TypeAdapter(NoteReview)


def generated_note_review_json_schema() -> dict[str, object]:
    """Return the strict JSON schema sent to the independent reviewer."""
    return NOTE_REVIEW_ADAPTER.json_schema()


def parse_note_review(raw_text: str) -> NoteReview:
    """Parse one reviewer response without accepting prose or extra fields."""
    return NOTE_REVIEW_ADAPTER.validate_json(raw_text)


def validate_note_review(
    review: NoteReview,
    content_pack: ContentPack,
    candidate: AnyGeneratedNote,
) -> list[str]:
    """Validate only deterministic identity, evidence, and pointer consistency."""
    errors: list[str] = []
    if review.task_id != content_pack.task_id:
        errors.append("note review task_id does not match content pack")
    if review.source_fingerprint != content_pack.source_fingerprint:
        errors.append("note review source_fingerprint does not match content pack")

    evidence_by_id = {item.id: item for item in content_pack.evidence}
    candidate_json = candidate.model_dump(mode="json")
    evidence_lists: list[list[str]] = [
        unit.source_evidence_ids for unit in review.coverage_units
    ] + [issue.source_evidence_ids for issue in review.issues]
    candidate_paths = [
        path for unit in review.coverage_units for path in unit.candidate_paths
    ] + [path for issue in review.issues for path in issue.candidate_paths]
    statement_entries = _factual_statement_entries(candidate)
    statement_paths = {path for path, _ in statement_entries}

    seen_unknown: set[str] = set()
    seen_ai_supplement: set[str] = set()
    for evidence_ids in evidence_lists:
        for duplicate in _duplicates(evidence_ids):
            errors.append(f"note review has duplicate source evidence id: {duplicate}")
        for evidence_id in evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None and evidence_id not in seen_unknown:
                errors.append(
                    f"note review has unknown source evidence id: {evidence_id}"
                )
                seen_unknown.add(evidence_id)
            elif (
                evidence is not None
                and evidence.kind == "ai_supplement"
                and evidence_id not in seen_ai_supplement
            ):
                errors.append(
                    f"note review cannot use AI supplement evidence: {evidence_id}"
                )
                seen_ai_supplement.add(evidence_id)

    for path in dict.fromkeys(candidate_paths):
        if not _json_pointer_resolves(candidate_json, path):
            errors.append(f"note review candidate path does not resolve: {path}")
    for unit in review.coverage_units:
        if unit.status == "missing":
            continue
        for path in unit.candidate_paths:
            if path not in statement_paths:
                errors.append(
                    "note review "
                    f"{unit.status} candidate path is not a factual statement: {path}"
                )

    expected_audit_paths = [path for path, _ in statement_entries]
    actual_audit_paths = [audit.candidate_path for audit in review.statement_audits]
    if actual_audit_paths != expected_audit_paths:
        errors.append("note review statement audits do not match candidate manifest")
    return errors


def build_statement_manifest(candidate: AnyGeneratedNote) -> list[dict[str, object]]:
    """Build the exact factual-statement manifest sent to the reviewer."""
    return [
        {
            "candidate_path": path,
            "text": statement.text,
            "evidence_ids": list(statement.evidence_ids),
        }
        for path, statement in _factual_statement_entries(candidate)
    ]


def _factual_statement_entries(
    note: AnyGeneratedNote,
) -> list[tuple[str, NoteStatement]]:
    entries: list[tuple[str, NoteStatement]] = []

    def add(path: str, statement: NoteStatement) -> None:
        entries.append((path, statement))

    if isinstance(note, GeneratedNote):
        add("/audience", note.audience)
        add("/summary", note.summary)
        for index, statement in enumerate(note.key_points):
            add(f"/key_points/{index}", statement)
        for index, step in enumerate(note.steps):
            add(
                f"/steps/{index}",
                NoteStatement(text=step.text, evidence_ids=step.evidence_ids),
            )
        for index, statement in enumerate(note.cautions):
            add(f"/cautions/{index}", statement)
        return entries

    add("/summary", note.summary)
    if isinstance(note, ConceptExplanationNote):
        if note.background is not None:
            add("/background", note.background)
        for index, concept in enumerate(note.concepts):
            add(f"/concepts/{index}/explanation", concept.explanation)
        for index, statement in enumerate(note.relationships):
            add(f"/relationships/{index}", statement)
        for index, statement in enumerate(note.misconceptions):
            add(f"/misconceptions/{index}", statement)
        if note.review is not None:
            add("/review", note.review)
        return entries

    if isinstance(note, ResourceShareNote):
        for index, resource in enumerate(note.resources):
            prefix = f"/resources/{index}"
            add(f"{prefix}/name", resource.name)
            add(f"{prefix}/value", resource.value)
            if resource.suitable_for is not None:
                add(f"{prefix}/suitable_for", resource.suitable_for)
            if resource.access_or_usage is not None:
                add(f"{prefix}/access_or_usage", resource.access_or_usage)
            for limitation_index, statement in enumerate(resource.limitations):
                add(f"{prefix}/limitations/{limitation_index}", statement)
        for index, statement in enumerate(note.reminders):
            add(f"/reminders/{index}", statement)
        return entries

    if isinstance(note, PracticalTutorialNote):
        add("/goal", note.goal)
        for index, statement in enumerate(note.prerequisites):
            add(f"/prerequisites/{index}", statement)
        for index, step in enumerate(note.steps):
            prefix = f"/steps/{index}"
            add(f"{prefix}/action", step.action)
            if step.expected_result is not None:
                add(f"{prefix}/expected_result", step.expected_result)
        for index, item in enumerate(note.troubleshooting):
            prefix = f"/troubleshooting/{index}"
            add(f"{prefix}/symptom", item.symptom)
            add(f"{prefix}/resolution", item.resolution)
        for index, statement in enumerate(note.completion_checks):
            add(f"/completion_checks/{index}", statement)
        for index, statement in enumerate(note.cautions):
            add(f"/cautions/{index}", statement)
    return entries


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def _json_pointer_resolves(document: object, pointer: str) -> bool:
    if not pointer.startswith("/"):
        return False
    current: Any = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                return False
            current = current[token]
            continue
        if isinstance(current, list):
            try:
                index = int(token)
            except ValueError:
                return False
            if index < 0 or index >= len(current) or token != str(index):
                return False
            current = current[index]
            continue
        return False
    return True
