"""Deterministic evidence projections for draft generation and citation audit."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from learnnest.models import ContentPack, Evidence, EvidenceKind
from learnnest.note_coverage import CoveragePlan, validate_coverage_plan
from learnnest.note_models import AnyGeneratedNote, GeneratedNote, ResourceShareNote
from learnnest.note_validation import (
    iter_factual_statement_entries,
    iter_factual_statements,
)


class _EvidenceScopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScopedEvidence(_EvidenceScopeModel):
    """The minimal raw projection of one evidence item permitted downstream."""

    id: str
    kind: EvidenceKind
    text: str | None
    start_ms: int | None
    end_ms: int | None
    frame_id: str | None
    related_evidence_ids: list[str]


class DraftScopeUnit(_EvidenceScopeModel):
    """An anonymous frozen source unit without planner prose."""

    unit_index: int = Field(ge=1)
    evidence: list[ScopedEvidence] = Field(min_length=1)


class DraftScopeUsedVisual(_EvidenceScopeModel):
    """A use decision with only the selected visual evidence projection."""

    visual_index: int = Field(ge=1)
    disposition: Literal["use"]
    unit_index: int = Field(ge=1)
    frame: ScopedEvidence
    supporting_ocr: list[ScopedEvidence]


class DraftScopeOmittedVisual(_EvidenceScopeModel):
    """An omit decision that deliberately reveals no frame content or rationale."""

    visual_index: int = Field(ge=1)
    disposition: Literal["omit"]


DraftScopeVisual = Annotated[
    DraftScopeUsedVisual | DraftScopeOmittedVisual,
    Field(discriminator="disposition"),
]


class DraftEvidenceScope(_EvidenceScopeModel):
    """The only source-evidence payload allowed into a draft provider."""

    schema_version: Literal["1.0"]
    task_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=1)
    units: list[DraftScopeUnit]
    visuals: list[DraftScopeVisual]
    allowlisted_evidence_ids: list[str]


class StatementCitationItemV10(_EvidenceScopeModel):
    """One isolated factual statement and only its local citation material."""

    candidate_path: str = Field(pattern=r"^/")
    statement_text: str = Field(min_length=1)
    statement_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: list[str]
    snippets: list[ScopedEvidence]
    structural_evidence_ids: list[str]


class StatementCitationPacketV10(_EvidenceScopeModel):
    """A candidate-local packet for a later statement citation audit."""

    schema_version: Literal["1.0"]
    task_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    statements: list[StatementCitationItemV10]


# Keep the original public names bound to the exact V1.0 models so existing
# bundles and their canonical hashes remain legacy-compatible.
StatementCitationItem = StatementCitationItemV10
StatementCitationPacket = StatementCitationPacketV10


class ProgramClause(BaseModel):
    """A program-owned clause locator, never computed by a model provider."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    clause_id: str = Field(pattern=r"^clause-[0-9]{4,}-[0-9]{4,}$")
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)


class StatementCitationItemV11(StatementCitationItemV10):
    """A statement plus its immutable, program-generated audit clause."""

    clauses: list[ProgramClause] = Field(min_length=1)


class StatementCitationPacketV11(_EvidenceScopeModel):
    """CitationAudit V1.1 packet with model-independent clause identifiers."""

    schema_version: Literal["1.1"]
    task_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    statements: list[StatementCitationItemV11]


class StatementCitationItemV12(StatementCitationItemV10):
    """A statement with deterministic sentence-level audit clauses."""

    clauses: list[ProgramClause] = Field(min_length=1)


class StatementCitationPacketV12(_EvidenceScopeModel):
    """CitationAudit V1.2 packet with program-owned sentence clause boundaries."""

    schema_version: Literal["1.2"]
    task_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    statements: list[StatementCitationItemV12]


AnyStatementCitationPacket = (
    StatementCitationPacketV10 | StatementCitationPacketV11 | StatementCitationPacketV12
)


