from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unicodedata

import pytest
from pydantic import ValidationError

from learnnest.citation_audit import (
    CitationAudit,
    CitationAuditV11,
    CitationAuditV12,
    parse_citation_audit,
    parse_citation_audit_v11,
    parse_citation_audit_v12,
    statement_citation_packet_sha256,
    validate_citation_audit,
    validate_citation_audit_v11,
    validate_citation_audit_v12,
)
from learnnest.models import ContentPack, Evidence
from learnnest.note_evidence_scope import (
    StatementCitationPacket,
    StatementCitationPacketV11,
    StatementCitationPacketV12,
    build_statement_citation_packet,
)
from learnnest.note_models import ConceptExplanationNote


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_packet_sha256(
    packet: StatementCitationPacket | StatementCitationPacketV11,
) -> str:
    serialized = json.dumps(
        packet.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(serialized)


def citation_packet() -> StatementCitationPacket:
    summary = "字幕说明 RAG，OCR 图示说明流程。"
    explanation = "第二条字幕说明生成。"
    return StatementCitationPacket.model_validate(
        {
            "schema_version": "1.0",
            "task_id": "20260716-citation-audit",
            "source_fingerprint": "citation-audit-fingerprint",
            "candidate_sha256": "a" * 64,
            "statements": [
                {
                    "candidate_path": "/summary",
                    "statement_text": summary,
                    "statement_sha256": _sha256(summary),
                    "evidence_ids": ["tr_0001", "fr_0001", "ocr_0001"],
                    "snippets": [
                        {
                            "id": "tr_0001",
                            "kind": "transcript",
                            "text": "RAG 的字幕原文。",
                            "start_ms": 0,
                            "end_ms": 1_000,
                            "frame_id": None,
                            "related_evidence_ids": [],
                        },
                        {
                            "id": "ocr_0001",
                            "kind": "ocr",
                            "text": "流程图片文字。",
                            "start_ms": None,
                            "end_ms": None,
                            "frame_id": "fr_0001",
                            "related_evidence_ids": [],
                        },
                    ],
                    "structural_evidence_ids": ["fr_0001"],
                },
                {
                    "candidate_path": "/concepts/0/explanation",
                    "statement_text": explanation,
                    "statement_sha256": _sha256(explanation),
                    "evidence_ids": ["tr_0002"],
                    "snippets": [
                        {
                            "id": "tr_0002",
                            "kind": "transcript",
                            "text": "生成阶段的字幕原文。",
                            "start_ms": 1_000,
                            "end_ms": 2_000,
                            "frame_id": None,
                            "related_evidence_ids": [],
                        }
                    ],
                    "structural_evidence_ids": [],
                },
            ],
        }
    )


def citation_packet_v11() -> StatementCitationPacketV11:
    legacy_packet = citation_packet()
    payload = legacy_packet.model_dump(mode="json")
    payload["schema_version"] = "1.1"
    statements = payload["statements"]
    assert isinstance(statements, list)
    for statement_index, statement in enumerate(statements, start=1):
        assert isinstance(statement, dict)
        statement_text = statement["statement_text"]
        assert isinstance(statement_text, str)
        statement["clauses"] = [
            {
                "clause_id": f"clause-{statement_index:04d}-0001",
                "start": 0,
                "end": len(statement_text),
                "text": statement_text,
            }
        ]
    return StatementCitationPacketV11.model_validate(payload)


def citation_packet_v12() -> StatementCitationPacketV12:
    packet = citation_packet()
    payload = packet.model_dump(mode="json")
    payload["schema_version"] = "1.2"
    statements = payload["statements"]
    assert isinstance(statements, list)
    for statement_index, statement in enumerate(statements, start=1):
        assert isinstance(statement, dict)
        statement_text = statement["statement_text"]
        assert isinstance(statement_text, str)
        statement["clauses"] = [
            {
                "clause_id": f"clause-{statement_index:04d}-0001",
                "start": 0,
                "end": len(statement_text),
                "text": statement_text,
            }
        ]
    return StatementCitationPacketV12.model_validate(payload)


def valid_v11_payload(
    packet: StatementCitationPacketV11,
    *,
    verdict: str = "approve",
    first_status: str = "entailed",
) -> dict[str, object]:
    first_support = ["tr_0001", "ocr_0001"] if first_status == "entailed" else []
    clauses = [
        {
            "clause_id": statement.clauses[0].clause_id,
            "status": first_status if statement_index == 0 else "entailed",
            "support_evidence_ids": (
                first_support if statement_index == 0 else ["tr_0002"]
            ),
            "reason_code": (
                {
                    "entailed": "direct",
                    "unsupported": "missing_direct_support",
                    "ambiguous": "ambiguous_wording",
                }[first_status]
                if statement_index == 0
                else "direct"
            ),
        }
        for statement_index, statement in enumerate(packet.statements)
    ]
    return {
        "schema_version": "1.1",
        "task_id": packet.task_id,
        "source_fingerprint": packet.source_fingerprint,
        "candidate_sha256": packet.candidate_sha256,
        "packet_sha256": _canonical_packet_sha256(packet),
        "verdict": verdict,
        "clauses": clauses,
    }


def valid_v12_payload(
    packet: StatementCitationPacketV12,
    *,
    verdict: str = "approve",
    first_status: str = "entailed",
) -> dict[str, object]:
    clauses: list[dict[str, object]] = []
    for statement_index, statement in enumerate(packet.statements):
        status = first_status if statement_index == 0 else "entailed"
        support_evidence_ids = (
            ["tr_0001", "ocr_0001"]
            if statement_index == 0 and status == "entailed"
            else (["tr_0002"] if status == "entailed" else [])
        )
        clauses.append(
            {
                "clause_id": statement.clauses[0].clause_id,
                "status": status,
                "support_evidence_ids": support_evidence_ids,
                "reason_code": (
                    "direct" if status == "entailed" else "missing_direct_support"
                ),
            }
        )
    return {
        "schema_version": "1.2",
        "task_id": packet.task_id,
        "source_fingerprint": packet.source_fingerprint,
        "candidate_sha256": packet.candidate_sha256,
        "packet_sha256": statement_citation_packet_sha256(packet),
        "verdict": verdict,
        "clauses": clauses,
    }


def _all_entailed_v11_payload(
    packet: StatementCitationPacketV11,
) -> dict[str, object]:
    return {
        "schema_version": "1.1",
        "task_id": packet.task_id,
        "source_fingerprint": packet.source_fingerprint,
        "candidate_sha256": packet.candidate_sha256,
        "packet_sha256": _canonical_packet_sha256(packet),
        "verdict": "approve",
        "clauses": [
            {
                "clause_id": statement.clauses[0].clause_id,
                "status": "entailed",
                "support_evidence_ids": [snippet.id for snippet in statement.snippets],
                "reason_code": "direct",
            }
            for statement in packet.statements
        ],
    }


def _clause_payload(
    packet: StatementCitationPacket,
    statement_index: int,
    start: int,
    end: int,
    support_evidence_ids: list[str],
    *,
    status: str = "entailed",
) -> dict[str, object]:
    statement = packet.statements[statement_index]
    reason_code = {
        "entailed": "direct",
        "unsupported": "missing_direct_support",
        "ambiguous": "ambiguous_wording",
    }[status]
    return {
        "candidate_path": statement.candidate_path,
        "statement_sha256": statement.statement_sha256,
        "start": start,
        "end": end,
        "status": status,
        "support_evidence_ids": support_evidence_ids,
        "reason_code": reason_code,
    }


def valid_payload(
    packet: StatementCitationPacket,
    *,
    verdict: str = "approve",
    first_status: str = "entailed",
) -> dict[str, object]:
    summary = packet.statements[0]
    summary_separator = summary.statement_text.index("，")
    final_punctuation_start = len(summary.statement_text) - 1
    first_support = ["tr_0001"] if first_status == "entailed" else []
    clauses = [
        _clause_payload(
            packet,
            0,
            0,
            summary_separator,
            first_support,
            status=first_status,
        ),
        _clause_payload(
            packet,
            0,
            summary_separator + 1,
            final_punctuation_start,
            ["ocr_0001"],
        ),
        _clause_payload(
            packet,
            1,
            0,
            len(packet.statements[1].statement_text) - 1,
            ["tr_0002"],
        ),
    ]
    return {
        "schema_version": "1.0",
        "task_id": packet.task_id,
        "source_fingerprint": packet.source_fingerprint,
        "candidate_sha256": packet.candidate_sha256,
        "packet_sha256": _canonical_packet_sha256(packet),
        "verdict": verdict,
        "clauses": clauses,
    }


def valid_audit(packet: StatementCitationPacket) -> CitationAudit:
    return CitationAudit.model_validate(valid_payload(packet))


def _all_entailed_payload(packet: StatementCitationPacket) -> dict[str, object]:
    clauses = [
        _clause_payload(
            packet,
            statement_index,
            0,
            len(statement.statement_text),
            [snippet.id for snippet in statement.snippets],
        )
        for statement_index, statement in enumerate(packet.statements)
    ]
    return {
        "schema_version": "1.0",
        "task_id": packet.task_id,
        "source_fingerprint": packet.source_fingerprint,
        "candidate_sha256": packet.candidate_sha256,
        "packet_sha256": _canonical_packet_sha256(packet),
        "verdict": "approve",
        "clauses": clauses,
    }


def test_approve_audit_requires_every_clause_entailed() -> None:
    packet = citation_packet()

    audit = CitationAudit.model_validate(valid_payload(packet, verdict="approve"))

    assert validate_citation_audit(audit, packet) == []


def test_approve_audit_rejects_unsupported_clause() -> None:
    packet = citation_packet()

    with pytest.raises(ValidationError, match="approve citation audit"):
        CitationAudit.model_validate(
            valid_payload(
                packet,
                verdict="approve",
                first_status="unsupported",
            )
        )


def test_reject_audit_requires_a_non_entailed_clause() -> None:
    with pytest.raises(ValidationError, match="reject citation audit"):
        CitationAudit.model_validate(valid_payload(citation_packet(), verdict="reject"))


def test_clause_rejects_duplicate_or_inconsistent_support_declarations() -> None:
    packet = citation_packet()
    payload = valid_payload(packet)
    payload["clauses"][0]["support_evidence_ids"] = ["tr_0001", "tr_0001"]

    with pytest.raises(ValidationError, match="duplicate support evidence"):
        CitationAudit.model_validate(payload)

    unsupported = valid_payload(packet, verdict="reject", first_status="unsupported")
    unsupported["clauses"][0]["support_evidence_ids"] = ["tr_0001"]
    with pytest.raises(ValidationError, match="non-entailed clause"):
        CitationAudit.model_validate(unsupported)


@pytest.mark.parametrize(
    "raw_suffix",
    [
        " prose",
        '\n{"schema_version":"1.0"}',
    ],
)
def test_parser_rejects_prose_and_multiple_json_objects(raw_suffix: str) -> None:
    raw = json.dumps(valid_payload(citation_packet()), ensure_ascii=False) + raw_suffix

    with pytest.raises(ValidationError):
        parse_citation_audit(raw)


def test_parser_rejects_extra_fields() -> None:
    payload = valid_payload(citation_packet())
    payload["untrusted_prose"] = "not allowed"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_citation_audit(json.dumps(payload, ensure_ascii=False))


def test_clause_rejects_model_calculated_substring_hash() -> None:
    payload = valid_payload(citation_packet())
    payload["clauses"][0]["text_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CitationAudit.model_validate(payload)


def test_packet_sha_is_canonical_and_candidate_linked() -> None:
    packet = citation_packet()

    assert statement_citation_packet_sha256(packet) == _canonical_packet_sha256(packet)
    assert statement_citation_packet_sha256(packet) == statement_citation_packet_sha256(
        packet.model_copy(deep=True)
    )


def test_matching_content_pack_builds_a_packet_that_passes_local_integrity() -> None:
    content_pack = ContentPack(
        task_id="20260716-builder",
        source_fingerprint="builder-fingerprint",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="RAG 使用检索到的内容辅助生成。",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=500,
                artifact_path="frames/selected/fr_0001.png",
                related_evidence_ids=["ocr_0001"],
            ),
            Evidence(
                id="ocr_0001",
                kind="ocr",
                text="检索增强生成流程",
                artifact_path="ocr.json",
                frame_id="fr_0001",
            ),
        ],
    )
    note = ConceptExplanationNote.model_validate(
        {
            "schema_version": "3.0",
            "task_id": content_pack.task_id,
            "source_fingerprint": content_pack.source_fingerprint,
            "classification_evidence_ids": ["tr_0001"],
            "note_type": "concept_explanation",
            "title": "RAG 概念",
            "summary": {
                "text": "字幕说明 RAG 的定义。图中文字展示检索增强流程。",
                "evidence_ids": ["tr_0001", "fr_0001", "ocr_0001"],
            },
            "concepts": [
                {
                    "title": "检索增强",
                    "explanation": {
                        "text": "字幕说明检索内容辅助生成。",
                        "evidence_ids": ["tr_0001"],
                    },
                }
            ],
        }
    )

    packet = build_statement_citation_packet(note, content_pack)
    assert isinstance(packet, StatementCitationPacketV12)
    summary = packet.statements[0]
    assert len(summary.clauses) == 2
    payload = {
        "schema_version": "1.2",
        "task_id": packet.task_id,
        "source_fingerprint": packet.source_fingerprint,
        "candidate_sha256": packet.candidate_sha256,
        "packet_sha256": statement_citation_packet_sha256(packet),
        "verdict": "approve",
        "clauses": [
            {
                "clause_id": summary.clauses[0].clause_id,
                "status": "entailed",
                "support_evidence_ids": ["tr_0001"],
                "reason_code": "direct",
            },
            {
                "clause_id": summary.clauses[1].clause_id,
                "status": "entailed",
                "support_evidence_ids": ["ocr_0001"],
                "reason_code": "direct",
            },
            {
                "clause_id": packet.statements[1].clauses[0].clause_id,
                "status": "entailed",
                "support_evidence_ids": ["tr_0001"],
                "reason_code": "direct",
            },
        ],
    }
    audit = CitationAuditV12.model_validate(payload)

    assert parse_citation_audit_v12(audit.model_dump_json()) == audit
    assert validate_citation_audit_v12(audit, packet) == []


