"""The optional V4 model-assisted NoteAudit contract."""

from __future__ import annotations

import json

from learnnest.note_audit import (
    candidate_sha256,
    parse_note_audit,
    validate_note_audit,
)
from learnnest.note_templates import builtin_template, template_snapshot_sha256
from learnnest.note_validation import parse_generated_note


def _candidate():
    template = builtin_template("concept-explanation")
    payload = {
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
                            "evidence_ids": ["tr_0001"],
                        },
                        "content": {
                            "text": "核心概念可用于组织学习材料。",
                            "evidence_ids": ["tr_0001"],
                        },
                    }
                ],
            },
            {"block_id": "review", "semantic_block": "review_questions", "items": []},
        ],
        "ai_supplements": [],
    }
    return parse_generated_note(json.dumps(payload, ensure_ascii=False))


def test_note_audit_is_bound_to_every_factual_candidate_path() -> None:
    candidate = _candidate()
    payload = {
        "schema_version": "1.0",
        "task_id": candidate.task_id,
        "source_fingerprint": candidate.source_fingerprint,
        "candidate_sha256": candidate_sha256(candidate),
        "verdict": "passed",
        "statement_audits": [
            {
                "candidate_path": "/title",
                "assessment": "supported",
                "rationale": "文档标题由同一来源支撑。",
            },
            {
                "candidate_path": "/blocks/0/items/0/content",
                "assessment": "supported",
                "rationale": "引用与陈述一致。",
            },
            {
                "candidate_path": "/blocks/1/items/0/title",
                "assessment": "supported",
                "rationale": "标题由同一来源支撑。",
            },
            {
                "candidate_path": "/blocks/1/items/0/content",
                "assessment": "supported",
                "rationale": "解释没有增加额外结论。",
            },
        ],
        "issues": [],
    }

    audit = parse_note_audit(json.dumps(payload, ensure_ascii=False))

    assert validate_note_audit(audit, candidate) == []