def build_draft_evidence_scope(
    content_pack: ContentPack,
    plan: CoveragePlan,
) -> DraftEvidenceScope:
    """Project only frozen plan selections; never serialize planner prose."""
    plan_errors = validate_coverage_plan(plan, content_pack)
    if plan_errors:
        raise ValueError("; ".join(plan_errors))
    evidence_by_id = {evidence.id: evidence for evidence in content_pack.evidence}
    allowlisted_ids: list[str] = []

    def allow(evidence_id: str) -> None:
        if evidence_id not in evidence_by_id:
            raise ValueError(
                f"coverage plan selects unknown evidence id: {evidence_id}"
            )
        if evidence_id not in allowlisted_ids:
            allowlisted_ids.append(evidence_id)

    for unit in plan.units:
        for evidence_id in unit.source_evidence_ids:
            allow(evidence_id)
    for visual in plan.visuals:
        if visual.disposition == "use":
            allow(visual.frame_evidence_id)
            for evidence_id in visual.supporting_ocr_evidence_ids:
                allow(evidence_id)

    for evidence_id in list(allowlisted_ids):
        evidence = evidence_by_id[evidence_id]
        if evidence.kind == "ocr" and evidence.frame_id is not None:
            allow(evidence.frame_id)

    allowed_set = set(allowlisted_ids)
    unit_index_by_label = {
        unit.label: index for index, unit in enumerate(plan.units, start=1)
    }
    units = [
        DraftScopeUnit(
            unit_index=index,
            evidence=[
                _project_evidence(evidence_by_id[evidence_id], allowed_set)
                for evidence_id in unit.source_evidence_ids
            ],
        )
        for index, unit in enumerate(plan.units, start=1)
    ]
    visuals: list[DraftScopeVisual] = []
    for visual_index, visual in enumerate(plan.visuals, start=1):
        if visual.disposition == "omit":
            visuals.append(
                DraftScopeOmittedVisual(
                    visual_index=visual_index,
                    disposition="omit",
                )
            )
            continue
        if visual.unit_label is None or visual.unit_label not in unit_index_by_label:
            raise ValueError("coverage plan used visual has no resolvable unit")
        visuals.append(
            DraftScopeUsedVisual(
                visual_index=visual_index,
                disposition="use",
                unit_index=unit_index_by_label[visual.unit_label],
                frame=_project_evidence(
                    evidence_by_id[visual.frame_evidence_id], allowed_set
                ),
                supporting_ocr=[
                    _project_evidence(evidence_by_id[evidence_id], allowed_set)
                    for evidence_id in visual.supporting_ocr_evidence_ids
                ],
            )
        )

    return DraftEvidenceScope(
        schema_version="1.0",
        task_id=content_pack.task_id,
        source_fingerprint=content_pack.source_fingerprint,
        units=units,
        visuals=visuals,
        allowlisted_evidence_ids=allowlisted_ids,
    )


def validate_note_citations_against_scope(
    note: AnyGeneratedNote,
    scope: DraftEvidenceScope,
) -> list[str]:
    """Reject citations outside the frozen draft scope without replacing V3 validation."""
    errors: list[str] = []
    if note.task_id != scope.task_id:
        errors.append("generated note task_id does not match draft evidence scope")
    if note.source_fingerprint != scope.source_fingerprint:
        errors.append(
            "generated note source_fingerprint does not match draft evidence scope"
        )

    candidates: list[str] = []
    if not isinstance(note, GeneratedNote):
        candidates.extend(note.classification_evidence_ids)
    for statement in iter_factual_statements(note):
        candidates.extend(statement.evidence_ids)
    if isinstance(note, ResourceShareNote):
        for resource in note.resources:
            if resource.locator is not None:
                candidates.extend(resource.locator.evidence_ids)

    allowed_ids = set(scope.allowlisted_evidence_ids)
    reported_ids: set[str] = set()
    for evidence_id in candidates:
        if evidence_id not in allowed_ids and evidence_id not in reported_ids:
            errors.append(
                f"generated note cites evidence outside draft scope: {evidence_id}"
            )
            reported_ids.add(evidence_id)
    return errors


