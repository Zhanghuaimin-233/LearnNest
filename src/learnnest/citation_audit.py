"""Strict local contracts for statement-local citation entailment audits."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from learnnest.note_evidence_scope import (
    StatementCitationItem,
    StatementCitationPacket,
    StatementCitationItemV11,
    StatementCitationPacketV11,
    StatementCitationItemV12,
    StatementCitationPacketV12,
    validate_statement_citation_packet_v11,
    validate_statement_citation_packet_v12,
)

NonEmptyString = Annotated[str, Field(min_length=1)]
JsonPointer = Annotated[
    str,
    Field(min_length=1, pattern=r"^(/([^~/]|~0|~1)*)+$"),
]
Sha256 = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
ClauseAuditStatus = Literal["entailed", "unsupported", "ambiguous"]
CitationAuditVerdict = Literal["approve", "reject"]
ClauseReasonCode = Literal[
    "direct",
    "missing_direct_support",
    "ambiguous_wording",
]
ClauseId = Annotated[
    str,
    Field(min_length=1, pattern=r"^clause-[0-9]{4,}-[0-9]{4,}$"),
]
_JSON_POINTER_PATTERN = re.compile(r"^(/([^~/]|~0|~1)*)+$")
_EVIDENCE_ID_PATTERN = re.compile(r"^(tr|fr|ocr)_[0-9]{4,}$")


class _CitationAuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClauseAudit(_CitationAuditModel):
    """An exact candidate substring and its local citation decision."""

    candidate_path: JsonPointer
    statement_sha256: Sha256
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    status: ClauseAuditStatus
    support_evidence_ids: list[NonEmptyString]
    reason_code: ClauseReasonCode

    @model_validator(mode="after")
    def _validate_status_contract(self) -> ClauseAudit:
        if self.end <= self.start:
            raise ValueError("clause audit end must be greater than start")

        duplicate_support_id = _first_duplicate(self.support_evidence_ids)
        if duplicate_support_id is not None:
            raise ValueError(
                "entailed clause audit has duplicate support evidence id: "
                f"{duplicate_support_id}"
            )

        expected_reason = {
            "entailed": "direct",
            "unsupported": "missing_direct_support",
            "ambiguous": "ambiguous_wording",
        }[self.status]
        if self.reason_code != expected_reason:
            raise ValueError(
                f"clause audit reason_code does not match status: {self.status}"
            )
        if self.status == "entailed" and not self.support_evidence_ids:
            raise ValueError("entailed clause audit requires support evidence ids")
        if self.status != "entailed" and self.support_evidence_ids:
            raise ValueError(
                "non-entailed clause audit cannot declare support evidence ids"
            )
        return self


class CitationAudit(_CitationAuditModel):
    """Versioned, fail-closed verdict over one statement citation packet."""

    schema_version: Literal["1.0"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    candidate_sha256: Sha256
    packet_sha256: Sha256
    verdict: CitationAuditVerdict
    clauses: list[ClauseAudit] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_verdict_contract(self) -> CitationAudit:
        has_non_entailed_clause = any(
            clause.status != "entailed" for clause in self.clauses
        )
        if self.verdict == "approve" and has_non_entailed_clause:
            raise ValueError(
                "approve citation audit cannot contain non-entailed clauses"
            )
        if self.verdict == "reject" and not has_non_entailed_clause:
            raise ValueError(
                "reject citation audit requires at least one non-entailed clause"
            )
        return self


ClauseAuditV10 = ClauseAudit
CitationAuditV10 = CitationAudit


class ClauseAuditV11(_CitationAuditModel):
    """A program-identified full-statement clause decision.

    V1.1 deliberately excludes candidate paths, hashes, substring text, and
    offsets from model output.  Those bindings are supplied by the local
    statement citation packet instead.
    """

    clause_id: ClauseId
    status: ClauseAuditStatus
    support_evidence_ids: list[NonEmptyString]
    reason_code: ClauseReasonCode

    @model_validator(mode="after")
    def _validate_status_contract(self) -> ClauseAuditV11:
        duplicate_support_id = _first_duplicate(self.support_evidence_ids)
        if duplicate_support_id is not None:
            raise ValueError(
                "entailed clause audit has duplicate support evidence id: "
                f"{duplicate_support_id}"
            )

        expected_reason = {
            "entailed": "direct",
            "unsupported": "missing_direct_support",
            "ambiguous": "ambiguous_wording",
        }[self.status]
        if self.reason_code != expected_reason:
            raise ValueError(
                f"clause audit reason_code does not match status: {self.status}"
            )
        if self.status == "entailed" and not self.support_evidence_ids:
            raise ValueError("entailed clause audit requires support evidence ids")
        if self.status != "entailed" and self.support_evidence_ids:
            raise ValueError(
                "non-entailed clause audit cannot declare support evidence ids"
            )
        return self


class CitationAuditV11(_CitationAuditModel):
    """Versioned V1.1 verdict bound to program-generated clause identities."""

    schema_version: Literal["1.1"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    candidate_sha256: Sha256
    packet_sha256: Sha256
    verdict: CitationAuditVerdict
    clauses: list[ClauseAuditV11] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_verdict_contract(self) -> CitationAuditV11:
        has_non_entailed_clause = any(
            clause.status != "entailed" for clause in self.clauses
        )
        if self.verdict == "approve" and has_non_entailed_clause:
            raise ValueError(
                "approve citation audit cannot contain non-entailed clauses"
            )
        if self.verdict == "reject" and not has_non_entailed_clause:
            raise ValueError(
                "reject citation audit requires at least one non-entailed clause"
            )
        return self


class ClauseAuditV12(_CitationAuditModel):
    """A program-identified sentence-clause decision for CitationAudit V1.2."""

    clause_id: ClauseId
    status: ClauseAuditStatus
    support_evidence_ids: list[NonEmptyString]
    reason_code: ClauseReasonCode

    @model_validator(mode="after")
    def _validate_status_contract(self) -> ClauseAuditV12:
        duplicate_support_id = _first_duplicate(self.support_evidence_ids)
        if duplicate_support_id is not None:
            raise ValueError(
                "entailed clause audit has duplicate support evidence id: "
                f"{duplicate_support_id}"
            )

        expected_reason = {
            "entailed": "direct",
            "unsupported": "missing_direct_support",
            "ambiguous": "ambiguous_wording",
        }[self.status]
        if self.reason_code != expected_reason:
            raise ValueError(
                f"clause audit reason_code does not match status: {self.status}"
            )
        if self.status == "entailed" and not self.support_evidence_ids:
            raise ValueError("entailed clause audit requires support evidence ids")
        if self.status != "entailed" and self.support_evidence_ids:
            raise ValueError(
                "non-entailed clause audit cannot declare support evidence ids"
            )
        return self


class CitationAuditV12(_CitationAuditModel):
    """Versioned V1.2 verdict bound to program-owned sentence clauses."""

    schema_version: Literal["1.2"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    candidate_sha256: Sha256
    packet_sha256: Sha256
    verdict: CitationAuditVerdict
    clauses: list[ClauseAuditV12] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_verdict_contract(self) -> CitationAuditV12:
        has_non_entailed_clause = any(
            clause.status != "entailed" for clause in self.clauses
        )
        if self.verdict == "approve" and has_non_entailed_clause:
            raise ValueError(
                "approve citation audit cannot contain non-entailed clauses"
            )
        if self.verdict == "reject" and not has_non_entailed_clause:
            raise ValueError(
                "reject citation audit requires at least one non-entailed clause"
            )
        return self


CITATION_AUDIT_ADAPTER = TypeAdapter(CitationAudit)
CITATION_AUDIT_V11_ADAPTER = TypeAdapter(CitationAuditV11)
CITATION_AUDIT_V12_ADAPTER = TypeAdapter(CitationAuditV12)


def generated_citation_audit_json_schema() -> dict[str, object]:
    """Return the strict JSON schema sent to a cited-only CitationAuditor."""
    return CITATION_AUDIT_ADAPTER.json_schema()


def generated_citation_audit_v11_json_schema() -> dict[str, object]:
    """Return the strict V1.1 schema for program-owned clause identities."""
    return CITATION_AUDIT_V11_ADAPTER.json_schema()


def generated_citation_audit_v12_json_schema() -> dict[str, object]:
    """Return the strict V1.2 schema for program-owned sentence clauses."""
    return CITATION_AUDIT_V12_ADAPTER.json_schema()


def parse_citation_audit(raw_text: str) -> CitationAudit:
    """Accept exactly one strict CitationAudit JSON object and no surrounding prose."""
    return CITATION_AUDIT_ADAPTER.validate_json(raw_text)


def parse_citation_audit_v11(raw_text: str) -> CitationAuditV11:
    """Accept exactly one strict CitationAudit 1.1 JSON object."""
    return CITATION_AUDIT_V11_ADAPTER.validate_json(raw_text)


def parse_citation_audit_v12(raw_text: str) -> CitationAuditV12:
    """Accept exactly one strict CitationAudit 1.2 JSON object."""
    return CITATION_AUDIT_V12_ADAPTER.validate_json(raw_text)


def statement_citation_packet_sha256(
    packet: (
        StatementCitationPacket
        | StatementCitationPacketV11
        | StatementCitationPacketV12
    ),
) -> str:
    """Return the stable SHA used to bind an audit to its exact local packet."""
    serialized = json.dumps(
        packet.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(serialized)


def validate_citation_audit(
    audit: CitationAudit,
    packet: StatementCitationPacket,
) -> list[str]:
    """Return deterministic fail-closed errors without accessing a full candidate."""
    if not isinstance(audit, CitationAudit) or not isinstance(
        packet, StatementCitationPacket
    ):
        return [
            "citation audit v1.0 requires CitationAudit and StatementCitationPacket"
        ]

    errors = _validate_statement_citation_packet_integrity(packet)
    _validate_identity(audit, packet, errors)

    statements_by_path: dict[str, StatementCitationItem] = {}
    expected_paths: list[str] = []
    for statement in packet.statements:
        if statement.candidate_path in statements_by_path:
            errors.append(
                "statement citation packet has duplicate candidate path: "
                f"{statement.candidate_path}"
            )
            continue
        statements_by_path[statement.candidate_path] = statement
        expected_paths.append(statement.candidate_path)

    clauses_by_path: dict[str, list[ClauseAudit]] = {
        path: [] for path in expected_paths
    }
    actual_path_blocks: list[str] = []
    previous_path: str | None = None
    seen_path_blocks: set[str] = set()
    for clause in audit.clauses:
        statement = statements_by_path.get(clause.candidate_path)
        if statement is None:
            errors.append(
                "citation audit clause references unknown candidate path: "
                f"{clause.candidate_path}"
            )
        else:
            clauses_by_path[clause.candidate_path].append(clause)
            if clause.statement_sha256 != statement.statement_sha256:
                errors.append(
                    "citation audit clause statement_sha256 does not match packet: "
                    f"{clause.candidate_path}"
                )

        if clause.candidate_path != previous_path:
            actual_path_blocks.append(clause.candidate_path)
            if clause.candidate_path in seen_path_blocks:
                errors.append(
                    "citation audit clauses repeat candidate path block: "
                    f"{clause.candidate_path}"
                )
            seen_path_blocks.add(clause.candidate_path)
            previous_path = clause.candidate_path

    if actual_path_blocks != expected_paths:
        errors.append("citation audit clause paths do not match packet statement order")

    for path in expected_paths:
        statement = statements_by_path[path]
        statement_clauses = clauses_by_path[path]
        if not statement_clauses:
            errors.append(f"citation audit has no clauses for statement: {path}")
            continue
        _validate_clause_spans(statement, statement_clauses, errors)
        _validate_clause_supports(statement, statement_clauses, errors)

    _validate_fail_closed_verdict(audit, errors)
    return errors


def validate_citation_audit_v11(
    audit: CitationAuditV11,
    packet: StatementCitationPacketV11,
) -> list[str]:
    """Validate a V1.1 audit against fixed program-owned clause identities.

    The packet, not the model, defines every full-statement clause.  A valid
    response must return each resulting ``clause_id`` exactly once and in the
    same order, then prove support only from that clause's parent statement.
    """
    if not isinstance(audit, CitationAuditV11) or not isinstance(
        packet, StatementCitationPacketV11
    ):
        return [
            "citation audit v1.1 requires CitationAuditV11 and "
            "StatementCitationPacketV11"
        ]

    errors = _validate_statement_citation_packet_integrity(packet)
    errors.extend(validate_statement_citation_packet_v11(packet))
    _validate_identity_v11(audit, packet, errors)

    expected_clause_ids: list[str] = []
    parent_statement_indexes: dict[str, int] = {}
    for statement_index, statement in enumerate(packet.statements):
        for clause in statement.clauses:
            if clause.clause_id in parent_statement_indexes:
                errors.append(
                    "statement citation packet V1.1 has duplicate program clause "
                    f"id: {clause.clause_id}"
                )
                continue
            expected_clause_ids.append(clause.clause_id)
            parent_statement_indexes[clause.clause_id] = statement_index

    actual_clause_ids = [clause.clause_id for clause in audit.clauses]
    if actual_clause_ids != expected_clause_ids:
        errors.append("citation audit clause ids do not match packet clause order")

    clauses_by_statement: dict[int, list[ClauseAuditV11]] = {
        statement_index: [] for statement_index, _ in enumerate(packet.statements)
    }
    for clause in audit.clauses:
        statement_index = parent_statement_indexes.get(clause.clause_id)
        if statement_index is None:
            errors.append(
                "citation audit clause references unknown program clause id: "
                f"{clause.clause_id}"
            )
            continue
        clauses_by_statement[statement_index].append(clause)

    for statement_index, statement in enumerate(packet.statements):
        statement_clauses = clauses_by_statement[statement_index]
        if not statement_clauses:
            errors.append(
                "citation audit has no clauses for statement: "
                f"{statement.candidate_path}"
            )
            continue
        _validate_clause_supports(statement, statement_clauses, errors)

    _validate_fail_closed_verdict_v11(audit, errors)
    return errors


def validate_citation_audit_v12(
    audit: CitationAuditV12,
    packet: StatementCitationPacketV12,
) -> list[str]:
    """Validate a V1.2 audit against fixed program-owned sentence clauses."""
    if not isinstance(audit, CitationAuditV12) or not isinstance(
        packet, StatementCitationPacketV12
    ):
        return [
            "citation audit v1.2 requires CitationAuditV12 and "
            "StatementCitationPacketV12"
        ]

    errors = _validate_statement_citation_packet_integrity(packet)
    errors.extend(validate_statement_citation_packet_v12(packet))
    _validate_identity_v12(audit, packet, errors)

    expected_clause_ids: list[str] = []
    parent_statement_indexes: dict[str, int] = {}
    for statement_index, statement in enumerate(packet.statements):
        for clause in statement.clauses:
            if clause.clause_id in parent_statement_indexes:
                errors.append(
                    "statement citation packet V1.2 has duplicate program clause "
                    f"id: {clause.clause_id}"
                )
                continue
            expected_clause_ids.append(clause.clause_id)
            parent_statement_indexes[clause.clause_id] = statement_index

    actual_clause_ids = [clause.clause_id for clause in audit.clauses]
    if actual_clause_ids != expected_clause_ids:
        errors.append("citation audit clause ids do not match packet clause order")

    clauses_by_statement: dict[int, list[ClauseAuditV12]] = {
        statement_index: [] for statement_index, _ in enumerate(packet.statements)
    }
    for clause in audit.clauses:
        statement_index = parent_statement_indexes.get(clause.clause_id)
        if statement_index is None:
            errors.append(
                "citation audit clause references unknown program clause id: "
                f"{clause.clause_id}"
            )
            continue
        clauses_by_statement[statement_index].append(clause)

    for statement_index, statement in enumerate(packet.statements):
        statement_clauses = clauses_by_statement[statement_index]
        if not statement_clauses:
            errors.append(
                "citation audit has no clauses for statement: "
                f"{statement.candidate_path}"
            )
            continue
        _validate_clause_supports(statement, statement_clauses, errors)

    _validate_fail_closed_verdict_v12(audit, errors)
    return errors


def _validate_statement_citation_packet_integrity(
    packet: (
        StatementCitationPacket
        | StatementCitationPacketV11
        | StatementCitationPacketV12
    ),
) -> list[str]:
    """Fail closed when an allegedly local packet is internally inconsistent."""
    errors: list[str] = []
    seen_paths: set[str] = set()
    for statement in packet.statements:
        path = statement.candidate_path
        if not _JSON_POINTER_PATTERN.fullmatch(path):
            errors.append(
                f"statement citation packet has invalid candidate path: {path}"
            )
        if path in seen_paths:
            errors.append(
                f"statement citation packet has duplicate candidate path: {path}"
            )
        seen_paths.add(path)
        _validate_statement_citation_item_integrity(statement, errors)
    return errors


def _validate_statement_citation_item_integrity(
    statement: StatementCitationItem
    | StatementCitationItemV11
    | StatementCitationItemV12,
    errors: list[str],
) -> None:
    path = statement.candidate_path
    if statement.statement_sha256 != _sha256_text(statement.statement_text):
        errors.append(
            "statement citation packet statement_sha256 does not match statement text: "
            f"{path}"
        )

    evidence_ids = statement.evidence_ids
    structural_ids = statement.structural_evidence_ids
    snippets = statement.snippets
    _append_duplicate_id_errors(
        evidence_ids,
        "statement citation packet has duplicate evidence id",
        errors,
    )
    _append_duplicate_id_errors(
        structural_ids,
        "statement citation packet has duplicate structural evidence id",
        errors,
    )
    _append_duplicate_id_errors(
        [snippet.id for snippet in snippets],
        "statement citation packet has duplicate snippet id",
        errors,
    )

    evidence_id_set = set(evidence_ids)
    structural_id_set = set(structural_ids)
    for evidence_id in evidence_ids:
        if _evidence_kind_from_id(evidence_id) is None:
            errors.append(
                f"statement citation packet has invalid evidence id: {evidence_id}"
            )
    for evidence_id in structural_ids:
        if _evidence_kind_from_id(evidence_id) != "frame":
            errors.append(
                "statement citation packet structural evidence id is not a frame: "
                f"{evidence_id}"
            )

    snippets_by_id: dict[str, list[object]] = {}
    expected_structural_ids = {
        evidence_id
        for evidence_id in evidence_ids
        if _evidence_kind_from_id(evidence_id) == "frame"
    }
    for snippet in snippets:
        snippets_by_id.setdefault(snippet.id, []).append(snippet)
        expected_kind = _evidence_kind_from_id(snippet.id)
        if expected_kind is None:
            errors.append(
                "statement citation packet snippet has invalid evidence id: "
                f"{snippet.id}"
            )
        elif snippet.kind != expected_kind:
            errors.append(
                f"statement citation packet evidence id/kind mismatch: {snippet.id}"
            )
        if snippet.kind == "frame" or expected_kind == "frame":
            errors.append(
                "statement citation packet frame cannot be a semantic snippet: "
                f"{snippet.id}"
            )
        if snippet.id not in evidence_id_set:
            errors.append(
                "statement citation packet snippet is not cited by statement: "
                f"{snippet.id}"
            )
        if snippet.kind in {"transcript", "ocr"} and (
            snippet.text is None or not snippet.text.strip()
        ):
            errors.append(
                "statement citation packet semantic snippet text is empty: "
                f"{snippet.id}"
            )
        if snippet.kind == "transcript":
            _validate_transcript_snippet_timestamps(snippet, errors)

        _validate_snippet_references(
            snippet,
            evidence_id_set.union(structural_id_set),
            errors,
        )
        if snippet.kind == "ocr":
            if snippet.frame_id is None:
                errors.append(
                    "statement citation packet OCR snippet has no frame parent: "
                    f"{snippet.id}"
                )
            elif _evidence_kind_from_id(snippet.frame_id) != "frame":
                errors.append(
                    "statement citation packet OCR snippet frame_id is not a frame: "
                    f"{snippet.frame_id}"
                )
            else:
                expected_structural_ids.add(snippet.frame_id)
                if snippet.frame_id not in structural_id_set:
                    errors.append(
                        "statement citation packet OCR snippet parent frame is "
                        "missing structural evidence: "
                        f"{snippet.frame_id}"
                    )

    for evidence_id in evidence_ids:
        expected_kind = _evidence_kind_from_id(evidence_id)
        matching_snippets = snippets_by_id.get(evidence_id, [])
        if expected_kind in {"transcript", "ocr"}:
            if len(matching_snippets) != 1:
                errors.append(
                    "statement citation packet semantic citation must have exactly "
                    f"one snippet: {evidence_id}"
                )
        elif expected_kind == "frame" and matching_snippets:
            errors.append(
                "statement citation packet direct frame cannot have a semantic "
                f"snippet: {evidence_id}"
            )

    for evidence_id in expected_structural_ids:
        if evidence_id not in structural_id_set:
            errors.append(
                "statement citation packet direct frame is missing structural "
                f"evidence: {evidence_id}"
            )
    for evidence_id in structural_ids:
        if evidence_id not in expected_structural_ids:
            errors.append(
                "statement citation packet has unexpected structural evidence id: "
                f"{evidence_id}"
            )


def _validate_transcript_snippet_timestamps(
    snippet: object,
    errors: list[str],
) -> None:
    start_ms = getattr(snippet, "start_ms")
    end_ms = getattr(snippet, "end_ms")
    if start_ms is None or end_ms is None:
        errors.append(
            "statement citation packet transcript snippet requires start_ms/end_ms: "
            f"{getattr(snippet, 'id')}"
        )
        return
    if start_ms < 0 or end_ms < 0:
        errors.append(
            "statement citation packet transcript snippet timestamps must be non-negative: "
            f"{getattr(snippet, 'id')}"
        )
        return
    if end_ms < start_ms:
        errors.append(
            "statement citation packet transcript snippet end_ms precedes start_ms: "
            f"{getattr(snippet, 'id')}"
        )


def _validate_snippet_references(
    snippet: object,
    local_evidence_ids: set[str],
    errors: list[str],
) -> None:
    frame_id = getattr(snippet, "frame_id")
    if frame_id is not None and frame_id not in local_evidence_ids:
        errors.append(
            "statement citation packet snippet frame id points outside statement: "
            f"{frame_id}"
        )
    for evidence_id in getattr(snippet, "related_evidence_ids"):
        if evidence_id not in local_evidence_ids:
            errors.append(
                "statement citation packet snippet related evidence id points "
                f"outside statement: {evidence_id}"
            )


def _append_duplicate_id_errors(
    values: list[str],
    error_prefix: str,
    errors: list[str],
) -> None:
    duplicate_id = _first_duplicate(values)
    if duplicate_id is not None:
        errors.append(f"{error_prefix}: {duplicate_id}")


def _evidence_kind_from_id(evidence_id: str) -> str | None:
    match = _EVIDENCE_ID_PATTERN.fullmatch(evidence_id)
    if match is None:
        return None
    return {
        "tr": "transcript",
        "fr": "frame",
        "ocr": "ocr",
    }[match.group(1)]


def _validate_identity(
    audit: CitationAudit,
    packet: StatementCitationPacket,
    errors: list[str],
) -> None:
    if audit.task_id != packet.task_id:
        errors.append("citation audit task_id does not match statement citation packet")
    if audit.source_fingerprint != packet.source_fingerprint:
        errors.append(
            "citation audit source_fingerprint does not match statement citation packet"
        )
    if audit.candidate_sha256 != packet.candidate_sha256:
        errors.append(
            "citation audit candidate_sha256 does not match statement citation packet"
        )
    if audit.packet_sha256 != statement_citation_packet_sha256(packet):
        errors.append(
            "citation audit packet_sha256 does not match statement citation packet"
        )


def _validate_identity_v11(
    audit: CitationAuditV11,
    packet: StatementCitationPacketV11,
    errors: list[str],
) -> None:
    if audit.task_id != packet.task_id:
        errors.append("citation audit task_id does not match statement citation packet")
    if audit.source_fingerprint != packet.source_fingerprint:
        errors.append(
            "citation audit source_fingerprint does not match statement citation packet"
        )
    if audit.candidate_sha256 != packet.candidate_sha256:
        errors.append(
            "citation audit candidate_sha256 does not match statement citation packet"
        )
    if audit.packet_sha256 != statement_citation_packet_sha256(packet):
        errors.append(
            "citation audit packet_sha256 does not match statement citation packet"
        )


def _validate_identity_v12(
    audit: CitationAuditV12,
    packet: StatementCitationPacketV12,
    errors: list[str],
) -> None:
    if audit.task_id != packet.task_id:
        errors.append("citation audit task_id does not match statement citation packet")
    if audit.source_fingerprint != packet.source_fingerprint:
        errors.append(
            "citation audit source_fingerprint does not match statement citation packet"
        )
    if audit.candidate_sha256 != packet.candidate_sha256:
        errors.append(
            "citation audit candidate_sha256 does not match statement citation packet"
        )
    if audit.packet_sha256 != statement_citation_packet_sha256(packet):
        errors.append(
            "citation audit packet_sha256 does not match statement citation packet"
        )


def _validate_clause_spans(
    statement: StatementCitationItem,
    clauses: list[ClauseAudit],
    errors: list[str],
) -> None:
    text = statement.statement_text
    covered = [False] * len(text)
    previous_end = 0
    for clause in clauses:
        if clause.start < 0 or clause.end <= clause.start:
            errors.append(
                "citation audit clause has invalid span bounds: "
                f"{statement.candidate_path}"
            )
            continue
        if clause.end > len(text):
            errors.append(
                "citation audit clause span exceeds statement bounds: "
                f"{statement.candidate_path}"
            )
            continue
        if clause.start < previous_end:
            errors.append(
                "citation audit clause spans overlap or are out of order: "
                f"{statement.candidate_path}"
            )
        previous_end = max(previous_end, clause.end)

        for index in range(clause.start, clause.end):
            if covered[index]:
                continue
            covered[index] = True
    if any(
        _requires_span_coverage(character) and not covered[index]
        for index, character in enumerate(text)
    ):
        errors.append(
            "citation audit clause spans leave non-whitespace/non-punctuation "
            f"text uncovered: {statement.candidate_path}"
        )


def _validate_clause_supports(
    statement: StatementCitationItem
    | StatementCitationItemV11
    | StatementCitationItemV12,
    clauses: list[ClauseAudit | ClauseAuditV11 | ClauseAuditV12],
    errors: list[str],
) -> None:
    semantic_evidence_ids = _semantic_citation_ids(statement, errors)
    semantic_id_set = set(semantic_evidence_ids)
    structural_id_set = set(statement.structural_evidence_ids)
    used_semantic_ids: set[str] = set()

    for clause in clauses:
        duplicate_support_id = _first_duplicate(clause.support_evidence_ids)
        if duplicate_support_id is not None:
            errors.append(
                "citation audit clause has duplicate support evidence id: "
                f"{duplicate_support_id}"
            )
        if clause.status != "entailed":
            if clause.support_evidence_ids:
                errors.append(
                    "citation audit non-entailed clause declares support evidence ids: "
                    f"{statement.candidate_path}"
                )
            continue
        if not clause.support_evidence_ids:
            errors.append(
                "citation audit entailed clause has no support evidence ids: "
                f"{statement.candidate_path}"
            )
        for evidence_id in clause.support_evidence_ids:
            if evidence_id in structural_id_set:
                errors.append(
                    "citation audit structural frame evidence cannot support a clause: "
                    f"{evidence_id}"
                )
            elif evidence_id not in semantic_id_set:
                errors.append(
                    "citation audit clause support evidence is not a local semantic "
                    f"citation: {evidence_id}"
                )
            else:
                used_semantic_ids.add(evidence_id)

    for evidence_id in semantic_evidence_ids:
        if evidence_id not in used_semantic_ids:
            errors.append(
                "citation audit leaves semantic citation unused: "
                f"{statement.candidate_path}: {evidence_id}"
            )


def _semantic_citation_ids(
    statement: StatementCitationItem
    | StatementCitationItemV11
    | StatementCitationItemV12,
    errors: list[str],
) -> list[str]:
    snippets_by_id = {snippet.id: snippet for snippet in statement.snippets}
    structural_id_set = set(statement.structural_evidence_ids)
    semantic_ids: list[str] = []
    for evidence_id in statement.evidence_ids:
        snippet = snippets_by_id.get(evidence_id)
        if snippet is not None and snippet.kind in {"transcript", "ocr"}:
            if evidence_id not in semantic_ids:
                semantic_ids.append(evidence_id)
            continue
        if evidence_id not in structural_id_set:
            errors.append(
                "statement citation packet has no local semantic snippet for evidence "
                f"id: {evidence_id}"
            )
    return semantic_ids


def _validate_fail_closed_verdict(audit: CitationAudit, errors: list[str]) -> None:
    non_entailed_clauses = [
        clause for clause in audit.clauses if clause.status != "entailed"
    ]
    if audit.verdict == "approve" and non_entailed_clauses:
        errors.append("approve citation audit contains non-entailed clauses")
    if audit.verdict == "reject" and not non_entailed_clauses:
        errors.append("reject citation audit has no non-entailed clause")
    for clause in non_entailed_clauses:
        errors.append(
            "citation audit rejected due to non-entailed clause: "
            f"{clause.candidate_path} ({clause.status})"
        )


def _validate_fail_closed_verdict_v11(
    audit: CitationAuditV11,
    errors: list[str],
) -> None:
    non_entailed_clauses = [
        clause for clause in audit.clauses if clause.status != "entailed"
    ]
    if audit.verdict == "approve" and non_entailed_clauses:
        errors.append("approve citation audit contains non-entailed clauses")
    if audit.verdict == "reject" and not non_entailed_clauses:
        errors.append("reject citation audit has no non-entailed clause")
    for clause in non_entailed_clauses:
        errors.append(
            "citation audit rejected due to non-entailed clause: "
            f"{clause.clause_id} ({clause.status})"
        )


def _validate_fail_closed_verdict_v12(
    audit: CitationAuditV12,
    errors: list[str],
) -> None:
    non_entailed_clauses = [
        clause for clause in audit.clauses if clause.status != "entailed"
    ]
    if audit.verdict == "approve" and non_entailed_clauses:
        errors.append("approve citation audit contains non-entailed clauses")
    if audit.verdict == "reject" and not non_entailed_clauses:
        errors.append("reject citation audit has no non-entailed clause")
    for clause in non_entailed_clauses:
        errors.append(
            "citation audit rejected due to non-entailed clause: "
            f"{clause.clause_id} ({clause.status})"
        )


def _first_duplicate(values: list[str]) -> str | None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def _requires_span_coverage(character: str) -> bool:
    return not character.isspace() and not unicodedata.category(character).startswith(
        "P"
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
