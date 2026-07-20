from __future__ import annotations

import hashlib

import pytest

from helpers.note_v3_fixtures import (
    concept_payload,
    practical_payload,
    resource_payload,
)
from learnnest.models import ContentPack, Evidence
from learnnest.note_coverage import CoveragePlan
from learnnest.note_evidence_scope import (
    _build_program_sentence_clauses,
    StatementCitationPacket,
    StatementCitationPacketV10,
    StatementCitationPacketV11,
    StatementCitationPacketV12,
    build_draft_evidence_scope,
    build_statement_citation_packet,
    parse_statement_citation_packet,
    parse_statement_citation_packet_v11,
    parse_statement_citation_packet_v12,
    validate_statement_citation_packet_v12,
    validate_note_citations_against_scope,
)
from learnnest.note_models import (
    GENERATED_NOTE_ADAPTER,
    AnyGeneratedNote,
    ConceptExplanationNote,
    GeneratedNote,
    ResourceShareNote,
)
from learnnest.note_review import build_statement_manifest
from learnnest.note_validation import iter_factual_statement_entries


def content_pack() -> ContentPack:
    return ContentPack(
        task_id="20260716-evidence-scope",
        source_fingerprint="evidence-scope-fingerprint",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="selected transcript source",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="tr_0002",
                kind="transcript",
                start_ms=1_000,
                end_ms=2_000,
                text="unselected classification source",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="tr_0003",
                kind="transcript",
                start_ms=2_000,
                end_ms=3_000,
                text="unselected factual source",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="tr_0004",
                kind="transcript",
                start_ms=3_000,
                end_ms=4_000,
                text="https://example.com/unselected-resource",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="ai_0001",
                kind="ai_supplement",
                text="unselected AI supplement sentinel",
                artifact_path="content_pack.json",
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
                text="selected OCR source",
                artifact_path="ocr.json",
                frame_id="fr_0001",
            ),
            Evidence(
                id="fr_0002",
                kind="frame",
                start_ms=2_500,
                artifact_path="frames/selected/fr_0002.png",
                related_evidence_ids=["ocr_0002"],
            ),
            Evidence(
                id="ocr_0002",
                kind="ocr",
                text="unselected OCR source",
                artifact_path="ocr.json",
                frame_id="fr_0002",
            ),
        ],
    )


def coverage_plan() -> CoveragePlan:
    return CoveragePlan.model_validate(
        {
            "schema_version": "1.1",
            "task_id": "20260716-evidence-scope",
            "source_fingerprint": "evidence-scope-fingerprint",
            "units": [
                {
                    "label": "plan label sentinel",
                    "source_evidence_ids": ["tr_0001"],
                    "rationale": "plan rationale sentinel",
                }
            ],
            "visuals": [
                {
                    "frame_evidence_id": "fr_0001",
                    "supporting_ocr_evidence_ids": ["ocr_0001"],
                    "disposition": "use",
                    "unit_label": "plan label sentinel",
                    "rationale": "used visual rationale sentinel",
                },
                {
                    "frame_evidence_id": "fr_0002",
                    "supporting_ocr_evidence_ids": [],
                    "disposition": "omit",
                    "unit_label": None,
                    "rationale": "omitted visual rationale sentinel",
                },
            ],
        }
    )


def packet_note() -> ConceptExplanationNote:
    return ConceptExplanationNote.model_validate(
        {
            "schema_version": "3.0",
            "task_id": "20260716-evidence-scope",
            "source_fingerprint": "evidence-scope-fingerprint",
            "classification_evidence_ids": ["tr_0001"],
            "note_type": "concept_explanation",
            "title": "概念笔记",
            "summary": {
                "text": "选择的字幕和图片文字共同说明概念。",
                "evidence_ids": ["tr_0001", "fr_0001", "ocr_0001"],
            },
            "concepts": [
                {
                    "title": "未选内容",
                    "explanation": {
                        "text": "另一条 statement 使用未选字幕。",
                        "evidence_ids": ["tr_0002"],
                    },
                }
            ],
        }
    )


