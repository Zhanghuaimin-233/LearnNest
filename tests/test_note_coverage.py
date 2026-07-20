from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from learnnest.models import ContentPack, Evidence


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
                text="基础概念",
                artifact_path="ocr.json",
                frame_id="fr_0001",
            ),
            Evidence(
                id="fr_0002",
                kind="frame",
                start_ms=1_500,
                artifact_path="frames/selected/fr_0002.png",
                related_evidence_ids=["ocr_0002"],
            ),
            Evidence(
                id="ocr_0002",
                kind="ocr",
                text="进阶机制",
                artifact_path="ocr.json",
                frame_id="fr_0002",
            ),
        ],
    )


def content_pack_without_frames() -> ContentPack:
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
        ],
    )


def plan_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.1",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "units": [
            {
                "label": "基础概念",
                "source_evidence_ids": ["tr_0001"],
                "rationale": "材料首先定义了基础概念。",
            },
            {
                "label": "进阶机制",
                "source_evidence_ids": ["tr_0002"],
                "rationale": "材料随后解释进阶机制。",
            },
        ],
        "visuals": [
            {
                "frame_evidence_id": "fr_0001",
                "supporting_ocr_evidence_ids": ["ocr_0001"],
                "disposition": "use",
                "unit_label": "基础概念",
                "rationale": "画面直接展示了基础概念。",
            },
            {
                "frame_evidence_id": "fr_0002",
                "supporting_ocr_evidence_ids": [],
                "disposition": "omit",
                "unit_label": None,
                "rationale": "画面只重复旁白，没有额外信息。",
            },
        ],
    }
    payload.update(updates)
    return payload


def review_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.1",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "verdict": "reject",
        "coverage_units": [
            {
                "label": "基础概念",
                "source_evidence_ids": ["tr_0001"],
                "candidate_paths": ["/concepts/0/explanation"],
                "status": "covered",
                "rationale": "候选笔记完整覆盖。",
            },
            {
                "label": "进阶机制",
                "source_evidence_ids": ["tr_0002"],
                "candidate_paths": [],
                "status": "missing",
                "rationale": "候选笔记没有覆盖。",
            },
        ],
        "statement_audits": [
            {
                "candidate_path": "/summary",
                "status": "aligned",
                "rationale": "候选总结与证据一致。",
            }
        ],
        "issues": [],
    }
    payload.update(updates)
    return payload


def candidate_note_payload(*, statements: list[list[str]]) -> dict[str, object]:
    first, *remaining = statements
    key_point_evidence = remaining or [["tr_0001"]]
    return {
        "schema_version": "2.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "title": "学习笔记",
        "audience": {"text": "学习者", "evidence_ids": ["tr_0001"]},
        "summary": {"text": "核心总结。", "evidence_ids": first},
        "key_points": [
            {"text": f"补充说明 {index}。", "evidence_ids": evidence_ids}
            for index, evidence_ids in enumerate(key_point_evidence, start=1)
        ],
        "steps": [],
        "cautions": [],
        "ai_supplements": [],
    }


def test_parse_and_validate_source_only_coverage_plan() -> None:
    from learnnest.note_coverage import parse_coverage_plan, validate_coverage_plan

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))

    assert [unit.label for unit in plan.units] == ["基础概念", "进阶机制"]
    assert validate_coverage_plan(plan, content_pack()) == []


@pytest.mark.parametrize(
    "payload",
    [
        plan_payload(schema_version="2.0"),
        plan_payload(unexpected="value"),
        plan_payload(units=[]),
        plan_payload(
            units=[
                {
                    "label": "基础概念",
                    "source_evidence_ids": [],
                    "rationale": "材料定义了基础概念。",
                }
            ]
        ),
        plan_payload(
            units=[
                {
                    "label": "基础概念",
                    "source_evidence_ids": [
                        "tr_0001",
                        "tr_0002",
                        "ocr_0001",
                        "ocr_0002",
                    ],
                    "rationale": "锚点超过一个局部 statement 可验证的范围。",
                }
            ]
        ),
        plan_payload(
            units=[
                {
                    "label": "基础概念",
                    "source_evidence_ids": ["tr_0001"],
                    "rationale": "材料定义了基础概念。",
                    "unexpected": "value",
                }
            ]
        ),
    ],
)
def test_coverage_plan_rejects_invalid_schema_and_extra_fields(
    payload: dict[str, object],
) -> None:
    from learnnest.note_coverage import parse_coverage_plan

    with pytest.raises(ValidationError):
        parse_coverage_plan(json.dumps(payload, ensure_ascii=False))


