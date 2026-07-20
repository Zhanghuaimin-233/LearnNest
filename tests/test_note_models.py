from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from helpers.note_v3_fixtures import (
    concept_payload,
    practical_payload,
    v3_payloads,
)
from learnnest.note_models import (
    GENERATED_NOTE_ADAPTER,
    GENERATED_NOTE_V3_ADAPTER,
    AiSupplement,
    GeneratedNote,
    NoteStatement,
    NoteStep,
    generated_note_v3_json_schema,
)
from learnnest.note_types import normalize_note_type


def valid_note_payload() -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "title": "设置模型参数",
        "audience": {
            "text": "需要配置本地工具的学习者",
            "evidence_ids": ["tr_0001"],
        },
        "summary": {
            "text": "视频演示了打开设置并保存模型配置的流程。",
            "evidence_ids": ["tr_0001", "fr_0001"],
        },
        "key_points": [
            {
                "text": "配置保存前需要确认模型名称。",
                "evidence_ids": ["tr_0002"],
            }
        ],
        "steps": [
            {
                "order": 1,
                "text": "打开设置页面。",
                "evidence_ids": ["tr_0001", "fr_0001"],
            },
            {
                "order": 2,
                "text": "保存模型配置。",
                "evidence_ids": ["tr_0002"],
            },
        ],
        "cautions": [],
        "ai_supplements": [{"text": "不同版本界面的入口名称可能变化。"}],
    }


def test_generated_note_accepts_a_valid_2_0_payload() -> None:
    note = GeneratedNote.model_validate(valid_note_payload())

    assert note.schema_version == "2.0"
    assert note.audience.evidence_ids == ["tr_0001"]
    assert [step.order for step in note.steps] == [1, 2]
    assert note.ai_supplements[0].text == "不同版本界面的入口名称可能变化。"


@pytest.mark.parametrize("field", ["audience", "summary"])
def test_generated_note_rejects_empty_required_statement_evidence_ids(
    field: str,
) -> None:
    payload = valid_note_payload()
    payload[field]["evidence_ids"] = []  # type: ignore[index]

    with pytest.raises(ValidationError):
        GeneratedNote.model_validate(payload)


@pytest.mark.parametrize("field", ["key_points", "cautions"])
def test_generated_note_rejects_empty_list_statement_evidence_ids(field: str) -> None:
    payload = valid_note_payload()
    payload[field] = [{"text": "需要证据的内容。", "evidence_ids": []}]

    with pytest.raises(ValidationError):
        GeneratedNote.model_validate(payload)


def test_generated_note_rejects_empty_step_evidence_ids() -> None:
    payload = valid_note_payload()
    payload["steps"][0]["evidence_ids"] = []  # type: ignore[index]

    with pytest.raises(ValidationError):
        GeneratedNote.model_validate(payload)


def test_note_statement_rejects_a_blank_evidence_id() -> None:
    with pytest.raises(ValidationError):
        NoteStatement(text="结论", evidence_ids=["   "])


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (NoteStatement, {"text": "结论", "evidence_ids": ["tr_0001"]}),
        (
            NoteStep,
            {"order": 1, "text": "操作", "evidence_ids": ["tr_0001"]},
        ),
        (AiSupplement, {"text": "补充"}),
        (GeneratedNote, valid_note_payload()),
    ],
)
def test_all_note_models_reject_extra_fields(
    model: type[NoteStatement | NoteStep | AiSupplement | GeneratedNote],
    kwargs: dict[str, object],
) -> None:
    invalid = deepcopy(kwargs)
    invalid["unexpected"] = "value"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate(invalid)


def test_generated_note_rejects_the_wrong_schema_version() -> None:
    payload = valid_note_payload()
    payload["schema_version"] = "1.0"

    with pytest.raises(ValidationError):
        GeneratedNote.model_validate(payload)


@pytest.mark.parametrize(
    "orders",
    [
        [0, 1],
        [2, 3],
        [1, 1],
        [1, 3],
    ],
)
def test_generated_note_rejects_step_orders_that_are_not_contiguous_from_one(
    orders: list[int],
) -> None:
    payload = valid_note_payload()
    for step, order in zip(payload["steps"], orders, strict=True):  # type: ignore[arg-type]
        step["order"] = order

    with pytest.raises(ValidationError, match="step order"):
        GeneratedNote.model_validate(payload)