def scope_identity_note() -> ConceptExplanationNote:
    return ConceptExplanationNote.model_validate(
        {
            "schema_version": "3.0",
            "task_id": "20260716-evidence-scope",
            "source_fingerprint": "evidence-scope-fingerprint",
            "classification_evidence_ids": ["tr_0001"],
            "note_type": "concept_explanation",
            "title": "作用域身份笔记",
            "summary": {
                "text": "选定字幕支持摘要。",
                "evidence_ids": ["tr_0001"],
            },
            "concepts": [
                {
                    "title": "选定内容",
                    "explanation": {
                        "text": "选定字幕支持解释。",
                        "evidence_ids": ["tr_0001"],
                    },
                }
            ],
        }
    )


def legacy_note() -> GeneratedNote:
    return GeneratedNote.model_validate(
        {
            "schema_version": "2.0",
            "task_id": "20260716-evidence-scope",
            "source_fingerprint": "evidence-scope-fingerprint",
            "title": "旧版笔记",
            "audience": {"text": "学习者。", "evidence_ids": ["tr_0001"]},
            "summary": {"text": "旧版摘要。", "evidence_ids": ["tr_0001"]},
            "key_points": [{"text": "旧版要点。", "evidence_ids": ["tr_0001"]}],
            "steps": [
                {
                    "order": 1,
                    "text": "旧版步骤。",
                    "evidence_ids": ["tr_0001"],
                }
            ],
            "cautions": [{"text": "旧版提醒。", "evidence_ids": ["tr_0001"]}],
            "ai_supplements": [],
        }
    )


def test_draft_scope_projects_only_selected_raw_evidence() -> None:
    scope = build_draft_evidence_scope(content_pack(), coverage_plan())

    serialized = scope.model_dump_json()

    assert "tr_0001" in serialized
    assert "selected transcript source" in serialized
    assert "tr_0002" not in serialized
    assert "fr_0002" not in serialized
    assert "ocr_0002" not in serialized
    assert "unselected factual source" not in serialized
    assert "unselected OCR source" not in serialized
    assert "plan label sentinel" not in serialized
    assert "plan rationale sentinel" not in serialized
    assert "used visual rationale sentinel" not in serialized
    assert "omitted visual rationale sentinel" not in serialized
    assert scope.allowlisted_evidence_ids == ["tr_0001", "fr_0001", "ocr_0001"]
    assert [evidence.id for evidence in scope.units[0].evidence] == ["tr_0001"]
    assert scope.visuals[0].model_dump() == {
        "visual_index": 1,
        "disposition": "use",
        "unit_index": 1,
        "frame": {
            "id": "fr_0001",
            "kind": "frame",
            "text": None,
            "start_ms": 500,
            "end_ms": None,
            "frame_id": None,
            "related_evidence_ids": ["ocr_0001"],
        },
        "supporting_ocr": [
            {
                "id": "ocr_0001",
                "kind": "ocr",
                "text": "selected OCR source",
                "start_ms": None,
                "end_ms": None,
                "frame_id": "fr_0001",
                "related_evidence_ids": [],
            }
        ],
    }
    assert scope.visuals[1].model_dump() == {
        "visual_index": 2,
        "disposition": "omit",
    }


def test_scope_rejects_unselected_citations_across_all_citation_fields() -> None:
    note = ResourceShareNote.model_validate(
        {
            "schema_version": "3.0",
            "task_id": "20260716-evidence-scope",
            "source_fingerprint": "evidence-scope-fingerprint",
            "classification_evidence_ids": ["tr_0002"],
            "note_type": "resource_share",
            "title": "资源笔记",
            "summary": {
                "text": "未选择的事实来源。",
                "evidence_ids": ["tr_0003"],
            },
            "resources": [
                {
                    "name": {
                        "text": "选择的资源名称。",
                        "evidence_ids": ["tr_0001"],
                    },
                    "value": {
                        "text": "选择的资源价值。",
                        "evidence_ids": ["tr_0001"],
                    },
                    "locator": {
                        "url": "https://example.com/unselected-resource",
                        "evidence_ids": ["tr_0004"],
                    },
                }
            ],
        }
    )

    errors = validate_note_citations_against_scope(
        note, build_draft_evidence_scope(content_pack(), coverage_plan())
    )

    assert errors == [
        "generated note cites evidence outside draft scope: tr_0002",
        "generated note cites evidence outside draft scope: tr_0003",
        "generated note cites evidence outside draft scope: tr_0004",
    ]