def test_v12_validator_rejects_version_mixing_and_clause_id_order() -> None:
    packet = citation_packet_v12()
    audit = CitationAuditV12.model_validate(valid_v12_payload(packet))
    legacy_packet = citation_packet_v11()
    legacy_audit = CitationAuditV11.model_validate(valid_v11_payload(legacy_packet))

    assert validate_citation_audit_v12(legacy_audit, packet) == [
        "citation audit v1.2 requires CitationAuditV12 and StatementCitationPacketV12"
    ]
    assert validate_citation_audit_v12(audit, legacy_packet) == [
        "citation audit v1.2 requires CitationAuditV12 and StatementCitationPacketV12"
    ]

    reordered_payload = valid_v12_payload(packet)
    reordered_clauses = reordered_payload["clauses"]
    assert isinstance(reordered_clauses, list)
    reordered_clauses.reverse()
    reordered_errors = validate_citation_audit_v12(
        CitationAuditV12.model_validate(reordered_payload), packet
    )
    assert (
        "citation audit clause ids do not match packet clause order" in reordered_errors
    )

    duplicated_payload = valid_v12_payload(packet)
    duplicated_clauses = duplicated_payload["clauses"]
    assert isinstance(duplicated_clauses, list)
    first_clause = duplicated_clauses[0]
    second_clause = duplicated_clauses[1]
    assert isinstance(first_clause, dict)
    assert isinstance(second_clause, dict)
    second_clause["clause_id"] = first_clause["clause_id"]
    duplicated_errors = validate_citation_audit_v12(
        CitationAuditV12.model_validate(duplicated_payload), packet
    )
    assert (
        "citation audit clause ids do not match packet clause order"
        in duplicated_errors
    )