def test_generated_coverage_plan_schema_is_strict_at_both_levels() -> None:
    from learnnest.note_coverage import generated_coverage_plan_json_schema

    schema = generated_coverage_plan_json_schema()

    assert schema["additionalProperties"] is False
    unit_ref = schema["properties"]["units"]["items"]["$ref"]
    unit_name = unit_ref.rsplit("/", maxsplit=1)[-1]
    assert schema["$defs"][unit_name]["additionalProperties"] is False
    assert schema["properties"]["units"]["minItems"] == 1
    visual_ref = schema["properties"]["visuals"]["items"]["$ref"]
    visual_name = visual_ref.rsplit("/", maxsplit=1)[-1]
    assert schema["$defs"][visual_name]["additionalProperties"] is False
    assert "visuals" in schema["required"]
    assert (
        schema["$defs"][unit_name]["properties"]["source_evidence_ids"]["maxItems"] == 3
    )
    assert (
        schema["$defs"][visual_name]["properties"]["supporting_ocr_evidence_ids"][
            "maxItems"
        ]
        == 3
    )


@pytest.mark.parametrize(
    "payload",
    [
        {key: value for key, value in plan_payload().items() if key != "visuals"},
        plan_payload(
            visuals=[
                {
                    **plan_payload()["visuals"][0],
                    "unexpected": "value",
                }
            ]
        ),
        plan_payload(
            visuals=[
                {
                    **plan_payload()["visuals"][0],
                    "disposition": "maybe",
                }
            ]
        ),
        plan_payload(
            visuals=[
                {
                    **plan_payload()["visuals"][0],
                    "unit_label": None,
                }
            ]
        ),
        plan_payload(
            visuals=[
                {
                    **plan_payload()["visuals"][1],
                    "unit_label": "进阶机制",
                }
            ]
        ),
        plan_payload(
            visuals=[
                {
                    **plan_payload()["visuals"][0],
                    "supporting_ocr_evidence_ids": [
                        "ocr_0001",
                        "ocr_0002",
                        "ocr_0001",
                        "ocr_0002",
                    ],
                },
                plan_payload()["visuals"][1],
            ]
        ),
        plan_payload(
            visuals=[
                {
                    **plan_payload()["visuals"][1],
                    "supporting_ocr_evidence_ids": ["ocr_0002"],
                },
                plan_payload()["visuals"][0],
            ]
        ),
    ],
)
def test_coverage_plan_visual_contract_is_strict(
    payload: dict[str, object],
) -> None:
    from learnnest.note_coverage import parse_coverage_plan

    with pytest.raises(ValidationError):
        parse_coverage_plan(json.dumps(payload, ensure_ascii=False))


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"task_id": "other"}, "task_id does not match content pack"),
        (
            {"source_fingerprint": "other"},
            "source_fingerprint does not match content pack",
        ),
        (
            {
                "units": [
                    {
                        "label": "未知证据",
                        "source_evidence_ids": ["tr_9999"],
                        "rationale": "无效。",
                    }
                ]
            },
            "unknown source evidence id: tr_9999",
        ),
        (
            {
                "units": [
                    {
                        "label": "AI 补充",
                        "source_evidence_ids": ["ai_0001"],
                        "rationale": "无效。",
                    }
                ]
            },
            "cannot use AI supplement evidence: ai_0001",
        ),
        (
            {
                "units": [
                    {
                        "label": "重复证据",
                        "source_evidence_ids": ["tr_0001", "tr_0001"],
                        "rationale": "无效。",
                    }
                ]
            },
            "duplicate source evidence id: tr_0001",
        ),
    ],
)
def test_validate_coverage_plan_reports_stable_consistency_errors(
    updates: dict[str, object], expected: str
) -> None:
    from learnnest.note_coverage import parse_coverage_plan, validate_coverage_plan

    plan = parse_coverage_plan(json.dumps(plan_payload(**updates), ensure_ascii=False))

    assert any(
        expected in error for error in validate_coverage_plan(plan, content_pack())
    )


def test_validate_coverage_plan_allows_same_evidence_in_different_units() -> None:
    from learnnest.note_coverage import parse_coverage_plan, validate_coverage_plan

    payload = plan_payload()
    units = payload["units"]
    assert isinstance(units, list)
    units[1]["source_evidence_ids"] = ["tr_0001"]
    plan = parse_coverage_plan(json.dumps(payload, ensure_ascii=False))

    assert validate_coverage_plan(plan, content_pack()) == []