@pytest.mark.parametrize(
    "text",
    [
        "第一行\n第二行",
        "第一行\r第二行",
        "参见 [[设置说明]]",
        "错误的结束标记 ]]",
        "嵌入 ![[frame.png]]",
    ],
)
@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (NoteStatement, {"evidence_ids": ["tr_0001"]}),
        (NoteStep, {"order": 1, "evidence_ids": ["tr_0001"]}),
        (AiSupplement, {}),
    ],
)
def test_note_content_rejects_multiline_or_obsidian_markup(
    model: type[NoteStatement | NoteStep | AiSupplement],
    kwargs: dict[str, object],
    text: str,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({**kwargs, "text": text})


@pytest.mark.parametrize("title", ["第一行\n第二行", "参见 [[设置说明]]", "错误 ]]"])
def test_generated_note_title_must_be_plain_single_paragraph_text(title: str) -> None:
    payload = valid_note_payload()
    payload["title"] = title

    with pytest.raises(ValidationError):
        GeneratedNote.model_validate(payload)


@pytest.mark.parametrize("field", ["task_id", "source_fingerprint", "title"])
def test_generated_note_rejects_empty_required_strings(field: str) -> None:
    payload = valid_note_payload()
    payload[field] = "   "

    with pytest.raises(ValidationError):
        GeneratedNote.model_validate(payload)


def test_generated_note_requires_at_least_one_key_point() -> None:
    payload = valid_note_payload()
    payload["key_points"] = []

    with pytest.raises(ValidationError):
        GeneratedNote.model_validate(payload)


def test_generated_note_v3_accepts_all_three_concrete_types() -> None:
    notes = [
        GENERATED_NOTE_V3_ADAPTER.validate_python(payload) for payload in v3_payloads()
    ]
    assert [note.note_type for note in notes] == [
        "concept_explanation",
        "resource_share",
        "practical_tutorial",
    ]


def test_any_generated_note_adapter_accepts_v2_and_v3() -> None:
    legacy_note = GENERATED_NOTE_ADAPTER.validate_python(valid_note_payload())
    v3_note = GENERATED_NOTE_ADAPTER.validate_python(concept_payload())

    assert isinstance(legacy_note, GeneratedNote)
    assert v3_note.schema_version == "3.0"


def test_generated_note_v3_json_schema_uses_note_type_discriminator() -> None:
    schema = generated_note_v3_json_schema()

    assert schema["discriminator"]["propertyName"] == "note_type"  # type: ignore[index]


def test_generated_note_v3_schema_limits_classification_evidence() -> None:
    schema = generated_note_v3_json_schema()
    definitions = schema["$defs"]  # type: ignore[index]

    for model_name in (
        "ConceptExplanationNote",
        "ResourceShareNote",
        "PracticalTutorialNote",
    ):
        field = definitions[model_name]["properties"][  # type: ignore[index]
            "classification_evidence_ids"
        ]
        assert field["maxItems"] == 12


def test_v3_discriminated_union_rejects_fields_from_another_type() -> None:
    payload = concept_payload()
    payload["steps"] = practical_payload()["steps"]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        GENERATED_NOTE_V3_ADAPTER.validate_python(payload)


def test_practical_step_orders_are_contiguous() -> None:
    payload = practical_payload()
    payload["steps"][0]["order"] = 2  # type: ignore[index]
    with pytest.raises(ValidationError, match="step order"):
        GENERATED_NOTE_V3_ADAPTER.validate_python(payload)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" concept ", "concept_explanation"),
        ("RESOURCE", "resource_share"),
        ("practical_tutorial", "practical_tutorial"),
    ],
)
def test_normalize_note_type_accepts_short_and_concrete_values(
    value: str,
    expected: str,
) -> None:
    assert normalize_note_type(value) == expected


@pytest.mark.parametrize("value", ["auto", "unknown"])
def test_normalize_note_type_rejects_non_concrete_values(value: str) -> None:
    with pytest.raises(ValueError, match="unknown note type"):
        normalize_note_type(value)
