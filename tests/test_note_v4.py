"""GeneratedNote 4.0 model and deterministic validation contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from learnnest.models import ContentPack, Evidence
from learnnest.note_templates import builtin_template, template_snapshot_sha256
from learnnest.note_validation import (
    parse_generated_note,
    validate_generated_note,
    validate_v4_source_contract,
    validate_v4_template_presentation,
)


def _pack() -> ContentPack:
    return ContentPack(
        task_id="20260719-a1b2c3d4",
        source_fingerprint="a1b2c3d4",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="材料说明了核心概念。",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=1_000,
                artifact_path="frames/selected/fr_0001.png",
                related_evidence_ids=["ocr_0001"],
            ),
            Evidence(
                id="ocr_0001",
                kind="ocr",
                text="概念结构图",
                artifact_path="ocr.json",
                frame_id="fr_0001",
            ),
        ],
    )


def _payload() -> tuple[dict[str, object], object]:
    template = builtin_template("concept-explanation")
    return (
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
        template,
    )


def test_v4_note_is_bound_to_the_template_snapshot_and_parent_frame() -> None:
    payload, template = _payload()

    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert note.schema_version == "4.0"
    assert validate_generated_note(note, _pack(), template=template) == []


def test_v4_note_rejects_a_missing_required_template_block() -> None:
    payload, template = _payload()
    payload["blocks"] = payload["blocks"][:1]  # type: ignore[index]
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    errors = validate_generated_note(note, _pack(), template=template)

    assert "generated note is missing required template block: concepts" in errors


def test_v4_template_presentation_is_reported_separately_from_source_validity() -> None:
    payload, template = _payload()
    item = payload["blocks"][1]["items"][0]  # type: ignore[index]
    del item["title"]  # type: ignore[index]
    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))

    assert validate_v4_source_contract(note, _pack(), template=template) == []
    assert validate_v4_template_presentation(note, template) == [
        "generated note template block requires evidence-backed item titles: concepts"
    ]
    assert validate_generated_note(note, _pack(), template=template) == [
        "generated note template block requires evidence-backed item titles: concepts"
    ]


def test_v4_note_rejects_ocr_evidence_without_its_parent_frame() -> None:
    payload, template = _payload()
    concepts = payload["blocks"][1]  # type: ignore[index]
    item = concepts["items"][0]  # type: ignore[index]
    item["content"]["evidence_ids"] = ["ocr_0001"]  # type: ignore[index]

    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))
    errors = validate_generated_note(note, _pack(), template=template)

    assert any("ocr_0001 without parent frame fr_0001" in error for error in errors)


def test_v4_note_rejects_a_locator_not_copied_verbatim_from_evidence() -> None:
    payload, template = _payload()
    core = payload["blocks"][0]  # type: ignore[index]
    item = core["items"][0]  # type: ignore[index]
    item["locator"] = {  # type: ignore[index]
        "url": "https://example.test/truncated",
        "evidence_ids": ["tr_0001"],
    }

    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))
    errors = validate_generated_note(note, _pack(), template=template)

    assert (
        "resource locator is not present in cited transcript or OCR evidence" in errors
    )


def test_v4_note_rejects_a_template_sha_mismatch() -> None:
    payload, template = _payload()
    payload["template_sha256"] = "0" * 64

    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))
    errors = validate_generated_note(note, _pack(), template=template)

    assert "generated note template_sha256 does not match template snapshot" in errors


def test_v4_note_requires_an_evidence_backed_document_title() -> None:
    payload, _ = _payload()
    payload["title"] = "无依据标题"

    with pytest.raises(ValidationError, match="title"):
        parse_generated_note(json.dumps(payload, ensure_ascii=False))


def test_v4_note_rejects_raw_html_evidence_comments_from_model_text() -> None:
    payload, template = _payload()
    core = payload["blocks"][0]  # type: ignore[index]
    item = core["items"][0]  # type: ignore[index]
    item["content"]["text"] = "材料<!-- evidence: tr_0001 -->"  # type: ignore[index]

    note = parse_generated_note(json.dumps(payload, ensure_ascii=False))
    errors = validate_generated_note(note, _pack(), template=template)

    assert "GeneratedNote 4.0 model text must not contain raw HTML markup" in errors
