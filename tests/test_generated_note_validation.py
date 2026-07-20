from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from learnnest.models import ContentPack, Evidence
from learnnest.note_models import GeneratedNote


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
                text="打开设置，并访问 https://example.com/resource。",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="tr_0002",
                kind="transcript",
                start_ms=1_000,
                end_ms=2_000,
                text="保存配置。",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="tr_0003",
                kind="transcript",
                start_ms=2_000,
                end_ms=3_000,
                text="补全必填参数后再次保存。",
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
                text="模型配置 https://example.com/ocr-resource",
                artifact_path="ocr.json",
                frame_id="fr_0001",
            ),
            Evidence(
                id="ai_0001",
                kind="ai_supplement",
                text="不同版本入口名称可能变化。",
                artifact_path="content_pack.json",
            ),
        ],
    )


def note_payload() -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "title": "设置模型参数",
        "audience": {"text": "需要配置工具的学习者", "evidence_ids": ["tr_0001"]},
        "summary": {
            "text": "视频演示了打开设置并保存配置。",
            "evidence_ids": ["tr_0001", "fr_0001"],
        },
        "key_points": [{"text": "保存前确认模型名称。", "evidence_ids": ["ocr_0001"]}],
        "steps": [{"order": 1, "text": "打开设置页面。", "evidence_ids": ["tr_0001"]}],
        "cautions": [],
        "ai_supplements": [{"text": "不同版本入口名称可能变化。"}],
    }


def generated_note(**updates: object) -> GeneratedNote:
    payload = note_payload()
    payload.update(updates)
    return GeneratedNote.model_validate(payload)


def _v3_base_payload(
    *, classification_ids: list[str] | None = None
) -> dict[str, object]:
    return {
        "schema_version": "3.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "classification_evidence_ids": (
            ["tr_0001"] if classification_ids is None else classification_ids
        ),
        "title": "结构化学习笔记",
        "summary": {
            "text": "材料介绍了一个可复查的学习主题。",
            "evidence_ids": ["tr_0001"],
        },
        "ai_supplements": [],
    }


def concept_payload(
    *, classification_ids: list[str] | None = None
) -> dict[str, object]:
    return {
        **_v3_base_payload(classification_ids=classification_ids),
        "note_type": "concept_explanation",
        "concepts": [
            {
                "title": "核心概念",
                "explanation": {
                    "text": "核心概念由材料中的定义支持。",
                    "evidence_ids": ["tr_0001"],
                },
            }
        ],
        "background": {
            "text": "材料先介绍了使用背景。",
            "evidence_ids": ["tr_0001"],
        },
        "relationships": [
            {
                "text": "配置与保存操作前后相关。",
                "evidence_ids": ["tr_0002"],
            }
        ],
        "misconceptions": [
            {
                "text": "只打开页面并不等于保存配置。",
                "evidence_ids": ["tr_0002"],
            }
        ],
        "review": {
            "text": "复习时需要区分打开与保存。",
            "evidence_ids": ["tr_0001", "tr_0002"],
        },
    }


def resource_payload(
    *, url: str = "https://example.com/resource", locator_ids: list[str] | None = None
) -> dict[str, object]:
    return {
        **_v3_base_payload(),
        "note_type": "resource_share",
        "resources": [
            {
                "name": {
                    "text": "示例资源",
                    "evidence_ids": ["tr_0001"],
                },
                "value": {
                    "text": "该资源可用于复习材料中的主题。",
                    "evidence_ids": ["tr_0002"],
                },
                "suitable_for": {
                    "text": "适合需要复查配置的学习者。",
                    "evidence_ids": ["tr_0001"],
                },
                "access_or_usage": {
                    "text": "按材料演示的方法访问资源。",
                    "evidence_ids": ["tr_0001"],
                },
                "locator": {
                    "url": url,
                    "evidence_ids": (
                        ["tr_0001"] if locator_ids is None else locator_ids
                    ),
                },
                "limitations": [
                    {
                        "text": "使用前需要完成配置。",
                        "evidence_ids": ["tr_0002"],
                    }
                ],
            }
        ],
        "reminders": [
            {
                "text": "保存前确认模型名称。",
                "evidence_ids": ["ocr_0001", "fr_0001"],
            }
        ],
    }


