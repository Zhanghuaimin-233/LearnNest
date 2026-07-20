from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from learnnest.models import ContentPack, Evidence
from learnnest.note_models import ConceptExplanationNote


def content_pack() -> ContentPack:
    return ContentPack(
        task_id="20260711-a1b2c3d4",
        source_fingerprint="a1b2c3d4",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="先解释基础概念。",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="tr_0002",
                kind="transcript",
                start_ms=1_000,
                end_ms=2_000,
                text="再解释进阶机制。",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="ai_0001",
                kind="ai_supplement",
                text="模型补充。",
                artifact_path="content_pack.json",
            ),
        ],
    )


def candidate_note() -> ConceptExplanationNote:
    return ConceptExplanationNote.model_validate(
        {
            "schema_version": "3.0",
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "classification_evidence_ids": ["tr_0001"],
            "note_type": "concept_explanation",
            "title": "概念说明",
            "summary": {"text": "材料解释了核心概念。", "evidence_ids": ["tr_0001"]},
            "concepts": [
                {
                    "title": "基础概念",
                    "explanation": {
                        "text": "先解释基础概念。",
                        "evidence_ids": ["tr_0001"],
                    },
                }
            ],
            "background": None,
            "relationships": [],
            "misconceptions": [],
            "review": None,
            "ai_supplements": [],
        }
    )


def review_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.1",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "verdict": "approve",
        "coverage_units": [
            {
                "label": "基础概念",
                "source_evidence_ids": ["tr_0001"],
                "candidate_paths": ["/concepts/0/explanation"],
                "status": "covered",
                "rationale": "候选有独立解释。",
            }
        ],
        "statement_audits": [
            {
                "candidate_path": "/summary",
                "status": "aligned",
                "rationale": "摘要证据直接支持正文。",
            },
            {
                "candidate_path": "/concepts/0/explanation",
                "status": "aligned",
                "rationale": "概念证据直接支持正文。",
            },
        ],
        "issues": [],
    }
    payload.update(updates)
    return payload


def test_parse_and_validate_note_review_accepts_approve_and_reject() -> None:
    from learnnest.note_review import parse_note_review, validate_note_review

    approved = parse_note_review(json.dumps(review_payload(), ensure_ascii=False))
    rejected = parse_note_review(
        json.dumps(
            review_payload(
                verdict="reject",
                coverage_units=[
                    {
                        "label": "进阶机制",
                        "source_evidence_ids": ["tr_0002"],
                        "candidate_paths": [],
                        "status": "missing",
                        "rationale": "候选没有解释该中央单元。",
                    }
                ],
            ),
            ensure_ascii=False,
        )
    )

    assert validate_note_review(approved, content_pack(), candidate_note()) == []
    assert validate_note_review(rejected, content_pack(), candidate_note()) == []


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"task_id": "other"}, "task_id does not match"),
        ({"source_fingerprint": "other"}, "source_fingerprint does not match"),
        (
            {
                "coverage_units": [
                    {
                        "label": "未知证据",
                        "source_evidence_ids": ["tr_9999"],
                        "candidate_paths": ["/concepts/0/explanation"],
                        "status": "covered",
                        "rationale": "无效。",
                    }
                ]
            },
            "unknown source evidence id: tr_9999",
        ),
        (
            {
                "coverage_units": [
                    {
                        "label": "重复证据",
                        "source_evidence_ids": ["tr_0001", "tr_0001"],
                        "candidate_paths": ["/concepts/0/explanation"],
                        "status": "covered",
                        "rationale": "无效。",
                    }
                ]
            },
            "duplicate source evidence id: tr_0001",
        ),
        (
            {
                "coverage_units": [
                    {
                        "label": "AI 补充",
                        "source_evidence_ids": ["ai_0001"],
                        "candidate_paths": ["/concepts/0/explanation"],
                        "status": "covered",
                        "rationale": "无效。",
                    }
                ]
            },
            "cannot use AI supplement evidence: ai_0001",
        ),
        (
            {
                "coverage_units": [
                    {
                        "label": "错误路径",
                        "source_evidence_ids": ["tr_0001"],
                        "candidate_paths": ["/concepts/9/explanation"],
                        "status": "covered",
                        "rationale": "无效。",
                    }
                ]
            },
            "candidate path does not resolve: /concepts/9/explanation",
        ),
        (
            {
                "coverage_units": [
                    {
                        "label": "非 statement 路径",
                        "source_evidence_ids": ["tr_0001"],
                        "candidate_paths": ["/concepts/0/explanation/text"],
                        "status": "covered",
                        "rationale": "路径只指向文本值。",
                    }
                ]
            },
            "covered candidate path is not a factual statement",
        ),
    ],
)
def test_validate_note_review_reports_stable_consistency_errors(
    updates: dict[str, object], expected: str
) -> None:
    from learnnest.note_review import parse_note_review, validate_note_review

    review = parse_note_review(
        json.dumps(review_payload(**updates), ensure_ascii=False)
    )

    errors = validate_note_review(review, content_pack(), candidate_note())
    assert any(expected in error for error in errors)