def build_statement_citation_packet(
    note: AnyGeneratedNote,
    content_pack: ContentPack,
) -> StatementCitationPacketV12:
    """Build V1.2 packets with program-owned sentence clause boundaries."""
    _require_note_identity_against_content_pack(note, content_pack)
    evidence_by_id = {evidence.id: evidence for evidence in content_pack.evidence}
    statements: list[StatementCitationItemV11] = []
    for statement_index, (candidate_path, statement) in enumerate(
        iter_factual_statement_entries(note), start=1
    ):
        structural_ids = _structural_frame_ids(statement.evidence_ids, evidence_by_id)
        visible_ids = set(statement.evidence_ids).union(structural_ids)
        snippets: list[ScopedEvidence] = []
        for evidence_id in statement.evidence_ids:
            evidence = _require_evidence(evidence_id, evidence_by_id)
            if evidence.kind in {"transcript", "ocr"}:
                snippets.append(_project_evidence(evidence, visible_ids))
            elif evidence.kind not in {"frame"}:
                raise ValueError(
                    "statement citation packet cannot use non-source evidence: "
                    f"{evidence_id}"
                )
        statements.append(
            StatementCitationItemV12(
                candidate_path=candidate_path,
                statement_text=statement.text,
                statement_sha256=_sha256_text(statement.text),
                evidence_ids=list(statement.evidence_ids),
                snippets=snippets,
                structural_evidence_ids=structural_ids,
                clauses=_build_program_sentence_clauses(
                    statement_index,
                    statement.text,
                ),
            )
        )
    return StatementCitationPacketV12(
        schema_version="1.2",
        task_id=note.task_id,
        source_fingerprint=note.source_fingerprint,
        candidate_sha256=_sha256_json(note.model_dump(mode="json")),
        statements=statements,
    )