@pytest.mark.parametrize(
    ("support_evidence_ids", "expected_error"),
    [
        (
            ["tr_foreign"],
            "citation audit clause support evidence is not a local semantic citation: tr_foreign",
        ),
        (
            ["fr_0001"],
            "citation audit structural frame evidence cannot support a clause: fr_0001",
        ),
        (
            ["tr_0001"],
            "citation audit leaves semantic citation unused: /summary: ocr_0001",
        ),
    ],
)
def test_v12_validator_rejects_nonlocal_structural_or_unused_support(
    support_evidence_ids: list[str],
    expected_error: str,
) -> None:
    packet = citation_packet_v12()
    payload = valid_v12_payload(packet)
    clauses = payload["clauses"]
    assert isinstance(clauses, list)
    first_clause = clauses[0]
    assert isinstance(first_clause, dict)
    first_clause["support_evidence_ids"] = support_evidence_ids

    errors = validate_citation_audit_v12(
        CitationAuditV12.model_validate(payload), packet
    )

    assert expected_error in errors


def test_v12_approve_schema_rejects_an_unsupported_clause() -> None:
    packet = citation_packet_v12()

    with pytest.raises(ValidationError, match="approve citation audit"):
        CitationAuditV12.model_validate(
            valid_v12_payload(packet, first_status="unsupported")
        )