def practical_payload() -> dict[str, object]:
    return {
        **_v3_base_payload(),
        "note_type": "practical_tutorial",
        "goal": {
            "text": "完成材料演示的配置流程。",
            "evidence_ids": ["tr_0001"],
        },
        "prerequisites": [
            {
                "text": "开始前准备好模型名称。",
                "evidence_ids": ["ocr_0001", "fr_0001"],
            }
        ],
        "steps": [
            {
                "order": 1,
                "title": "打开设置",
                "action": {
                    "text": "打开设置页面。",
                    "evidence_ids": ["tr_0001", "fr_0001"],
                },
                "expected_result": {
                    "text": "页面显示配置选项。",
                    "evidence_ids": ["fr_0001"],
                },
            }
        ],
        "troubleshooting": [
            {
                "symptom": {
                    "text": "保存按钮不可用。",
                    "evidence_ids": ["tr_0003"],
                },
                "resolution": {
                    "text": "补全必填参数后再次保存。",
                    "evidence_ids": ["tr_0003"],
                },
            }
        ],
        "completion_checks": [
            {
                "text": "确认配置已经保存。",
                "evidence_ids": ["tr_0002"],
            }
        ],
        "cautions": [
            {
                "text": "保存前确认模型名称。",
                "evidence_ids": ["ocr_0001", "fr_0001"],
            }
        ],
    }


def complete_payload_for_path(path: tuple[object, ...]) -> dict[str, object]:
    if path[0] in {
        "concepts",
        "background",
        "relationships",
        "misconceptions",
        "review",
        "classification_evidence_ids",
        "summary",
    }:
        return concept_payload()
    if path[0] in {"resources", "reminders"}:
        return resource_payload()
    return practical_payload()


def set_nested(
    payload: dict[str, object], path: tuple[object, ...], value: object
) -> None:
    target: Any = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def non_locator_url_cases() -> list[tuple[dict[str, object], tuple[object, ...]]]:
    supplement_payload = concept_payload()
    supplement_payload["ai_supplements"] = [{"text": "补充说明"}]
    return [
        (concept_payload(), ("title",)),
        (concept_payload(), ("concepts", 0, "title")),
        (practical_payload(), ("steps", 0, "title")),
        (supplement_payload, ("ai_supplements", 0, "text")),
    ]


def test_parse_generated_note_accepts_the_2_0_contract() -> None:
    from learnnest.note_validation import parse_generated_note

    note = parse_generated_note(json.dumps(note_payload(), ensure_ascii=False))

    assert note.schema_version == "2.0"
    assert note.task_id == "20260711-a1b2c3d4"


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (concept_payload(), "concept_explanation"),
        (resource_payload(), "resource_share"),
        (practical_payload(), "practical_tutorial"),
    ],
)
def test_parse_generated_note_accepts_all_3_0_contracts(
    payload: dict[str, object], expected_type: str
) -> None:
    from learnnest.note_validation import parse_generated_note

    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert note.schema_version == "3.0"
    assert note.note_type == expected_type


def test_v3_classification_evidence_accepts_twelve_items() -> None:
    from learnnest.note_validation import parse_generated_note

    evidence_ids = [f"tr_{index:04d}" for index in range(1, 13)]
    note = parse_generated_note(
        json.dumps(concept_payload(classification_ids=evidence_ids), ensure_ascii=False)
    )

    assert note.classification_evidence_ids == evidence_ids


def test_v3_classification_evidence_rejects_thirteen_items() -> None:
    from learnnest.note_validation import parse_generated_note

    evidence_ids = [f"tr_{index:04d}" for index in range(1, 14)]
    with pytest.raises(ValidationError) as captured:
        parse_generated_note(
            json.dumps(
                concept_payload(classification_ids=evidence_ids),
                ensure_ascii=False,
            )
        )

    error = next(
        item
        for item in captured.value.errors()
        if item["loc"][-1] == "classification_evidence_ids"
        and item["type"] == "too_long"
    )
    assert error["type"] == "too_long"
    assert error["ctx"]["max_length"] == 12  # type: ignore[index]


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (
            concept_payload(classification_ids=["tr_0001", "tr_0001"]),
            "generated note classification_evidence_ids contains duplicate "
            "evidence id: tr_0001",
        ),
        (
            {
                **concept_payload(),
                "summary": {
                    "text": "材料介绍了一个可复查的学习主题。",
                    "evidence_ids": ["tr_0001", "tr_0001"],
                },
            },
            "generated note factual statement 1 contains duplicate evidence id: "
            "tr_0001",
        ),
        (
            resource_payload(locator_ids=["tr_0001", "tr_0001"]),
            "generated note resource 1 locator contains duplicate evidence id: tr_0001",
        ),
    ],
)
def test_v3_rejects_duplicate_evidence_within_one_list(
    payload: dict[str, object], expected_error: str
) -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert expected_error in validate_generated_note(note, content_pack())


def test_v3_allows_evidence_reuse_across_lists() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(json.dumps(concept_payload(), ensure_ascii=False))

    assert not any(
        "duplicate evidence id" in error
        for error in validate_generated_note(note, content_pack())
    )


def test_v2_allows_same_list_duplicates_for_legacy_compatibility() -> None:
    from learnnest.note_validation import validate_generated_note

    payload = note_payload()
    payload["summary"]["evidence_ids"] = [  # type: ignore[index]
        "tr_0001",
        "tr_0001",
    ]

    assert (
        validate_generated_note(GeneratedNote.model_validate(payload), content_pack())
        == []
    )