def test_validate_coverage_plan_allows_empty_visuals_only_without_frames() -> None:
    from learnnest.note_coverage import parse_coverage_plan, validate_coverage_plan

    plan = parse_coverage_plan(json.dumps(plan_payload(visuals=[]), ensure_ascii=False))

    assert validate_coverage_plan(plan, content_pack_without_frames()) == []


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {
                "units": [
                    {
                        "label": "重复标签",
                        "source_evidence_ids": ["tr_0001"],
                        "rationale": "第一项。",
                    },
                    {
                        "label": "重复标签",
                        "source_evidence_ids": ["tr_0002"],
                        "rationale": "第二项。",
                    },
                ],
                "visuals": [
                    {
                        **plan_payload()["visuals"][0],
                        "unit_label": "重复标签",
                    },
                    plan_payload()["visuals"][1],
                ],
            },
            "coverage plan has duplicate unit label: 重复标签",
        ),
        (
            {"visuals": [plan_payload()["visuals"][0]]},
            "coverage plan is missing frame visual decision: fr_0002",
        ),
        (
            {
                "visuals": [
                    plan_payload()["visuals"][0],
                    {
                        **plan_payload()["visuals"][1],
                        "frame_evidence_id": "fr_0001",
                    },
                ]
            },
            "coverage plan has duplicate frame visual decision: fr_0001",
        ),
        (
            {
                "visuals": [
                    plan_payload()["visuals"][0],
                    {
                        **plan_payload()["visuals"][1],
                        "frame_evidence_id": "fr_9999",
                    },
                ]
            },
            "coverage plan has unknown frame visual evidence id: fr_9999",
        ),
        (
            {
                "visuals": [
                    plan_payload()["visuals"][0],
                    {
                        **plan_payload()["visuals"][1],
                        "frame_evidence_id": "tr_0002",
                    },
                ]
            },
            "coverage plan visual evidence is not a frame: tr_0002",
        ),
        (
            {
                "visuals": [
                    {
                        **plan_payload()["visuals"][0],
                        "unit_label": "未知单元",
                    },
                    plan_payload()["visuals"][1],
                ]
            },
            "coverage plan visual unit label does not resolve uniquely: 未知单元",
        ),
        (
            {
                "visuals": [
                    {
                        **plan_payload()["visuals"][0],
                        "supporting_ocr_evidence_ids": ["ocr_0001", "ocr_0001"],
                    },
                    plan_payload()["visuals"][1],
                ]
            },
            "coverage plan visual has duplicate supporting OCR evidence id: ocr_0001",
        ),
        (
            {
                "visuals": [
                    {
                        **plan_payload()["visuals"][0],
                        "supporting_ocr_evidence_ids": ["ocr_9999"],
                    },
                    plan_payload()["visuals"][1],
                ]
            },
            "coverage plan visual has unknown supporting OCR evidence id: ocr_9999",
        ),
        (
            {
                "visuals": [
                    {
                        **plan_payload()["visuals"][0],
                        "supporting_ocr_evidence_ids": ["tr_0001"],
                    },
                    plan_payload()["visuals"][1],
                ]
            },
            "coverage plan supporting visual evidence is not OCR: tr_0001",
        ),
        (
            {
                "visuals": [
                    {
                        **plan_payload()["visuals"][0],
                        "supporting_ocr_evidence_ids": ["ai_0001"],
                    },
                    plan_payload()["visuals"][1],
                ]
            },
            "coverage plan visual cannot use AI supplement evidence: ai_0001",
        ),
        (
            {
                "visuals": [
                    {
                        **plan_payload()["visuals"][0],
                        "supporting_ocr_evidence_ids": ["ocr_0002"],
                    },
                    plan_payload()["visuals"][1],
                ]
            },
            "coverage plan supporting OCR parent does not match frame: ocr_0002",
        ),
    ],
)
def test_validate_coverage_plan_enforces_all_frame_visual_decisions(
    updates: dict[str, object], expected: str
) -> None:
    from learnnest.note_coverage import parse_coverage_plan, validate_coverage_plan

    plan = parse_coverage_plan(json.dumps(plan_payload(**updates), ensure_ascii=False))

    assert expected in validate_coverage_plan(plan, content_pack())


