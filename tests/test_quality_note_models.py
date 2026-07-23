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
                            "markdown": "小文件字符数<5000，大文件字符数>=5000。",
                            "evidence_unit_ids": ["eu_0001"],
                        }
                    ],
                }
            ],
        }
    )

    assert (
        draft.sections[0].items[0].markdown == "小文件字符数<5000，大文件字符数>=5000。"
    )


def test_reader_draft_rejects_actual_html_markup() -> None:
    with pytest.raises(ValidationError, match="HTML"):
        ReaderDraft.model_validate(
            {
                "title": "读取策略",
                "sections": [
                    {
                        "slot": "core",
                        "items": [
                            {
                                "markdown": "使用<strong>完整读取</strong>。",
                                "evidence_unit_ids": ["eu_0001"],
                            }
                        ],
                    }
                ],
            }
        )


def test_quality_writer_preserves_markdown_line_breaks() -> None:
    raw = json.dumps(
        {
            "title": "读取策略",
            "sections": [
                {
                    "slot": "core",
                    "items": [
                        {
                            "markdown": "- 第一行\n- 第二行",
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

    assert draft.sections[0].items[0].markdown == "- 第一行\n- 第二行"
    assert actions == []


def test_quality_writer_demotes_item_headings_without_rewriting_body() -> None:
    raw = json.dumps(
        {
            "title": "读取策略",
            "sections": [
                {
                    "slot": "core",
                    "items": [
                        {
                            "markdown": "### 1. 读取工具\n\n保留正文。",
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

    assert draft.sections[0].items[0].markdown == "**1. 读取工具**\n\n保留正文。"
    assert actions == ["demoted ReaderDraft headings at sections[0].items[0].markdown"]


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
                                "markdown": "事实",
                                "evidence_unit_ids": ["eu_0001"],
                                "ai_supplement": True,
                            }
                        ],
                    }
                ],
            }
        )


def test_reader_draft_limits_distinct_visuals_across_the_note() -> None:
    with pytest.raises(ValidationError, match="at most three visuals"):
        ReaderDraft.model_validate(
            {
                "title": "标题",
                "sections": [
                    {
                        "slot": "core",
                        "items": [
                            {
                                "markdown": f"内容 {index}",
                                "evidence_unit_ids": [f"eu_{index:04d}"],
                                "visual_unit_id": f"eu_{index:04d}",
                            }
                            for index in range(1, 5)
                        ],
                    }
                ],
            }
        )


def test_writer_normalizes_visual_anchor_aliases_and_enforces_visual_budget() -> None:
    raw = json.dumps(
        {
            "title": "标题",
            "sections": [
                {
                    "slot": "core",
                    "items": [
                        {
                            "markdown": f"内容 {index}",
                            "evidence_unit_ids": [f"eu_{index:04d}"],
                            "visual_unit_id": f"fr_{index:04d}",
                        }
                        for index in range(1, 5)
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )

    normalized, actions = _normalize_reader_draft_json(
        raw,
        visual_unit_aliases={
            f"fr_{index:04d}": f"eu_{index:04d}" for index in range(1, 5)
        },
    )
    draft = ReaderDraft.model_validate_json(normalized)

    assert [item.visual_unit_id for item in draft.sections[0].items] == [
        "eu_0001",
        "eu_0002",
        "eu_0003",
        None,
    ]
    assert len(actions) == 5
    assert actions[-1].startswith("removed over-budget visual selection eu_0004")


def test_quality_note_envelope_requires_program_identity_and_order() -> None:
    with pytest.raises(ValidationError, match="template_sha256"):
        QualityNoteEnvelope.model_validate(
            {
                "schema_version": "2.0",
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