def test_v3_factual_ocr_requires_parent_frame() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    payload = concept_payload()
    payload["summary"]["evidence_ids"] = ["ocr_0001"]  # type: ignore[index]
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert validate_generated_note(note, content_pack()) == [
        "generated note factual statement 1 cites OCR evidence ocr_0001 "
        "without parent frame fr_0001"
    ]


def test_v3_factual_ocr_accepts_parent_frame() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    payload = concept_payload()
    payload["summary"]["evidence_ids"] = [  # type: ignore[index]
        "ocr_0001",
        "fr_0001",
    ]
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert validate_generated_note(note, content_pack()) == []


def test_v3_classification_ocr_does_not_require_parent_frame() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(
        json.dumps(
            concept_payload(classification_ids=["ocr_0001"]),
            ensure_ascii=False,
        )
    )

    assert (
        validate_generated_note(
            note,
            content_pack(),
            require_classification_evidence=True,
        )
        == []
    )


def test_unknown_ocr_only_reports_unknown_evidence() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    payload = concept_payload()
    payload["summary"]["evidence_ids"] = ["ocr_9999"]  # type: ignore[index]
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert validate_generated_note(note, content_pack()) == [
        "generated note references unknown evidence id: ocr_9999"
    ]


def test_validate_generated_note_accepts_resolvable_sources() -> None:
    from learnnest.note_validation import validate_generated_note

    assert validate_generated_note(generated_note(), content_pack()) == []


def test_validate_generated_note_reports_task_and_fingerprint_mismatch_in_order() -> (
    None
):
    from learnnest.note_validation import validate_generated_note

    errors = validate_generated_note(
        generated_note(task_id="wrong-task", source_fingerprint="wrong-fingerprint"),
        content_pack(),
    )

    assert errors == [
        "generated note task_id does not match content pack",
        "generated note source_fingerprint does not match content pack",
    ]


def test_validate_generated_note_rejects_dangling_evidence() -> None:
    from learnnest.note_validation import validate_generated_note

    payload = note_payload()
    payload["summary"] = {"text": "无来源总结", "evidence_ids": ["tr_9999"]}
    errors = validate_generated_note(
        GeneratedNote.model_validate(payload), content_pack()
    )

    assert errors == ["generated note references unknown evidence id: tr_9999"]


def test_validate_generated_note_rejects_ocr_only_operation_step() -> None:
    from learnnest.note_validation import validate_generated_note

    payload = note_payload()
    payload["steps"] = [
        {"order": 1, "text": "保存模型配置。", "evidence_ids": ["ocr_0001"]}
    ]
    errors = validate_generated_note(
        GeneratedNote.model_validate(payload), content_pack()
    )

    assert errors == ["generated note step 1 requires transcript or frame evidence"]


def test_validate_generated_note_rejects_ai_supplement_as_factual_evidence() -> None:
    from learnnest.note_validation import validate_generated_note

    payload = note_payload()
    payload["summary"] = {
        "text": "不同版本入口名称可能变化。",
        "evidence_ids": ["ai_0001"],
    }
    errors = validate_generated_note(
        GeneratedNote.model_validate(payload), content_pack()
    )

    assert errors == [
        "generated note factual statements cannot cite AI supplement evidence: ai_0001"
    ]


def test_auto_classification_requires_real_evidence() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(
        json.dumps(concept_payload(classification_ids=[]), ensure_ascii=False)
    )
    errors = validate_generated_note(
        note, content_pack(), require_classification_evidence=True
    )

    assert errors == ["automatic note classification requires evidence"]


def test_manual_type_mismatch_is_rejected() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(json.dumps(resource_payload(), ensure_ascii=False))
    errors = validate_generated_note(
        note, content_pack(), requested_note_type="concept_explanation"
    )

    assert errors == ["generated note type does not match requested note type"]


def test_resource_locator_must_be_verbatim_in_transcript_or_ocr() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(
        json.dumps(resource_payload(url="https://invented.invalid"), ensure_ascii=False)
    )
    errors = validate_generated_note(note, content_pack())

    assert (
        "resource locator is not present in cited transcript or OCR evidence" in errors
    )


def test_resource_locator_verbatim_match_is_case_sensitive() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(
        json.dumps(
            resource_payload(url="https://EXAMPLE.com/resource"), ensure_ascii=False
        )
    )

    assert (
        "resource locator is not present in cited transcript or OCR evidence"
        in validate_generated_note(note, content_pack())
    )