def _update_clause_span(
    payload: dict[str, object],
    _: StatementCitationPacket,
    clause_index: int,
    start: int,
    end: int,
) -> None:
    clause = payload["clauses"][clause_index]
    assert isinstance(clause, dict)
    clause["start"] = start
    clause["end"] = end


def _mutate_gap(payload: dict[str, object], packet: StatementCitationPacket) -> None:
    _update_clause_span(
        payload, packet, 0, 1, packet.statements[0].statement_text.index("，")
    )


def _mutate_overlap(
    payload: dict[str, object], packet: StatementCitationPacket
) -> None:
    separator = packet.statements[0].statement_text.index("，")
    _update_clause_span(
        payload, packet, 1, separator - 1, len(packet.statements[0].statement_text) - 1
    )


def _mutate_out_of_bounds(
    payload: dict[str, object], packet: StatementCitationPacket
) -> None:
    clause = payload["clauses"][0]
    assert isinstance(clause, dict)
    clause["end"] = len(packet.statements[0].statement_text) + 1


def _mutate_foreign_support(
    payload: dict[str, object], _: StatementCitationPacket
) -> None:
    clause = payload["clauses"][0]
    assert isinstance(clause, dict)
    clause["support_evidence_ids"] = ["tr_foreign"]


def _mutate_structural_support(
    payload: dict[str, object], _: StatementCitationPacket
) -> None:
    clause = payload["clauses"][0]
    assert isinstance(clause, dict)
    clause["support_evidence_ids"] = ["fr_0001"]


