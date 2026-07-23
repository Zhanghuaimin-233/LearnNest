from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from learnnest.note_models import QualityNoteEnvelope, ReaderDraft
from learnnest.note_providers import (
    OpenAICompatibleChatConfig,
    OpenAICompatibleQualityNoteProvider,
    NoteProviderError,
    build_quality_organizer_system_prompt,
)
from learnnest.reader_templates import (
    builtin_reader_template,
    reader_template_snapshot_json,
)


def test_quality_organizer_prompt_treats_evidence_ids_as_opaque() -> None:
    prompt = build_quality_organizer_system_prompt()

    assert "opaque" in prompt
    assert "ocr_0022" in prompt
    assert "ocr_022" in prompt


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)


class _Response:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def create(self, **request: object) -> _Response:
        self.calls.append(request)
        return _Response(self.responses.pop(0))


class _Client:
    def __init__(self, completions: _Completions) -> None:
        self.chat = type("Chat", (), {"completions": completions})()


def _provider(
    responses: list[str], calls: _Completions
) -> OpenAICompatibleQualityNoteProvider:
    return OpenAICompatibleQualityNoteProvider(
        OpenAICompatibleChatConfig(
            provider_name="fake-quality",
            model="fake-model",
            base_url="https://example.test/v1",
            api_key=SecretStr("test-key"),
        ),
        client_factory=lambda **_kwargs: _Client(calls),
    )


def test_quality_provider_uses_one_bounded_call_per_explicit_role() -> None:
    organizer_response = {
        "schema_version": "2.0",
        "units": [
            {
                "raw_evidence_ids": ["tr_0001"],
                "unit_type": "concept",
                "topic_labels": ["概念"],
                "outline": "组织",
                "reader_relevance": "core",
                "citation_anchor_ids": ["tr_0001"],
                "visual_role": "none",
            }
        ],
    }
    draft = ReaderDraft(title="标题", sections=[]).model_dump(mode="json")
    review = {"schema_version": "1.0", "status": "passed", "issues": []}
    calls = _Completions(
        [
            json.dumps(organizer_response, ensure_ascii=False),
            json.dumps(draft, ensure_ascii=False),
            json.dumps(review, ensure_ascii=False),
        ]
    )
    provider = _provider([], calls)
    shard_json = json.dumps(
        {
            "schema_version": "2.0",
            "shard": {
                "shard_id": "shard_0001",
                "atom_ids": ["tr_0001"],
                "start_ms": 0,
                "end_ms": 1_000,
            },
            "atoms": [
                {
                    "evidence_id": "tr_0001",
                    "kind": "transcript",
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "text": "概念",
                    "artifact_path": "content_pack.json",
                    "related_evidence_ids": [],
                }
            ],
        },
        ensure_ascii=False,
    )
    organization_json = json.dumps(
        {
            "schema_version": "2.0",
            "task_id": "task-1",
            "source_fingerprint": "source-1",
            "content_pack_sha256": "a" * 64,
            "units": [
                {
                    "unit_id": "eu_0001",
                    "shard_id": "shard_0001",
                    "unit_type": "concept",
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "topic_labels": ["概念"],
                    "outline": "组织",
                    "raw_evidence_ids": ["tr_0001"],
                    "evidence_ids": ["tr_0001"],
                    "transcript_ids": ["tr_0001"],
                    "frame_ids": [],
                    "ocr_ids": [],
                    "evidence": [
                        {
                            "evidence_id": "tr_0001",
                            "kind": "transcript",
                            "start_ms": 0,
                            "end_ms": 1_000,
                            "text": "概念",
                            "artifact_path": "content_pack.json",
                            "related_evidence_ids": [],
                        }
                    ],
                    "reader_relevance": "core",
                    "citation_anchor_ids": ["tr_0001"],
                    "visual_role": "none",
                }
            ],
            "shard_ids": ["shard_0001"],
            "normalizations": [],
        },
        ensure_ascii=False,
    )
    writer_organization_json = json.dumps(
        {
            "schema_version": "2.0",
            "units": [
                {
                    "unit_id": "eu_0001",
                    "unit_type": "concept",
                    "reader_relevance": "core",
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "topic_labels": ["概念"],
                    "outline": "组织",
                    "citation_anchor_ids": ["tr_0001"],
                    "citation_anchors": [
                        {
                            "evidence_id": "tr_0001",
                            "kind": "transcript",
                            "start_ms": 0,
                            "end_ms": 1_000,
                            "text": "概念",
                            "artifact_path": "content_pack.json",
                            "related_evidence_ids": [],
                        }
                    ],
                    "visual_role": "none",
                }
            ],
        },
        ensure_ascii=False,
    )
    note_json = QualityNoteEnvelope(
        schema_version="2.0",
        task_id="task-1",
        source_fingerprint="source-1",
        content_pack_sha256="a" * 64,
        template_id="quality-first-concept",
        template_sha256="b" * 64,
        title="标题",
        sections=[
            {
                "order": index,
                "section_id": f"section-{index}",
                "slot": "summary",
                "heading": f"章节 {index}",
                "purpose": "目的",
                "required": False,
                "items": [],
            }
            for index in range(1, 8)
        ],
        ai_supplements=[],
    ).model_dump_json()

    assert provider.organize(shard_json).startswith("{")
    assert provider.write(
        writer_organization_json,
        reader_template_snapshot_json(builtin_reader_template("concept")),
    ).startswith("{")
    assert provider.review(note_json, organization_json).startswith("{")
    assert len(calls.calls) == 3
    assert all(call["model"] == "fake-model" for call in calls.calls)
    assert all(call["stream"] is False for call in calls.calls)


def test_quality_provider_rejects_over_budget_input_before_transport_call() -> None:
    calls = _Completions([])
    provider = OpenAICompatibleQualityNoteProvider(
        OpenAICompatibleChatConfig(
            provider_name="fake-quality",
            model="fake-model",
            base_url="https://example.test/v1",
            api_key=SecretStr("test-key"),
            safe_input_tokens=1_024,
        ),
        client_factory=lambda **_kwargs: _Client(calls),
    )
    atom_text = "x" * 3_000
    shard_json = json.dumps(
        {
            "schema_version": "2.0",
            "shard": {
                "shard_id": "shard_0001",
                "atom_ids": ["tr_0001"],
                "start_ms": 0,
                "end_ms": 1_000,
            },
            "atoms": [
                {
                    "evidence_id": "tr_0001",
                    "kind": "transcript",
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "text": atom_text,
                    "artifact_path": "content_pack.json",
                    "related_evidence_ids": [],
                }
            ],
        },
        ensure_ascii=False,
    )

    with pytest.raises(NoteProviderError, match="before fake-quality call"):
        provider.organize(shard_json)

    assert calls.calls == []
