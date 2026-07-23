from __future__ import annotations

import pytest
from pydantic import ValidationError

from learnnest.note_models import ReaderDraft
from learnnest.reader_templates import (
    builtin_reader_template,
    reader_template_snapshot_sha256,
)


def test_reader_template_keeps_common_skeleton_and_type_module_order() -> None:
    template = builtin_reader_template("concept")

    assert template.schema_version == "1.0"
    assert [section.slot for section in template.sections[:7]] == [
        "summary",
        "why_learn",
        "narrative",
        "core",
        "practice",
        "cautions",
        "review",
    ]
    assert template.sections[7].slot == "concept_model"
    assert len({section.section_id for section in template.sections}) == len(
        template.sections
    )
    assert reader_template_snapshot_sha256(template) == reader_template_snapshot_sha256(
        template.model_copy(deep=True)
    )


def test_reader_draft_is_provider_only_and_rejects_program_owned_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ReaderDraft.model_validate(
            {
                "title": "设置入口",
                "sections": [],
                "task_id": "must-be-program-owned",
            }
        )


def test_reader_draft_allows_empty_optional_sections_but_marks_ai_supplement() -> None:
    draft = ReaderDraft.model_validate(
        {
            "title": "设置入口",
            "sections": [
                {
                    "slot": "summary",
                    "items": [
                        {
                            "markdown": "先打开设置页面。",
                            "evidence_unit_ids": ["eu_0001"],
                        }
                    ],
                },
                {"slot": "review", "items": []},
            ],
            "ai_supplements": [
                {"text": "不同版本的入口名称可能不同。"},
            ],
        }
    )

    assert draft.sections[1].items == []
    assert draft.ai_supplements[0].text.startswith("不同版本")
