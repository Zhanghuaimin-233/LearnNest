"""Reader-oriented manifests for the quality-first note workflow."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReaderContentType = Literal["concept", "resource", "tutorial", "mixed"]
ReaderSlot = Literal[
    "summary",
    "why_learn",
    "narrative",
    "core",
    "practice",
    "cautions",
    "review",
    "concept_model",
    "concept_relationships",
    "concept_analogy",
    "concept_practice",
    "resource_audience",
    "resource_usage",
    "resource_limits",
    "resource_actions",
    "tutorial_goal",
    "tutorial_preparation",
    "tutorial_steps",
    "tutorial_result",
    "tutorial_checks",
    "tutorial_faq",
]

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _safe_text(value: str, field_name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be blank")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field_name} must be a single line")
    if "[[" in value or "]]" in value or "<" in value or ">" in value:
        raise ValueError(f"{field_name} must not contain markup")
    return value


class _ReaderTemplateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReaderTemplateSection(_ReaderTemplateModel):
    """A renderer-owned section mapped from a provider-facing slot."""

    section_id: str
    slot: ReaderSlot
    heading: str
    purpose: str
    required: bool = False

    @model_validator(mode="after")
    def section_is_safe(self) -> ReaderTemplateSection:
        if not _ID_PATTERN.fullmatch(self.section_id):
            raise ValueError("reader section_id is invalid")
        self.heading = _safe_text(self.heading, "reader section heading")
        self.purpose = _safe_text(self.purpose, "reader section purpose")
        return self


class ReaderTemplateManifest(_ReaderTemplateModel):
    """Versioned common skeleton plus one content-type module."""

    schema_version: Literal["1.0"]
    template_id: str
    content_type: ReaderContentType
    display_name: str
    sections: list[ReaderTemplateSection] = Field(min_length=7, max_length=24)

    @model_validator(mode="after")
    def manifest_is_coherent(self) -> ReaderTemplateManifest:
        if not _ID_PATTERN.fullmatch(self.template_id.replace("-", "_")):
            raise ValueError("reader template_id is invalid")
        self.display_name = _safe_text(
            self.display_name, "reader template display_name"
        )
        section_ids = [section.section_id for section in self.sections]
        slots = [section.slot for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("reader section_id values must be unique")
        if len(slots) != len(set(slots)):
            raise ValueError("reader slots must be unique")
        common = [
            "summary",
            "why_learn",
            "narrative",
            "core",
            "practice",
            "cautions",
            "review",
        ]
        if slots[: len(common)] != common:
            raise ValueError("reader template must begin with the common skeleton")
        return self


def reader_template_snapshot_json(template: ReaderTemplateManifest) -> str:
    return json.dumps(
        template.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def reader_template_snapshot_sha256(template: ReaderTemplateManifest) -> str:
    return hashlib.sha256(
        reader_template_snapshot_json(template).encode("utf-8")
    ).hexdigest()


def parse_reader_template(
    payload: Mapping[str, object] | str | bytes,
) -> ReaderTemplateManifest:
    if isinstance(payload, Mapping):
        return ReaderTemplateManifest.model_validate(dict(payload))
    return ReaderTemplateManifest.model_validate_json(payload)


def load_reader_template(path: Path) -> ReaderTemplateManifest:
    try:
        return parse_reader_template(path.read_bytes())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(
            f"reader template could not be read: {type(error).__name__}"
        ) from error


_COMMON = (
    ("summary", "一句话总结", "压缩这段内容的核心价值", True),
    ("why_learn", "这段内容解决什么问题", "说明为什么值得学习", True),
    ("narrative", "内容脉络", "按视频实际推导顺序保留主线", True),
    ("core", "核心内容", "解释主要概念、信息或方法", True),
    ("practice", "实践或应用", "把内容转成可执行的应用", False),
    ("cautions", "注意事项、不确定性与局限", "诚实呈现边界和不确定性", False),
    ("review", "快速复习", "帮助读者回忆和自测", False),
)

_MODULES: dict[str, tuple[tuple[str, str, str, bool], ...]] = {
    "concept": (
        ("concept_model", "核心模型", "说明概念的组成和工作方式", False),
        ("concept_relationships", "概念关系", "说明概念之间的关系或对比", False),
        ("concept_analogy", "类比与边界", "用清楚的类比帮助理解并说明边界", False),
        ("concept_practice", "实践启示", "说明读者可以如何使用这个概念", False),
    ),
    "resource": (
        ("resource_audience", "适合谁", "说明资源适用的人群或前置条件", False),
        ("resource_usage", "使用方式", "说明如何获取、使用或评估资源", False),
        ("resource_limits", "限制", "说明资源的限制和风险", False),
        ("resource_actions", "行动建议", "给出下一步行动", False),
    ),
    "tutorial": (
        ("tutorial_goal", "目标", "明确操作目标", False),
        ("tutorial_preparation", "准备", "列出必要准备", False),
        ("tutorial_steps", "步骤", "按实际操作顺序说明步骤", False),
        ("tutorial_result", "预期结果", "说明完成后的可观察结果", False),
        ("tutorial_checks", "完成检查", "提供完成前的检查项", False),
        ("tutorial_faq", "常见问题", "说明视频中出现的常见问题和处理", False),
    ),
}


def _builtin_payload(content_type: ReaderContentType) -> dict[str, object]:
    module = _MODULES.get(content_type, ())
    sections = [
        {
            "section_id": section_id,
            "slot": section_id,
            "heading": heading,
            "purpose": purpose,
            "required": required,
        }
        for section_id, heading, purpose, required in (*_COMMON, *module)
    ]
    return {
        "schema_version": "1.0",
        "template_id": f"quality-first-{content_type}",
        "content_type": content_type,
        "display_name": {
            "concept": "质量优先·概念讲解",
            "resource": "质量优先·资源分享",
            "tutorial": "质量优先·实操教程",
            "mixed": "质量优先·混合内容",
        }[content_type],
        "sections": sections,
    }


def builtin_reader_template(
    content_type: ReaderContentType | str,
) -> ReaderTemplateManifest:
    normalized = str(content_type).replace("quality-first-", "")
    if normalized not in {"concept", "resource", "tutorial", "mixed"}:
        raise ValueError(f"unknown quality-first reader template: {content_type}")
    return parse_reader_template(_builtin_payload(normalized))


def resolve_reader_template(
    reference: str | Path | None, *, default_type: ReaderContentType = "mixed"
) -> ReaderTemplateManifest:
    if reference is None:
        return builtin_reader_template(default_type)
    text = str(reference)
    if text in {"concept", "resource", "tutorial", "mixed"} or text.startswith(
        "quality-first-"
    ):
        return builtin_reader_template(text)
    path = Path(text)
    if not path.is_file():
        raise ValueError(
            "reader template must be a built-in type or readable JSON file"
        )
    return load_reader_template(path)