def test_validate_candidate_visuals_accepts_one_statement_with_frame_and_ocr() -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_candidate_visuals_against_plan,
    )
    from learnnest.note_validation import parse_generated_note

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(
            candidate_note_payload(statements=[["tr_0001", "fr_0001", "ocr_0001"]]),
            ensure_ascii=False,
        )
    )

    assert validate_candidate_visuals_against_plan(candidate, plan) == []


@pytest.mark.parametrize(
    "statements",
    [
        [["tr_0001", "fr_0001"]],
        [["tr_0001", "fr_0001"], ["tr_0002", "ocr_0001"]],
        [
            ["tr_0001", "fr_0001"],
            ["tr_0002", "fr_0001", "ocr_0001"],
        ],
    ],
)
def test_validate_candidate_visuals_requires_frame_and_ocr_in_same_statement(
    statements: list[list[str]],
) -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_candidate_visuals_against_plan,
    )
    from learnnest.note_validation import parse_generated_note

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(candidate_note_payload(statements=statements), ensure_ascii=False)
    )

    assert validate_candidate_visuals_against_plan(candidate, plan) == [
        "candidate does not cite planned visual evidence together: fr_0001"
    ]


def test_validate_candidate_visuals_does_not_require_omitted_frames() -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_candidate_visuals_against_plan,
    )
    from learnnest.note_validation import parse_generated_note

    payload = plan_payload()
    visuals = payload["visuals"]
    assert isinstance(visuals, list)
    visuals[0]["disposition"] = "omit"
    visuals[0]["unit_label"] = None
    visuals[0]["supporting_ocr_evidence_ids"] = []
    plan = parse_coverage_plan(json.dumps(payload, ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(
            candidate_note_payload(statements=[["tr_0001"]]),
            ensure_ascii=False,
        )
    )

    assert validate_candidate_visuals_against_plan(candidate, plan) == []


def test_validate_candidate_visuals_rejects_a_frame_frozen_as_omitted() -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_candidate_visuals_against_plan,
    )
    from learnnest.note_validation import parse_generated_note

    payload = plan_payload()
    visuals = payload["visuals"]
    assert isinstance(visuals, list)
    visuals[0]["disposition"] = "omit"
    visuals[0]["unit_label"] = None
    visuals[0]["supporting_ocr_evidence_ids"] = []
    plan = parse_coverage_plan(json.dumps(payload, ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(
            candidate_note_payload(statements=[["tr_0001", "fr_0001"]]),
            ensure_ascii=False,
        )
    )

    assert validate_candidate_visuals_against_plan(candidate, plan) == [
        "candidate cites frame planned for omission: fr_0001"
    ]


def test_validate_candidate_visuals_requires_mapped_unit_anchor_at_first_frame() -> (
    None
):
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_candidate_visuals_against_plan,
    )
    from learnnest.note_validation import parse_generated_note

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(
            candidate_note_payload(
                statements=[
                    ["fr_0001", "ocr_0001"],
                    ["tr_0001"],
                ]
            ),
            ensure_ascii=False,
        )
    )

    assert validate_candidate_visuals_against_plan(candidate, plan) == [
        "candidate does not cite a mapped unit anchor with planned visual evidence: "
        "fr_0001"
    ]


def test_validate_candidate_visuals_allows_one_mapped_unit_anchor_at_first_frame() -> (
    None
):
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_candidate_visuals_against_plan,
    )
    from learnnest.note_validation import parse_generated_note

    payload = plan_payload()
    units = payload["units"]
    assert isinstance(units, list)
    units[0]["source_evidence_ids"] = ["tr_0001", "tr_0002"]
    plan = parse_coverage_plan(json.dumps(payload, ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(
            candidate_note_payload(
                statements=[
                    ["tr_0001", "fr_0001", "ocr_0001"],
                    ["tr_0001", "tr_0002"],
                ]
            ),
            ensure_ascii=False,
        )
    )

    assert validate_candidate_visuals_against_plan(candidate, plan) == []


def test_candidate_source_coverage_requires_each_unit_in_one_local_statement() -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_candidate_source_coverage_against_plan,
    )
    from learnnest.note_validation import parse_generated_note

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(
            candidate_note_payload(statements=[["tr_0001"], ["tr_0001"]]),
            ensure_ascii=False,
        )
    )

    assert validate_candidate_source_coverage_against_plan(candidate, plan) == [
        "candidate does not cite coverage unit source evidence together: 2"
    ]