@pytest.mark.parametrize(
    ("identity_field", "other_value", "expected"),
    [
        (
            "task_id",
            "other-task",
            "generated note task_id does not match draft evidence scope",
        ),
        (
            "source_fingerprint",
            "other-fingerprint",
            "generated note source_fingerprint does not match draft evidence scope",
        ),
    ],
)
def test_scope_rejects_cross_task_note_even_when_evidence_ids_match(
    identity_field: str,
    other_value: str,
    expected: str,
) -> None:
    note = scope_identity_note().model_copy(update={identity_field: other_value})

    errors = validate_note_citations_against_scope(
        note, build_draft_evidence_scope(content_pack(), coverage_plan())
    )

    assert errors == [expected]


def test_scope_builder_rejects_ai_supplement_plan_anchor() -> None:
    invalid_plan = CoveragePlan.model_validate(
        {
            **coverage_plan().model_dump(),
            "units": [
                {
                    "label": "plan label sentinel",
                    "source_evidence_ids": ["ai_0001"],
                    "rationale": "无效锚点。",
                }
            ],
        }
    )

    with pytest.raises(
        ValueError,
        match="coverage plan cannot use AI supplement evidence: ai_0001",
    ):
        build_draft_evidence_scope(content_pack(), invalid_plan)


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {
                "units": [
                    {
                        "label": "plan label sentinel",
                        "source_evidence_ids": ["tr_0001"],
                        "rationale": "第一项。",
                    },
                    {
                        "label": "plan label sentinel",
                        "source_evidence_ids": ["tr_0002"],
                        "rationale": "重复项。",
                    },
                ]
            },
            "coverage plan has duplicate unit label: plan label sentinel",
        ),
        (
            {
                "visuals": [
                    {
                        "frame_evidence_id": "tr_0001",
                        "supporting_ocr_evidence_ids": [],
                        "disposition": "use",
                        "unit_label": "plan label sentinel",
                        "rationale": "错误地把字幕当作图片。",
                    },
                    coverage_plan().model_dump()["visuals"][1],
                ]
            },
            "coverage plan visual evidence is not a frame: tr_0001",
        ),
    ],
)
def test_scope_builder_rejects_other_semantically_invalid_coverage_plans(
    updates: dict[str, object],
    expected: str,
) -> None:
    invalid_plan = CoveragePlan.model_validate(
        {**coverage_plan().model_dump(), **updates}
    )

    with pytest.raises(ValueError, match=expected):
        build_draft_evidence_scope(content_pack(), invalid_plan)


@pytest.mark.parametrize(
    "note",
    [
        legacy_note(),
        GENERATED_NOTE_ADAPTER.validate_python(concept_payload()),
        GENERATED_NOTE_ADAPTER.validate_python(resource_payload()),
        GENERATED_NOTE_ADAPTER.validate_python(practical_payload()),
    ],
    ids=["legacy_v2", "concept_v3", "resource_v3", "practical_v3"],
)
def test_factual_statement_entry_paths_match_existing_review_manifest(
    note: AnyGeneratedNote,
) -> None:
    assert [path for path, _ in iter_factual_statement_entries(note)] == [
        str(item["candidate_path"]) for item in build_statement_manifest(note)
    ]


def test_statement_packet_contains_only_its_own_cited_snippets() -> None:
    packet = build_statement_citation_packet(packet_note(), content_pack())
    item = packet.statements[0]

    assert packet.schema_version == "1.2"
    assert item.candidate_path == "/summary"
    assert item.statement_text == "选择的字幕和图片文字共同说明概念。"
    assert (
        item.statement_sha256
        == hashlib.sha256(item.statement_text.encode("utf-8")).hexdigest()
    )
    assert item.evidence_ids == ["tr_0001", "fr_0001", "ocr_0001"]
    assert [snippet.id for snippet in item.snippets] == ["tr_0001", "ocr_0001"]
    assert item.structural_evidence_ids == ["fr_0001"]
    assert [clause.model_dump() for clause in item.clauses] == [
        {
            "clause_id": "clause-0001-0001",
            "start": 0,
            "end": len(item.statement_text),
            "text": item.statement_text,
        }
    ]
    assert [
        clause.clause_id
        for statement in packet.statements
        for clause in statement.clauses
    ] == ["clause-0001-0001", "clause-0002-0001"]
    assert "tr_0002" not in item.model_dump_json()
    assert "另一条 statement 使用未选字幕。" not in item.model_dump_json()


