"""Constrained, versioned templates for GeneratedNote 4.0."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TEMPLATE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SECTION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESERVED_FRONTMATTER_PREFIXES = (
    "citation",
    "content_pack",
    "evidence",
    "learnpipe",
    "learnnest",
    "privacy",
    "security",
    "sha",
    "source",
    "task",
    "template_sha",
)

SemanticBlockKind = Literal[
    "core_facts",
    "concept_cards",
    "steps",
    "cautions",
    "review_questions",
    "action_checklist",
]
TemplateLength = Literal["concise", "standard", "detailed"]
TemplateFocus = Literal["general", "review", "practical"]


def _plain_template_text(value: str, *, field_name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be blank")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field_name} must be a single line")
    if "[[" in value or "]]" in value:
        raise ValueError(f"{field_name} must not contain Obsidian markup")
    if "<" in value or ">" in value:
        raise ValueError(f"{field_name} must not contain raw HTML markup")
    return value


class _TemplateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TemplatePreferences(_TemplateModel):
    """Non-factual presentation preferences exposed to the generation prompt."""

    length: TemplateLength = "standard"
    tone: str = "清晰、克制、面向学习"
    focus: TemplateFocus = "general"

    @model_validator(mode="after")
    def text_is_safe(self) -> TemplatePreferences:
        self.tone = _plain_template_text(self.tone, field_name="template tone")
        return self


class TemplateSection(_TemplateModel):
    """One renderer-owned section and the semantic block it consumes."""

    section_id: str
    heading: str
    semantic_block: SemanticBlockKind
    required: bool = True
    min_items: int = Field(default=1, ge=0, le=50)
    max_items: int = Field(default=12, ge=1, le=50)

    @model_validator(mode="after")
    def section_is_coherent(self) -> TemplateSection:
        if not _SECTION_ID_PATTERN.fullmatch(self.section_id):
            raise ValueError("section_id is invalid")
        self.heading = _plain_template_text(self.heading, field_name="section heading")
        if self.min_items > self.max_items:
            raise ValueError("section min_items cannot exceed max_items")
        if self.required and self.min_items == 0:
            raise ValueError("required section must require at least one item")
        return self


class NoteTemplate(_TemplateModel):
    """The entire restricted V4 template manifest."""

    schema_version: Literal["1.0"]
    template_id: str
    display_name: str
    frontmatter: dict[str, str] = Field(default_factory=dict)
    preferences: TemplatePreferences = Field(default_factory=TemplatePreferences)
    sections: list[TemplateSection] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def manifest_is_safe(self) -> NoteTemplate:
        if not _TEMPLATE_ID_PATTERN.fullmatch(self.template_id):
            raise ValueError("template_id is invalid")
        self.display_name = _plain_template_text(
            self.display_name, field_name="template display_name"
        )
        section_ids = [section.section_id for section in self.sections]
        if len(set(section_ids)) != len(section_ids):
            raise ValueError("template section_id values must be unique")
        block_kinds = [section.semantic_block for section in self.sections]
        if len(set(block_kinds)) != len(block_kinds):
            raise ValueError("template semantic_block values must be unique")
        for key, value in self.frontmatter.items():
            normalized_key = key.strip().lower().replace("-", "_")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", normalized_key):
                raise ValueError("frontmatter key is invalid")
            if normalized_key.startswith(_RESERVED_FRONTMATTER_PREFIXES):
                raise ValueError("frontmatter key is reserved")
            _plain_template_text(value, field_name="frontmatter value")
        return self


def parse_note_template(payload: Mapping[str, object] | str | bytes) -> NoteTemplate:
    """Parse only one strict template manifest, never arbitrary prompt text."""
    if isinstance(payload, Mapping):
        return NoteTemplate.model_validate(dict(payload))
    return NoteTemplate.model_validate_json(payload)


def template_snapshot_json(template: NoteTemplate) -> str:
    """Return the canonical snapshot persisted with every V4 bundle."""
    return json.dumps(
        template.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def template_snapshot_sha256(template: NoteTemplate) -> str:
    """Hash the canonical snapshot rather than its mutable source file."""
    return hashlib.sha256(template_snapshot_json(template).encode("utf-8")).hexdigest()


def load_template_file(path: Path) -> NoteTemplate:
    """Read and validate one user-provided JSON template."""
    try:
        return parse_note_template(path.read_bytes())
    except (OSError, UnicodeError) as error:
        raise ValueError(
            f"template could not be read: {type(error).__name__}"
        ) from error


def load_template_snapshot(path: Path) -> NoteTemplate:
    """Load a persisted canonical template snapshot for reproducible rerendering."""
    return load_template_file(path)


_BUILTIN_TEMPLATE_PAYLOADS: dict[str, dict[str, object]] = {
    "concept-explanation": {
        "schema_version": "1.0",
        "template_id": "concept-explanation",
        "display_name": "概念讲解",
        "preferences": {
            "length": "standard",
            "tone": "清晰、循序渐进",
            "focus": "review",
        },
        "sections": [
            {
                "section_id": "core",
                "heading": "核心事实",
                "semantic_block": "core_facts",
                "required": True,
                "min_items": 1,
                "max_items": 6,
            },
            {
                "section_id": "concepts",
                "heading": "概念卡片",
                "semantic_block": "concept_cards",
                "required": True,
                "min_items": 1,
                "max_items": 8,
            },
            {
                "section_id": "review",
                "heading": "复习问题",
                "semantic_block": "review_questions",
                "required": False,
                "min_items": 0,
                "max_items": 5,
            },
        ],
    },
    "resource-share": {
        "schema_version": "1.0",
        "template_id": "resource-share",
        "display_name": "资源分享",
        "preferences": {
            "length": "standard",
            "tone": "直接、可执行",
            "focus": "general",
        },
        "sections": [
            {
                "section_id": "resources",
                "heading": "资源与价值",
                "semantic_block": "core_facts",
                "required": True,
                "min_items": 1,
                "max_items": 8,
            },
            {
                "section_id": "cautions",
                "heading": "注意事项",
                "semantic_block": "cautions",
                "required": False,
                "min_items": 0,
                "max_items": 6,
            },
            {
                "section_id": "actions",
                "heading": "行动清单",
                "semantic_block": "action_checklist",
                "required": False,
                "min_items": 0,
                "max_items": 6,
            },
        ],
    },
    "practical-tutorial": {
        "schema_version": "1.0",
        "template_id": "practical-tutorial",
        "display_name": "实操教程",
        "preferences": {
            "length": "detailed",
            "tone": "明确、逐步",
            "focus": "practical",
        },
        "sections": [
            {
                "section_id": "goal",
                "heading": "目标与准备",
                "semantic_block": "core_facts",
                "required": True,
                "min_items": 1,
                "max_items": 4,
            },
            {
                "section_id": "steps",
                "heading": "操作步骤",
                "semantic_block": "steps",
                "required": True,
                "min_items": 1,
                "max_items": 12,
            },
            {
                "section_id": "checks",
                "heading": "完成检查",
                "semantic_block": "action_checklist",
                "required": False,
                "min_items": 0,
                "max_items": 6,
            },
            {
                "section_id": "cautions",
                "heading": "注意事项",
                "semantic_block": "cautions",
                "required": False,
                "min_items": 0,
                "max_items": 6,
            },
        ],
    },
}


def builtin_template(template_id: str) -> NoteTemplate:
    """Return a fresh official preset by its stable public identifier."""
    payload = _BUILTIN_TEMPLATE_PAYLOADS.get(template_id)
    if payload is None:
        choices = ", ".join(sorted(_BUILTIN_TEMPLATE_PAYLOADS))
        raise ValueError(
            f"unknown template preset: {template_id}; choose one of: {choices}"
        )
    return parse_note_template(payload)


def resolve_note_template(
    reference: str | Path | None, *, default_id: str
) -> NoteTemplate:
    """Resolve an official preset or a custom JSON path before a provider is called."""
    if reference is None:
        return builtin_template(default_id)
    text = str(reference)
    if text in _BUILTIN_TEMPLATE_PAYLOADS:
        return builtin_template(text)
    path = Path(text)
    if not path.is_file():
        raise ValueError(
            f"template must be an official preset or a readable JSON file: {text}"
        )
    return load_template_file(path)
