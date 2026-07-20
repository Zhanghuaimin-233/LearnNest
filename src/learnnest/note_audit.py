"""Optional model-assisted semantic audit for GeneratedNote 4.0 candidates."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from learnnest.note_models import AnyGeneratedNote
from learnnest.note_validation import iter_factual_statement_entries

NonEmptyString = Annotated[str, Field(min_length=1)]
JsonPointer = Annotated[
    str,
    Field(min_length=1, pattern=r"^(/([^~/]|~0|~1)*)+$"),
]
AuditAssessment = Literal["supported", "ambiguous", "overreaching"]
AuditVerdict = Literal["passed", "flagged"]
AuditIssueCategory = Literal[
    "semantic_support",
    "ambiguity",
    "over_inference",
    "structure",
    "readability",
    "learning_value",
]
AuditSeverity = Literal["low", "medium", "high"]


class _AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoteAuditStatement(_AuditModel):
    """One advisory support assessment for a program-owned candidate path."""

    candidate_path: JsonPointer
    assessment: AuditAssessment
    rationale: NonEmptyString


class NoteAuditIssue(_AuditModel):
    """An advisory quality signal, never a replacement for evidence validation."""

    category: AuditIssueCategory
    severity: AuditSeverity
    candidate_paths: list[JsonPointer] = Field(default_factory=list)
    rationale: NonEmptyString


class NoteAudit(_AuditModel):
    """A complete audit of one immutable V4 candidate."""

    schema_version: Literal["1.0"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    candidate_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    verdict: AuditVerdict
    statement_audits: list[NoteAuditStatement] = Field(min_length=1)
    issues: list[NoteAuditIssue]

    @model_validator(mode="after")
    def verdict_matches_advisory_signals(self) -> NoteAudit:
        has_flag = bool(self.issues) or any(
            statement.assessment != "supported" for statement in self.statement_audits
        )
        if self.verdict == "passed" and has_flag:
            raise ValueError("passed audit cannot contain advisory flags")
        if self.verdict == "flagged" and not has_flag:
            raise ValueError("flagged audit requires at least one advisory flag")
        return self


NOTE_AUDIT_ADAPTER = TypeAdapter(NoteAudit)


def note_audit_json_schema() -> dict[str, object]:
    """Return the strict schema sent to an optional V4 auditor."""
    return NOTE_AUDIT_ADAPTER.json_schema()


def parse_note_audit(raw_text: str) -> NoteAudit:
    """Parse a single strict JSON audit response without repair or retries."""
    return NOTE_AUDIT_ADAPTER.validate_json(raw_text)


def candidate_sha256(candidate: AnyGeneratedNote) -> str:
    """Bind an audit to canonical candidate JSON, not mutable raw response text."""
    payload = json.dumps(
        candidate.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def note_audit_statement_manifest(
    candidate: AnyGeneratedNote,
) -> list[dict[str, object]]:
    """Expose only candidate facts and their citations in stable program order."""
    return [
        {
            "candidate_path": path,
            "text": statement.text,
            "evidence_ids": list(statement.evidence_ids),
        }
        for path, statement in iter_factual_statement_entries(candidate)
    ]


def validate_note_audit(audit: NoteAudit, candidate: AnyGeneratedNote) -> list[str]:
    """Verify identity and candidate coverage without judging semantic opinions."""
    errors: list[str] = []
    if audit.task_id != candidate.task_id:
        errors.append("note audit task_id does not match candidate")
    if audit.source_fingerprint != candidate.source_fingerprint:
        errors.append("note audit source_fingerprint does not match candidate")
    if audit.candidate_sha256 != candidate_sha256(candidate):
        errors.append("note audit candidate_sha256 does not match candidate")
    expected_paths = [path for path, _ in iter_factual_statement_entries(candidate)]
    actual_paths = [item.candidate_path for item in audit.statement_audits]
    if actual_paths != expected_paths:
        errors.append("note audit statement paths do not match candidate manifest")
    expected = set(expected_paths)
    for issue in audit.issues:
        for path in issue.candidate_paths:
            if path not in expected:
                errors.append("note audit issue path does not resolve: " + path)
    return errors