def test_candidate_source_coverage_accepts_separate_local_statements() -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_candidate_source_coverage_against_plan,
    )
    from learnnest.note_validation import parse_generated_note

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(
            candidate_note_payload(statements=[["tr_0001"], ["tr_0002"]]),
            ensure_ascii=False,
        )
    )

    assert validate_candidate_source_coverage_against_plan(candidate, plan) == []


def test_validate_review_against_plan_accepts_review_owned_fields() -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_review_against_plan,
    )
    from learnnest.note_review import parse_note_review

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))
    review = parse_note_review(json.dumps(review_payload(), ensure_ascii=False))

    assert validate_review_against_plan(review, plan) == []


@pytest.mark.parametrize(
    ("coverage_units", "expected"),
    [
        (
            [review_payload()["coverage_units"][1]],
            "note review coverage unit count does not match plan",
        ),
        (
            list(reversed(review_payload()["coverage_units"])),
            "note review coverage unit 1 label does not match plan",
        ),
        (
            [
                {
                    **review_payload()["coverage_units"][0],
                    "source_evidence_ids": ["tr_0002"],
                },
                review_payload()["coverage_units"][1],
            ],
            "note review coverage unit 1 source_evidence_ids do not match plan",
        ),
    ],
)
def test_validate_review_against_plan_rejects_changed_frozen_units(
    coverage_units: list[object], expected: str
) -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_review_against_plan,
    )
    from learnnest.note_review import parse_note_review

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))
    review = parse_note_review(
        json.dumps(review_payload(coverage_units=coverage_units), ensure_ascii=False)
    )

    assert expected in validate_review_against_plan(review, plan)


def test_reviewed_candidate_must_cite_each_units_frozen_source_evidence() -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_review_against_plan,
    )
    from learnnest.note_review import parse_note_review
    from learnnest.note_validation import parse_generated_note

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))
    payload = review_payload(
        verdict="approve",
        coverage_units=[
            {
                **review_payload()["coverage_units"][0],
                "candidate_paths": ["/summary"],
            },
            {
                **review_payload()["coverage_units"][1],
                "candidate_paths": ["/key_points/0"],
                "status": "covered",
            },
        ],
    )
    review = parse_note_review(json.dumps(payload, ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(
            candidate_note_payload(
                statements=[
                    ["tr_0001", "fr_0001", "ocr_0001"],
                    ["tr_0001"],
                ]
            ),
            ensure_ascii=False,
        )
    )

    assert validate_review_against_plan(review, plan, candidate) == [
        "note review coverage unit 2 candidate statement does not cite all plan "
        "source evidence"
    ]


def test_reviewed_visual_must_be_first_cited_in_its_mapped_unit_statement() -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_review_against_plan,
    )
    from learnnest.note_review import parse_note_review
    from learnnest.note_validation import parse_generated_note

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))
    payload = review_payload(
        coverage_units=[
            {
                **review_payload()["coverage_units"][0],
                "candidate_paths": ["/key_points/0"],
            },
            review_payload()["coverage_units"][1],
        ]
    )
    review = parse_note_review(json.dumps(payload, ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(
            candidate_note_payload(
                statements=[
                    ["tr_0001", "fr_0001", "ocr_0001"],
                    ["tr_0001"],
                ]
            ),
            ensure_ascii=False,
        )
    )

    assert validate_review_against_plan(review, plan, candidate) == [
        "planned visual first citation is outside mapped coverage unit: fr_0001"
    ]


def test_reviewed_visual_first_statement_must_also_cite_a_mapped_unit_anchor() -> None:
    from learnnest.note_coverage import (
        parse_coverage_plan,
        validate_review_against_plan,
    )
    from learnnest.note_review import parse_note_review
    from learnnest.note_validation import parse_generated_note

    plan = parse_coverage_plan(json.dumps(plan_payload(), ensure_ascii=False))
    payload = review_payload(
        coverage_units=[
            {
                **review_payload()["coverage_units"][0],
                "candidate_paths": ["/summary", "/key_points/0"],
            },
            review_payload()["coverage_units"][1],
        ],
    )
    review = parse_note_review(json.dumps(payload, ensure_ascii=False))
    candidate = parse_generated_note(
        json.dumps(
            candidate_note_payload(
                statements=[
                    ["fr_0001", "ocr_0001"],
                    ["tr_0001"],
                ]
            ),
            ensure_ascii=False,
        )
    )

    assert validate_review_against_plan(review, plan, candidate) == [
        "planned visual first citation does not cite a mapped unit anchor: fr_0001"
    ]