def _mutate_unused_semantic_citation(
    payload: dict[str, object], _: StatementCitationPacket
) -> None:
    clause = payload["clauses"][1]
    assert isinstance(clause, dict)
    clause["support_evidence_ids"] = ["tr_0001"]


@pytest.mark.parametrize(
    "mutator",
    [
        _mutate_gap,
        _mutate_overlap,
        _mutate_out_of_bounds,
        _mutate_foreign_support,
        _mutate_structural_support,
        _mutate_unused_semantic_citation,
    ],
)
def test_citation_audit_rejects_non_exact_clause_proof(
    mutator: object,
) -> None:
    packet = citation_packet()
    payload = deepcopy(valid_payload(packet))
    assert callable(mutator)
    mutator(payload, packet)

    errors = validate_citation_audit(CitationAudit.model_validate(payload), packet)

    assert errors


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("task_id", "another-task", "task_id does not match"),
        (
            "source_fingerprint",
            "another-fingerprint",
            "source_fingerprint does not match",
        ),
        ("candidate_sha256", "b" * 64, "candidate_sha256 does not match"),
        ("packet_sha256", "c" * 64, "packet_sha256 does not match"),
    ],
)
def test_citation_audit_requires_exact_packet_identity(
    field: str,
    value: str,
    expected: str,
) -> None:
    packet = citation_packet()
    payload = valid_payload(packet)
    payload[field] = value

    errors = validate_citation_audit(CitationAudit.model_validate(payload), packet)

    assert any(expected in error for error in errors)


def test_citation_audit_rejects_unknown_path_and_wrong_statement_hash() -> None:
    packet = citation_packet()
    unknown_payload = valid_payload(packet)
    unknown_payload["clauses"][0]["candidate_path"] = "/unknown"
    unknown_errors = validate_citation_audit(
        CitationAudit.model_validate(unknown_payload), packet
    )
    assert any("unknown candidate path" in error for error in unknown_errors)

    hash_payload = valid_payload(packet)
    hash_payload["clauses"][0]["statement_sha256"] = "b" * 64
    hash_errors = validate_citation_audit(
        CitationAudit.model_validate(hash_payload), packet
    )
    assert any("statement_sha256 does not match" in error for error in hash_errors)