@pytest.mark.parametrize(
    ("url", "locator_ids"),
    [
        ("https://example.com/resource", ["tr_0001"]),
        ("https://example.com/ocr-resource", ["ocr_0001"]),
    ],
)
def test_resource_locator_accepts_exact_transcript_or_ocr_url(
    url: str, locator_ids: list[str]
) -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(
        json.dumps(
            resource_payload(url=url, locator_ids=locator_ids), ensure_ascii=False
        )
    )

    assert validate_generated_note(note, content_pack()) == []


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/resource",
        "https://example.com/bad url",
        "https://example.com/<resource>",
    ],
)
def test_resource_locator_rejects_non_http_or_unsafe_url(url: str) -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(
        json.dumps(resource_payload(url=url), ensure_ascii=False)
    )

    assert "resource locator must be a safe http(s) URL" in validate_generated_note(
        note, content_pack()
    )


def test_resource_locator_requires_transcript_or_ocr_evidence() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(
        json.dumps(resource_payload(locator_ids=["fr_0001"]), ensure_ascii=False)
    )

    assert (
        "resource locator requires transcript or OCR evidence"
        in validate_generated_note(note, content_pack())
    )


def test_resource_locator_evidence_is_included_in_identity_validation() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    note = parse_generated_note(
        json.dumps(resource_payload(locator_ids=["tr_9999"]), ensure_ascii=False)
    )

    assert "generated note references unknown evidence id: tr_9999" in (
        validate_generated_note(note, content_pack())
    )


def test_url_token_is_rejected_outside_resource_locator() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    payload = concept_payload()
    payload["summary"] = {
        "text": "访问 https://example.com/resource 查看材料。",
        "evidence_ids": ["tr_0001"],
    }
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert "generated note factual text must not contain a URL" in (
        validate_generated_note(note, content_pack())
    )


@pytest.mark.parametrize(("payload", "path"), non_locator_url_cases())
def test_every_v3_non_locator_text_field_rejects_url_token(
    payload: dict[str, object], path: tuple[object, ...]
) -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    set_nested(payload, path, "查看 https://example.com/resource")
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert "generated note factual text must not contain a URL" in (
        validate_generated_note(note, content_pack())
    )


def test_cognitive_only_text_is_not_a_practical_action() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    payload = practical_payload()
    payload["steps"][0]["action"]["text"] = "理解 Agent 的核心组成。"  # type: ignore[index]
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert "practical step 1 is not an executable action" in validate_generated_note(
        note, content_pack()
    )


def test_cognitive_text_with_executable_marker_is_a_practical_action() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    payload = practical_payload()
    payload["steps"][0]["action"]["text"] = "理解参数后点击保存。"  # type: ignore[index]
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert validate_generated_note(note, content_pack()) == []


@pytest.mark.parametrize(
    "path",
    [
        ("classification_evidence_ids",),
        ("summary", "evidence_ids"),
        ("concepts", 0, "explanation", "evidence_ids"),
        ("background", "evidence_ids"),
        ("relationships", 0, "evidence_ids"),
        ("misconceptions", 0, "evidence_ids"),
        ("review", "evidence_ids"),
        ("resources", 0, "name", "evidence_ids"),
        ("resources", 0, "value", "evidence_ids"),
        ("resources", 0, "suitable_for", "evidence_ids"),
        ("resources", 0, "access_or_usage", "evidence_ids"),
        ("resources", 0, "limitations", 0, "evidence_ids"),
        ("reminders", 0, "evidence_ids"),
        ("goal", "evidence_ids"),
        ("prerequisites", 0, "evidence_ids"),
        ("steps", 0, "action", "evidence_ids"),
        ("steps", 0, "expected_result", "evidence_ids"),
        ("troubleshooting", 0, "symptom", "evidence_ids"),
        ("troubleshooting", 0, "resolution", "evidence_ids"),
        ("completion_checks", 0, "evidence_ids"),
        ("cautions", 0, "evidence_ids"),
    ],
)
def test_every_v3_factual_field_rejects_unknown_evidence(
    path: tuple[object, ...],
) -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    payload = complete_payload_for_path(path)
    set_nested(payload, path, ["tr_9999"])
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert "generated note references unknown evidence id: tr_9999" in (
        validate_generated_note(note, content_pack())
    )


def test_classification_cannot_cite_ai_supplement() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    payload = concept_payload(classification_ids=["ai_0001"])
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert (
        "generated note factual statements cannot cite AI supplement evidence: ai_0001"
        in validate_generated_note(note, content_pack())
    )


def test_practical_action_rejects_ocr_only_evidence() -> None:
    from learnnest.note_validation import parse_generated_note, validate_generated_note

    payload = practical_payload()
    payload["steps"][0]["action"]["evidence_ids"] = ["ocr_0001"]  # type: ignore[index]
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert "generated note step 1 requires transcript or frame evidence" in (
        validate_generated_note(note, content_pack())
    )
