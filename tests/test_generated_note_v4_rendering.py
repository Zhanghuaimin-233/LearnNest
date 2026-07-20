"""Renderer contracts for template-driven GeneratedNote 4.0 Markdown."""

from __future__ import annotations

import json

from learnnest.models import ContentPack, Evidence, TaskRecord
from learnnest.note_templates import builtin_template, template_snapshot_sha256
from learnnest.note_validation import (
    parse_generated_note,
    validate_v4_template_presentation,
)
from learnnest.rendering import render_generated_note


def test_v4_renderer_uses_template_headings_and_keeps_evidence_program_owned() -> None:
    template = builtin_template("concept-explanation")
    note = parse_generated_note(
        json.dumps(
            {
                "schema_version": "4.0",
                "task_id": "20260719-a1b2c3d4",
                "source_fingerprint": "a1b2c3d4",
                "template_id": template.template_id,
                "template_sha256": template_snapshot_sha256(template),
                "title": {"text": "概念学习笔记", "evidence_ids": ["tr_0001"]},
                "blocks": [
                    {
                        "block_id": "core",
                        "semantic_block": "core_facts",
                        "items": [
                            {
                                "content": {
                                    "text": "材料说明了核心概念。",
                                    "evidence_ids": ["tr_0001"],
                                }
                            }
                        ],
                    },
                    {
                        "block_id": "concepts",
                        "semantic_block": "concept_cards",
                        "items": [
                            {
                                "title": {
                                    "text": "核心概念",
                                    "evidence_ids": ["ocr_0001", "fr_0001"],
                                },
                                "content": {
                                    "text": "结构图展示了核心概念。",
                                    "evidence_ids": ["ocr_0001", "fr_0001"],
                                },
                            }
                        ],
                    },
                    {
                        "block_id": "review",
                        "semantic_block": "review_questions",
                        "items": [],
                    },
                ],
                "ai_supplements": [],
            },
            ensure_ascii=False,
        )
    )
    pack = ContentPack(
        task_id="20260719-a1b2c3d4",
        source_fingerprint="a1b2c3d4",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1,
                text="材料说明了核心概念。",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=1,
                artifact_path="frames/selected/fr_0001.png",
                related_evidence_ids=["ocr_0001"],
            ),
            Evidence(
                id="ocr_0001",
                kind="ocr",
                text="核心概念",
                artifact_path="ocr.json",
                frame_id="fr_0001",
            ),
        ],
    )
    task = TaskRecord(
        task_id=pack.task_id,
        source_path="C:/lesson.mp4",
        source_fingerprint=pack.source_fingerprint,
        title="lesson",
    )

    markdown = render_generated_note(
        task,
        pack,
        note,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
        template=template,
    )

    assert "learnnest_template_id: concept-explanation" in markdown
    assert "## 概念卡片" in markdown
    assert "<!-- evidence: ocr_0001, fr_0001 -->" in markdown
    assert "![[视频学习素材/lesson--a1b2c3d4/frames/selected/fr_0001.png]]" in markdown


def test_v4_renderer_keeps_semantically_matched_flagged_blocks_visible() -> None:
    """A default-active source-valid candidate must not silently lose its body."""
    template = builtin_template("concept-explanation")
    note = parse_generated_note(
        json.dumps(
            {
                "schema_version": "4.0",
                "task_id": "20260719-a1b2c3d4",
                "source_fingerprint": "a1b2c3d4",
                "template_id": template.template_id,
                "template_sha256": template_snapshot_sha256(template),
                "title": {"text": "概念学习笔记", "evidence_ids": ["tr_0001"]},
                "blocks": [
                    {
                        "block_id": "core_facts",
                        "semantic_block": "core_facts",
                        "items": [
                            {
                                "content": {
                                    "text": "材料说明了核心概念。",
                                    "evidence_ids": ["tr_0001"],
                                }
                            }
                        ],
                    },
                    {
                        "block_id": "concept_cards",
                        "semantic_block": "concept_cards",
                        "items": [
                            {
                                "title": {
                                    "text": "核心概念",
                                    "evidence_ids": ["tr_0001"],
                                },
                                "content": {
                                    "text": "结构图展示了核心概念。",
                                    "evidence_ids": ["tr_0001"],
                                },
                            }
                        ],
                    },
                    {
                        "block_id": "review_questions",
                        "semantic_block": "review_questions",
                        "items": [],
                    },
                ],
                "ai_supplements": [],
            },
            ensure_ascii=False,
        )
    )
    pack = ContentPack(
        task_id="20260719-a1b2c3d4",
        source_fingerprint="a1b2c3d4",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1,
                text="材料说明了核心概念。",
                artifact_path="transcript.json",
            )
        ],
    )
    task = TaskRecord(
        task_id=pack.task_id,
        source_path="C:/lesson.mp4",
        source_fingerprint=pack.source_fingerprint,
        title="lesson",
    )

    assert validate_v4_template_presentation(note, template)

    markdown = render_generated_note(
        task,
        pack,
        note,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
        template=template,
    )

    assert "## 核心事实" in markdown
    assert "材料说明了核心概念。" in markdown
    assert "## 概念卡片" in markdown
    assert "核心概念" in markdown
    assert "结构图展示了核心概念。" in markdown
