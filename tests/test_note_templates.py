"""Contracts for the constrained GeneratedNote 4.0 template DSL."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from learnnest.note_templates import (
    builtin_template,
    parse_note_template,
    template_snapshot_json,
    template_snapshot_sha256,
)


def test_official_template_has_a_stable_canonical_snapshot() -> None:
    template = builtin_template("concept-explanation")

    snapshot = template_snapshot_json(template)

    assert template.template_id == "concept-explanation"
    assert template_snapshot_sha256(template) == template_snapshot_sha256(template)
    assert '"schema_version":"1.0"' in snapshot
    assert '"semantic_block":"concept_cards"' in snapshot


def test_template_rejects_frontmatter_that_could_override_traceability() -> None:
    payload = {
        "schema_version": "1.0",
        "template_id": "unsafe-template",
        "display_name": "不安全模板",
        "frontmatter": {"source_sha256": "forged"},
        "sections": [
            {
                "section_id": "facts",
                "heading": "核心事实",
                "semantic_block": "core_facts",
                "required": True,
                "min_items": 1,
                "max_items": 3,
            }
        ],
    }

    with pytest.raises(ValidationError, match="frontmatter key is reserved"):
        parse_note_template(payload)


@pytest.mark.parametrize("key", ["learnnest_task_id", "learnpipe_task_id"])
def test_template_rejects_current_and_legacy_traceability_frontmatter(
    key: str,
) -> None:
    payload = builtin_template("concept-explanation").model_dump(mode="json")
    payload["frontmatter"] = {key: "forged"}

    with pytest.raises(ValidationError, match="frontmatter key is reserved"):
        parse_note_template(payload)


def test_template_rejects_raw_html_that_could_hide_renderer_annotations() -> None:
    payload = builtin_template("concept-explanation").model_dump(mode="json")
    payload["sections"][0]["heading"] = "<!-- evidence: tr_0001 -->"

    with pytest.raises(ValidationError, match="raw HTML"):
        parse_note_template(payload)