def test_citation_audit_rejects_path_reordering_or_omission() -> None:
    packet = citation_packet()
    reordered = valid_payload(packet)
    clauses = reordered["clauses"]
    assert isinstance(clauses, list)
    clauses[1], clauses[2] = clauses[2], clauses[1]
    reorder_errors = validate_citation_audit(
        CitationAudit.model_validate(reordered), packet
    )
    assert any("packet statement order" in error for error in reorder_errors)

    omitted = valid_payload(packet)
    omitted_clauses = omitted["clauses"]
    assert isinstance(omitted_clauses, list)
    omitted["clauses"] = omitted_clauses[:-1]
    omission_errors = validate_citation_audit(
        CitationAudit.model_validate(omitted), packet
    )
    assert any("packet statement order" in error for error in omission_errors)


def test_reject_with_non_entailed_clause_returns_stable_fail_closed_error() -> None:
    packet = citation_packet()
    audit = CitationAudit.model_validate(
        valid_payload(packet, verdict="reject", first_status="unsupported")
    )

    errors = validate_citation_audit(audit, packet)

    assert (
        "citation audit rejected due to non-entailed clause: /summary (unsupported)"
        in errors
    )


def test_unicode_whitespace_and_punctuation_do_not_require_span_coverage() -> None:
    text = "甲， A；\n乙。"
    packet = StatementCitationPacket.model_validate(
        {
            "schema_version": "1.0",
            "task_id": "20260716-unicode",
            "source_fingerprint": "unicode-fingerprint",
            "candidate_sha256": "d" * 64,
            "statements": [
                {
                    "candidate_path": "/summary",
                    "statement_text": text,
                    "statement_sha256": _sha256(text),
                    "evidence_ids": ["tr_0009"],
                    "snippets": [
                        {
                            "id": "tr_0009",
                            "kind": "transcript",
                            "text": "甲 A 乙",
                            "start_ms": 0,
                            "end_ms": 1_000,
                            "frame_id": None,
                            "related_evidence_ids": [],
                        }
                    ],
                    "structural_evidence_ids": [],
                }
            ],
        }
    )
    statement = packet.statements[0]
    clauses = [
        _clause_payload(packet, 0, index, index + 1, ["tr_0009"])
        for index, character in enumerate(text)
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    ]
    payload = {
        "schema_version": "1.0",
        "task_id": packet.task_id,
        "source_fingerprint": packet.source_fingerprint,
        "candidate_sha256": packet.candidate_sha256,
        "packet_sha256": _canonical_packet_sha256(packet),
        "verdict": "approve",
        "clauses": clauses,
    }

    audit = CitationAudit.model_validate(payload)

    assert statement.statement_text == text
    assert validate_citation_audit(audit, packet) == []


def _replace_first_statement(
    packet: StatementCitationPacket,
    **updates: object,
) -> StatementCitationPacket:
    statements = list(packet.statements)
    statements[0] = statements[0].model_copy(update=updates)
    return packet.model_copy(update={"statements": statements})


def test_packet_integrity_rejects_forged_statement_text_hash() -> None:
    packet = _replace_first_statement(
        citation_packet(),
        statement_sha256="f" * 64,
    )
    audit = CitationAudit.model_validate(valid_payload(packet))

    errors = validate_citation_audit(audit, packet)

    assert any(
        "statement_sha256 does not match statement text" in error for error in errors
    )


def test_packet_integrity_rejects_extra_uncited_semantic_snippet() -> None:
    source_packet = citation_packet()
    first = source_packet.statements[0]
    extra_snippet = first.snippets[0].model_copy(
        update={"id": "tr_9999", "text": "未被当前 statement 引用的字幕。"}
    )
    packet = _replace_first_statement(
        source_packet,
        snippets=[*first.snippets, extra_snippet],
    )
    audit = CitationAudit.model_validate(valid_payload(packet))

    errors = validate_citation_audit(audit, packet)

    assert any("snippet is not cited by statement" in error for error in errors)


def test_packet_integrity_rejects_duplicate_evidence_and_snippet_ids() -> None:
    source_packet = citation_packet()
    first = source_packet.statements[0]
    packet = _replace_first_statement(
        source_packet,
        evidence_ids=[*first.evidence_ids, "tr_0001"],
        snippets=[*first.snippets, first.snippets[0].model_copy(deep=True)],
    )
    audit = CitationAudit.model_validate(valid_payload(packet))

    errors = validate_citation_audit(audit, packet)

    assert any("duplicate evidence id: tr_0001" in error for error in errors)
    assert any("duplicate snippet id: tr_0001" in error for error in errors)