def parse_statement_citation_packet(
    value: str | bytes | bytearray | Mapping[str, object] | AnyStatementCitationPacket,
) -> AnyStatementCitationPacket:
    """Parse a supported packet version without silently upgrading legacy data."""
    if isinstance(
        value,
        (
            StatementCitationPacketV10,
            StatementCitationPacketV11,
            StatementCitationPacketV12,
        ),
    ):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("statement citation packet is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise ValueError("statement citation packet must be a JSON object")

    schema_version = value.get("schema_version")
    if schema_version == "1.0":
        return StatementCitationPacketV10.model_validate(value)
    if schema_version == "1.1":
        return StatementCitationPacketV11.model_validate(value)
    if schema_version == "1.2":
        return StatementCitationPacketV12.model_validate(value)
    raise ValueError(
        f"unsupported statement citation packet schema_version: {schema_version!r}"
    )


def parse_statement_citation_packet_v11(
    value: str | bytes | bytearray | Mapping[str, object] | AnyStatementCitationPacket,
) -> StatementCitationPacketV11:
    """Parse only the current V1.1 packet boundary used for new audits."""
    packet = parse_statement_citation_packet(value)
    if not isinstance(packet, StatementCitationPacketV11):
        raise ValueError("statement citation packet expected schema_version 1.1")
    return packet


def parse_statement_citation_packet_v12(
    value: str | bytes | bytearray | Mapping[str, object] | AnyStatementCitationPacket,
) -> StatementCitationPacketV12:
    """Parse only the current V1.2 packet boundary used for new audits."""
    packet = parse_statement_citation_packet(value)
    if not isinstance(packet, StatementCitationPacketV12):
        raise ValueError("statement citation packet expected schema_version 1.2")
    return packet


def validate_statement_citation_packet_v11(
    packet: StatementCitationPacketV11,
) -> list[str]:
    """Return deterministic V1.1 packet-integrity errors without candidate access."""
    errors: list[str] = []
    seen_paths: set[str] = set()
    seen_clause_ids: set[str] = set()
    for statement_index, statement in enumerate(packet.statements, start=1):
        if statement.candidate_path in seen_paths:
            errors.append(
                "statement citation packet has duplicate candidate path: "
                f"{statement.candidate_path}"
            )
        seen_paths.add(statement.candidate_path)

        if statement.statement_sha256 != _sha256_text(statement.statement_text):
            errors.append(
                "statement citation packet statement_sha256 does not match statement "
                f"text: {statement.candidate_path}"
            )

        expected_clause_id = f"clause-{statement_index:04d}-0001"
        if len(statement.clauses) != 1:
            errors.append(
                "statement citation packet V1.1 must contain exactly one full-statement "
                f"clause: {statement.candidate_path}"
            )
        for clause in statement.clauses:
            if clause.clause_id in seen_clause_ids:
                errors.append(
                    "statement citation packet V1.1 has duplicate clause_id: "
                    f"{clause.clause_id}"
                )
            seen_clause_ids.add(clause.clause_id)
            if clause.clause_id != expected_clause_id:
                errors.append(
                    "statement citation packet V1.1 clause_id does not match statement "
                    f"order: {clause.clause_id}"
                )
            if (
                clause.start != 0
                or clause.end != len(statement.statement_text)
                or clause.text != statement.statement_text
            ):
                errors.append(
                    "statement citation packet V1.1 clause must cover its full statement: "
                    f"{clause.clause_id}"
                )
    return errors


def validate_statement_citation_packet_v12(
    packet: StatementCitationPacketV12,
) -> list[str]:
    """Validate deterministic V1.2 sentence clauses without candidate access."""
    errors: list[str] = []
    seen_paths: set[str] = set()
    seen_clause_ids: set[str] = set()
    for statement_index, statement in enumerate(packet.statements, start=1):
        if statement.candidate_path in seen_paths:
            errors.append(
                "statement citation packet has duplicate candidate path: "
                f"{statement.candidate_path}"
            )
        seen_paths.add(statement.candidate_path)

        if statement.statement_sha256 != _sha256_text(statement.statement_text):
            errors.append(
                "statement citation packet statement_sha256 does not match statement "
                f"text: {statement.candidate_path}"
            )

        expected_clauses = _build_program_sentence_clauses(
            statement_index,
            statement.statement_text,
        )
        if statement.clauses != expected_clauses:
            errors.append(
                "statement citation packet V1.2 clauses do not match "
                "program-generated sentence boundaries: "
                f"{statement.candidate_path}"
            )
        for clause in statement.clauses:
            if clause.clause_id in seen_clause_ids:
                errors.append(
                    "statement citation packet V1.2 has duplicate clause_id: "
                    f"{clause.clause_id}"
                )
            seen_clause_ids.add(clause.clause_id)
    return errors


def _build_program_sentence_clauses(
    statement_index: int,
    text: str,
) -> list[ProgramClause]:
    """Split a statement at deterministic sentence boundaries without model input."""
    clauses: list[ProgramClause] = []
    start = 0
    for index, character in enumerate(text):
        if index < start:
            continue
        if not _is_program_sentence_boundary(text, index, character):
            continue
        end = index + 1
        while end < len(text) and text[end].isspace():
            end += 1
        clauses.append(
            ProgramClause(
                clause_id=f"clause-{statement_index:04d}-{len(clauses) + 1:04d}",
                start=start,
                end=end,
                text=text[start:end],
            )
        )
        start = end
    if start < len(text):
        clauses.append(
            ProgramClause(
                clause_id=f"clause-{statement_index:04d}-{len(clauses) + 1:04d}",
                start=start,
                end=len(text),
                text=text[start:],
            )
        )
    return clauses


def _is_program_sentence_boundary(text: str, index: int, character: str) -> bool:
    if character in {"。", "！", "？", "；", "!", "?", ";", "\n"}:
        return True
    return character == "." and (index == len(text) - 1 or text[index + 1].isspace())


def _project_evidence(
    evidence: Evidence,
    allowed_ids: set[str],
) -> ScopedEvidence:
    return ScopedEvidence(
        id=evidence.id,
        kind=evidence.kind,
        text=evidence.text,
        start_ms=evidence.start_ms,
        end_ms=evidence.end_ms,
        frame_id=(evidence.frame_id if evidence.frame_id in allowed_ids else None),
        related_evidence_ids=[
            evidence_id
            for evidence_id in evidence.related_evidence_ids
            if evidence_id in allowed_ids
        ],
    )


def _require_note_identity_against_content_pack(
    note: AnyGeneratedNote,
    content_pack: ContentPack,
) -> None:
    if note.task_id != content_pack.task_id:
        raise ValueError("generated note task_id does not match content pack")
    if note.source_fingerprint != content_pack.source_fingerprint:
        raise ValueError(
            "generated note source_fingerprint does not match content pack"
        )


def _require_evidence(
    evidence_id: str, evidence_by_id: dict[str, Evidence]
) -> Evidence:
    evidence = evidence_by_id.get(evidence_id)
    if evidence is None:
        raise ValueError(
            f"statement citation packet references unknown evidence id: {evidence_id}"
        )
    return evidence


def _structural_frame_ids(
    evidence_ids: list[str],
    evidence_by_id: dict[str, Evidence],
) -> list[str]:
    frame_ids: list[str] = []
    for evidence_id in evidence_ids:
        evidence = _require_evidence(evidence_id, evidence_by_id)
        if evidence.kind == "frame":
            frame_id = evidence.id
        elif evidence.kind == "ocr":
            frame_id = evidence.frame_id
        else:
            frame_id = None
        if frame_id is not None and frame_id not in frame_ids:
            frame_ids.append(frame_id)
    return frame_ids


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(serialized)