@pytest.mark.parametrize(
    "payload",
    [
        review_payload(
            verdict="approve",
            issues=[
                {
                    "category": "semantic_duplication",
                    "candidate_paths": ["/summary", "/concepts/0/explanation"],
                    "source_evidence_ids": ["tr_0001"],
                    "rationale": "两个字段重复。",
                }
            ],
        ),
        review_payload(verdict="reject"),
        review_payload(
            coverage_units=[
                {
                    "label": "缺失单元",
                    "source_evidence_ids": ["tr_0002"],
                    "candidate_paths": [],
                    "status": "missing",
                    "rationale": "候选缺失。",
                }
            ]
        ),
        review_payload(
            verdict="reject",
            coverage_units=[
                {
                    "label": "浅解释",
                    "source_evidence_ids": ["tr_0002"],
                    "candidate_paths": [],
                    "status": "shallow",
                    "rationale": "状态缺少候选路径。",
                }
            ],
        ),
        review_payload(
            coverage_units=[
                {
                    "label": "非 JSON Pointer",
                    "source_evidence_ids": ["tr_0001"],
                    "candidate_paths": ["concepts[0].explanation"],
                    "status": "covered",
                    "rationale": "路径格式错误。",
                }
            ],
        ),
        review_payload(
            coverage_units=[
                {
                    "label": "过多锚点",
                    "source_evidence_ids": ["tr_0001", "tr_0002", "a", "b"],
                    "candidate_paths": ["/concepts/0/explanation"],
                    "status": "covered",
                    "rationale": "超过冻结锚点上限。",
                }
            ],
        ),
    ],
)
def test_note_review_rejects_inconsistent_verdict_status_and_paths(
    payload: dict[str, object],
) -> None:
    from learnnest.note_review import parse_note_review

    with pytest.raises(ValidationError):
        parse_note_review(json.dumps(payload, ensure_ascii=False))


def test_note_review_forbids_extra_fields() -> None:
    from learnnest.note_review import parse_note_review

    with pytest.raises(ValidationError):
        parse_note_review(
            json.dumps(review_payload(unexpected="value"), ensure_ascii=False)
        )


def test_statement_manifest_enumerates_every_factual_statement_in_order() -> None:
    from learnnest.note_review import build_statement_manifest

    assert build_statement_manifest(candidate_note()) == [
        {
            "candidate_path": "/summary",
            "text": "材料解释了核心概念。",
            "evidence_ids": ["tr_0001"],
        },
        {
            "candidate_path": "/concepts/0/explanation",
            "text": "先解释基础概念。",
            "evidence_ids": ["tr_0001"],
        },
    ]


@pytest.mark.parametrize(
    "statement_audits",
    [
        [
            {
                "candidate_path": "/summary",
                "status": "aligned",
                "rationale": "只审了摘要。",
            }
        ],
        [
            {
                "candidate_path": "/concepts/0/explanation",
                "status": "aligned",
                "rationale": "顺序错误。",
            },
            {
                "candidate_path": "/summary",
                "status": "aligned",
                "rationale": "顺序错误。",
            },
        ],
    ],
)
def test_validate_note_review_requires_exact_statement_manifest(
    statement_audits: list[dict[str, object]],
) -> None:
    from learnnest.note_review import parse_note_review, validate_note_review

    review = parse_note_review(
        json.dumps(
            review_payload(statement_audits=statement_audits),
            ensure_ascii=False,
        )
    )

    assert "note review statement audits do not match candidate manifest" in (
        validate_note_review(review, content_pack(), candidate_note())
    )


def test_approve_review_rejects_a_misaligned_statement_audit() -> None:
    from learnnest.note_review import parse_note_review

    audits = review_payload()["statement_audits"]
    assert isinstance(audits, list)
    audits[0] = {
        "candidate_path": "/summary",
        "status": "misaligned",
        "rationale": "摘要含无直接证据的推论。",
    }

    with pytest.raises(ValidationError):
        parse_note_review(
            json.dumps(
                review_payload(statement_audits=audits),
                ensure_ascii=False,
            )
        )


def test_missing_coverage_can_point_to_a_non_substantive_mention() -> None:
    from learnnest.note_review import parse_note_review, validate_note_review

    review = parse_note_review(
        json.dumps(
            review_payload(
                verdict="reject",
                coverage_units=[
                    {
                        "label": "只被点名的进阶机制",
                        "source_evidence_ids": ["tr_0002"],
                        "candidate_paths": ["/summary"],
                        "status": "missing",
                        "rationale": "候选只点名，没有实质解释。",
                    }
                ],
            ),
            ensure_ascii=False,
        )
    )

    assert validate_note_review(review, content_pack(), candidate_note()) == []