def test_packet_integrity_rejects_frame_id_disguised_as_transcript() -> None:
    source_packet = citation_packet()
    first = source_packet.statements[0]
    forged_frame_snippet = first.snippets[0].model_copy(
        update={
            "id": "fr_0001",
            "kind": "transcript",
            "text": "伪造的图片字幕。",
            "frame_id": None,
        }
    )
    packet = _replace_first_statement(
        source_packet,
        snippets=[first.snippets[0], forged_frame_snippet, first.snippets[1]],
        structural_evidence_ids=[],
    )
    payload = valid_payload(packet)
    payload["clauses"][0]["support_evidence_ids"] = ["tr_0001", "fr_0001"]
    audit = CitationAudit.model_validate(payload)

    errors = validate_citation_audit(audit, packet)

    assert any("evidence id/kind mismatch: fr_0001" in error for error in errors)


def test_packet_integrity_rejects_ocr_without_its_structural_frame_parent() -> None:
    source_packet = citation_packet()
    first = source_packet.statements[0]
    packet = _replace_first_statement(
        source_packet,
        evidence_ids=["tr_0001", "ocr_0001"],
        snippets=[first.snippets[0], first.snippets[1]],
        structural_evidence_ids=[],
    )
    audit = CitationAudit.model_validate(valid_payload(packet))

    errors = validate_citation_audit(audit, packet)

    assert any(
        "OCR snippet parent frame is missing structural evidence: fr_0001" in error
        for error in errors
    )


def test_packet_integrity_rejects_invalid_or_duplicate_statement_paths() -> None:
    source_packet = citation_packet()
    invalid_path_packet = _replace_first_statement(
        source_packet,
        candidate_path="/bad~pointer",
    )
    invalid_path_errors = validate_citation_audit(
        valid_audit(source_packet), invalid_path_packet
    )
    assert any(
        "invalid candidate path: /bad~pointer" in error for error in invalid_path_errors
    )

    statements = list(source_packet.statements)
    statements[1] = statements[1].model_copy(update={"candidate_path": "/summary"})
    duplicate_path_packet = source_packet.model_copy(update={"statements": statements})
    duplicate_path_errors = validate_citation_audit(
        valid_audit(source_packet), duplicate_path_packet
    )
    assert any(
        "duplicate candidate path: /summary" in error for error in duplicate_path_errors
    )


def test_packet_integrity_rejects_extra_structural_and_outside_snippet_references() -> (
    None
):
    source_packet = citation_packet()
    first = source_packet.statements[0]
    extra_structural_packet = _replace_first_statement(
        source_packet,
        structural_evidence_ids=["fr_0001", "fr_9999"],
    )
    extra_structural_errors = validate_citation_audit(
        CitationAudit.model_validate(valid_payload(extra_structural_packet)),
        extra_structural_packet,
    )
    assert any(
        "unexpected structural evidence id: fr_9999" in error
        for error in extra_structural_errors
    )

    outside_reference_snippet = first.snippets[0].model_copy(
        update={"related_evidence_ids": ["tr_9999"]}
    )
    outside_reference_packet = _replace_first_statement(
        source_packet,
        snippets=[outside_reference_snippet, first.snippets[1]],
    )
    outside_reference_errors = validate_citation_audit(
        CitationAudit.model_validate(valid_payload(outside_reference_packet)),
        outside_reference_packet,
    )
    assert any(
        "snippet related evidence id points outside statement: tr_9999" in error
        for error in outside_reference_errors
    )


def test_packet_integrity_rejects_empty_semantic_snippet_text() -> None:
    source_packet = citation_packet()
    first = source_packet.statements[0]
    empty_text_packet = _replace_first_statement(
        source_packet,
        snippets=[
            first.snippets[0].model_copy(update={"text": ""}),
            first.snippets[1],
        ],
    )
    audit = CitationAudit.model_validate(valid_payload(empty_text_packet))

    errors = validate_citation_audit(audit, empty_text_packet)

    assert any("semantic snippet text is empty: tr_0001" in error for error in errors)