def test_statement_packet_v12_keeps_program_generated_sentence_clauses() -> None:
    packet = build_statement_citation_packet(packet_note(), content_pack())

    parsed = parse_statement_citation_packet(packet.model_dump(mode="json"))
    parsed_v12 = parse_statement_citation_packet_v12(packet.model_dump_json())
    tampered_clause = packet.statements[0].clauses[0].model_copy(update={"start": 1})
    tampered_statement = packet.statements[0].model_copy(
        update={"clauses": [tampered_clause]}
    )
    tampered_packet = packet.model_copy(
        update={"statements": [tampered_statement, *packet.statements[1:]]}
    )

    assert isinstance(packet, StatementCitationPacketV12)
    assert parsed == packet
    assert parsed_v12 == packet
    assert validate_statement_citation_packet_v12(packet) == []
    assert validate_statement_citation_packet_v12(tampered_packet) == [
        "statement citation packet V1.2 clauses do not match "
        "program-generated sentence boundaries: /summary"
    ]


def test_statement_packet_v12_splits_program_owned_sentence_clauses() -> None:
    payload = packet_note().model_dump(mode="json")
    summary = payload["summary"]
    assert isinstance(summary, dict)
    summary["text"] = "字幕说明核心概念。图片文字补充流程。"
    note = ConceptExplanationNote.model_validate(payload)

    packet = build_statement_citation_packet(note, content_pack())
    item = packet.statements[0]

    assert isinstance(packet, StatementCitationPacketV12)
    assert [clause.clause_id for clause in item.clauses] == [
        "clause-0001-0001",
        "clause-0001-0002",
    ]
    assert [clause.text for clause in item.clauses] == [
        "字幕说明核心概念。",
        "图片文字补充流程。",
    ]
    assert "".join(clause.text for clause in item.clauses) == item.statement_text
    assert item.clauses[0].start == 0
    assert item.clauses[0].end == item.clauses[1].start
    assert item.clauses[1].end == len(item.statement_text)
    assert parse_statement_citation_packet(packet) == packet
    assert parse_statement_citation_packet_v12(packet.model_dump_json()) == packet
    assert validate_statement_citation_packet_v12(packet) == []


def test_program_sentence_clauses_keep_a_hard_newline_after_punctuation() -> None:
    clauses = _build_program_sentence_clauses(1, "第一句。\n第二句。")

    assert [clause.text for clause in clauses] == ["第一句。\n", "第二句。"]
    assert [(clause.start, clause.end) for clause in clauses] == [(0, 5), (5, 9)]
    assert "".join(clause.text for clause in clauses) == "第一句。\n第二句。"


def test_statement_packet_v12_parser_does_not_upgrade_a_v11_packet() -> None:
    packet = build_statement_citation_packet(packet_note(), content_pack())
    payload = packet.model_dump(mode="json")
    payload["schema_version"] = "1.1"
    legacy_v11 = StatementCitationPacketV11.model_validate(payload)

    with pytest.raises(ValueError, match="expected schema_version 1.2"):
        parse_statement_citation_packet_v12(legacy_v11.model_dump_json())


def test_statement_packet_v10_alias_and_v11_parser_keep_version_boundary() -> None:
    legacy_payload = {
        "schema_version": "1.0",
        "task_id": "legacy-task",
        "source_fingerprint": "legacy-fingerprint",
        "candidate_sha256": "a" * 64,
        "statements": [],
    }
    legacy_packet = StatementCitationPacket.model_validate(legacy_payload)

    assert isinstance(legacy_packet, StatementCitationPacketV10)
    assert parse_statement_citation_packet(legacy_payload) == legacy_packet
    with pytest.raises(ValueError, match="expected schema_version 1.1"):
        parse_statement_citation_packet_v11(legacy_payload)


@pytest.mark.parametrize(
    ("identity_field", "other_value", "expected"),
    [
        (
            "task_id",
            "other-task",
            "generated note task_id does not match content pack",
        ),
        (
            "source_fingerprint",
            "other-fingerprint",
            "generated note source_fingerprint does not match content pack",
        ),
    ],
)
def test_statement_packet_rejects_cross_pack_identity_with_same_evidence_ids(
    identity_field: str,
    other_value: str,
    expected: str,
) -> None:
    other_pack = content_pack().model_copy(update={identity_field: other_value})

    with pytest.raises(ValueError, match=expected):
        build_statement_citation_packet(packet_note(), other_pack)
