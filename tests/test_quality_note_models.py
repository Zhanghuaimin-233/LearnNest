from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from learnnest.note_models import QualityNoteEnvelope, ReaderDraft
from learnnest.quality_note_generation import _normalize_reader_draft_json


def test_reader_draft_allows_comparison_operators_in_source_text() -> None:
    draft = ReaderDraft.model_validate(
        {
            "title": "读取策略",
            "sections": [
                {
                    "slot": "core",
                    "items": [
                        {
                            "text": "小文件字符数<5000，大文件字符数>=5000。",
                            "evidence_unit_ids": ["eu_0001"],
                        }
                    ],
                }
            ],
        }
    )

    assert draft.sections[0].items[0].text == "小文件字符数<5000，大文件字符数>=5000。"


def test_reader_draft_rejects_actual_html_markup() -> None:
    with pytest.raises(ValidationError, match="markup"):
        ReaderDraft.model_validate(
            {
                "title": "读取策略",
                "sections": [
                    {
                        "slot": "core",
                        "items": [
                            {
                                "text": "使用<strong>完整读取</strong>。",
                                "evidence_unit_ids": ["eu_0001"],
                            }
                        ],
                    }
                ],
            }
        )


def test_quality_writer_normalizes_line_breaks_before_reader_draft_validation() -> None:
    raw = json.dumps(
        {
            "title": "读取策略",
            "sections": [
                {
                    "slot": "core",
                    "items": [
                        {
                            "text": "第一行\n第二行",
                            "evidence_unit_ids": ["eu_0001"],
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )

    normalized, actions = _normalize_reader_draft_json(raw)
    draft = ReaderDraft.model_validate_json(normalized)

    assert draft.sections[0].items[0].text == "第一行 第二行"
    assert actions == ["flattened ReaderDraft line breaks at sections[0].items[0].text"]


def test_reader_draft_item_cannot_mark_source_ids_as_ai_supplement() -> None:
    with pytest.raises(ValidationError, match="ai_supplement"):
        ReaderDraft.model_validate(
            {
                "title": "标题",
                "sections": [
                    {
                        "slot": "summary",
                        "items": [
                            {
                                "text": "事实",
                                "evidence_unit_ids": ["eu_0001"],
                                "ai_supplement": True,
                            }
                        ],
                    }
                ],
            }
        )


def test_quality_note_envelope_requires_program_identity_and_order() -> None:
    with pytest.raises(ValidationError, match="template_sha256"):
        QualityNoteEnvelope.model_validate(
            {
                "schema_version": "1.0",
                "task_id": "task-1",
                "source_fingerprint": "source-1",
                "content_pack_sha256": "a" * 64,
                "template_id": "quality-first-concept",
                "template_sha256": "not-a-sha",
                "title": "标题",
                "sections": [],
                "ai_supplements": [],
            }
        )