@pytest.mark.parametrize(
    ("updates", "expected_error"),
    [
        (
            {"start_ms": None},
            "statement citation packet transcript snippet requires start_ms/end_ms: "
            "tr_0001",
        ),
        (
            {"end_ms": None},
            "statement citation packet transcript snippet requires start_ms/end_ms: "
            "tr_0001",
        ),
        (
            {"start_ms": -1},
            "statement citation packet transcript snippet timestamps must be "
            "non-negative: tr_0001",
        ),
        (
            {"start_ms": 1_001, "end_ms": 1_000},
            "statement citation packet transcript snippet end_ms precedes "
            "start_ms: tr_0001",
        ),
    ],
)
def test_packet_integrity_rejects_forged_transcript_timestamps(
    updates: dict[str, int | None],
    expected_error: str,
) -> None:
    source_packet = citation_packet()
    first = source_packet.statements[0]
    invalid_transcript = first.snippets[0].model_copy(update=updates)
    packet = _replace_first_statement(
        source_packet,
        snippets=[invalid_transcript, first.snippets[1]],
    )
    audit = CitationAudit.model_validate(valid_payload(packet))

    errors = validate_citation_audit(audit, packet)

    assert expected_error in errors


def test_v11_accepts_program_owned_clause_ids_without_model_offsets() -> None:
    packet = citation_packet_v11()

    audit = CitationAuditV11.model_validate(valid_v11_payload(packet))

    assert validate_citation_audit_v11(audit, packet) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_path", "/summary"),
        ("statement_sha256", "a" * 64),
        ("start", 0),
        ("end", 3),
        ("text", "模型不应返回片段文本"),
    ],
)
def test_v11_clause_rejects_model_calculated_location_fields(
    field: str,
    value: object,
) -> None:
    payload = valid_v11_payload(citation_packet_v11())
    clause = payload["clauses"][0]
    assert isinstance(clause, dict)
    clause[field] = value

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CitationAuditV11.model_validate(payload)


def test_v11_parser_rejects_legacy_audit_protocol() -> None:
    raw_v10 = json.dumps(valid_payload(citation_packet()), ensure_ascii=False)

    with pytest.raises(ValidationError):
        parse_citation_audit_v11(raw_v10)


def test_v11_validator_requires_the_exact_program_clause_order() -> None:
    packet = citation_packet_v11()
    payload = valid_v11_payload(packet)
    clauses = payload["clauses"]
    assert isinstance(clauses, list)
    clauses.reverse()

    errors = validate_citation_audit_v11(
        CitationAuditV11.model_validate(payload), packet
    )

    assert "citation audit clause ids do not match packet clause order" in errors


def test_v11_validator_requires_local_semantic_support_for_each_statement() -> None:
    packet = citation_packet_v11()
    payload = valid_v11_payload(packet)
    clause = payload["clauses"][0]
    assert isinstance(clause, dict)
    clause["support_evidence_ids"] = ["tr_0001"]

    errors = validate_citation_audit_v11(
        CitationAuditV11.model_validate(payload), packet
    )

    assert any(
        "leaves semantic citation unused: /summary: ocr_0001" in error
        for error in errors
    )


def test_v11_validator_preserves_local_packet_evidence_integrity_checks() -> None:
    packet = citation_packet_v11()
    statements = list(packet.statements)
    statements[0] = statements[0].model_copy(update={"structural_evidence_ids": []})
    invalid_packet = packet.model_copy(update={"statements": statements})
    audit = CitationAuditV11.model_validate(valid_v11_payload(invalid_packet))

    errors = validate_citation_audit_v11(audit, invalid_packet)

    assert any(
        "OCR snippet parent frame is missing structural evidence: fr_0001" in error
        for error in errors
    )


def test_v11_reject_verdict_returns_clause_id_fail_closed_error() -> None:
    packet = citation_packet_v11()
    audit = CitationAuditV11.model_validate(
        valid_v11_payload(packet, verdict="reject", first_status="unsupported")
    )

    errors = validate_citation_audit_v11(audit, packet)

    assert (
        "citation audit rejected due to non-entailed clause: "
        "clause-0001-0001 (unsupported)"
    ) in errors


def test_v11_validator_fails_closed_for_a_v10_audit_or_packet() -> None:
    legacy_packet = citation_packet()
    legacy_audit = CitationAudit.model_validate(valid_payload(legacy_packet))

    errors = validate_citation_audit_v11(legacy_audit, legacy_packet)  # type: ignore[arg-type]

    assert errors == [
        "citation audit v1.1 requires CitationAuditV11 and StatementCitationPacketV11"
    ]
