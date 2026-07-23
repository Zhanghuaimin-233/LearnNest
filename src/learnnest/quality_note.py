"""Program-owned envelope construction and Markdown rendering for quality notes."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import PurePosixPath

from learnnest.evidence_unit_models import EvidenceUnitOrganization
from learnnest.models import ContentPack, Evidence, TaskRecord
from learnnest.note_models import (
    QualityNoteEnvelope,
    QualityNoteItem,
    QualityNoteLocator,
    QualityNoteSection,
    ReaderDraft,
)
from learnnest.reader_templates import ReaderTemplateManifest, ReaderSlot

_URL_PATTERN = re.compile(r"https?://[^\s<>\]]+", re.IGNORECASE)


def build_quality_note(
    task: TaskRecord,
    content_pack: ContentPack,
    organization: EvidenceUnitOrganization,
    draft: ReaderDraft,
    *,
    template: ReaderTemplateManifest,
    content_pack_sha256: str,
) -> QualityNoteEnvelope:
    """Inject identity, template, order, and expanded source evidence."""
    if task.task_id != content_pack.task_id or task.task_id != organization.task_id:
        raise ValueError("quality note task_id does not match its source bundle")
    if (
        task.source_fingerprint != content_pack.source_fingerprint
        or task.source_fingerprint != organization.source_fingerprint
    ):
        raise ValueError(
            "quality note source_fingerprint does not match its source bundle"
        )
    if organization.content_pack_sha256 != content_pack_sha256:
        raise ValueError("quality note content pack SHA does not match organization")

    evidence_by_id = content_pack.evidence_by_id()
    units_by_id = {unit.unit_id: unit for unit in organization.units}
    for unit in organization.units:
        for evidence_id in unit.evidence_ids:
            if evidence_id not in evidence_by_id:
                raise ValueError(
                    f"organization references unknown evidence id: {evidence_id}"
                )

    draft_by_slot: dict[ReaderSlot, list] = {}
    for section in draft.sections:
        draft_by_slot.setdefault(section.slot, []).extend(section.items)

    sections: list[QualityNoteSection] = []
    for section_order, spec in enumerate(template.sections, start=1):
        source_items = draft_by_slot.get(spec.slot, [])
        if spec.required and not source_items:
            raise ValueError(f"required reader section is empty: {spec.slot}")
        items: list[QualityNoteItem] = []
        for item_order, item in enumerate(source_items, start=1):
            unit_ids = _unique(item.evidence_unit_ids)
            if item.ai_supplement:
                if unit_ids:
                    raise ValueError("AI supplement cannot cite evidence units")
                items.append(
                    QualityNoteItem(
                        order=item_order,
                        text=item.text,
                        ai_supplement=True,
                    )
                )
                continue
            if not unit_ids:
                raise ValueError(
                    f"source item has no evidence units: {spec.slot} item {item_order}"
                )
            expanded_ids = _expand_unit_evidence_ids(unit_ids, units_by_id)
            if any(evidence_id not in evidence_by_id for evidence_id in expanded_ids):
                raise ValueError("quality note unit closure contains unknown evidence")
            items.append(
                QualityNoteItem(
                    order=item_order,
                    text=item.text,
                    evidence_unit_ids=unit_ids,
                    evidence_ids=expanded_ids,
                    locator=_derive_locator(spec.slot, expanded_ids, evidence_by_id),
                )
            )
        sections.append(
            QualityNoteSection(
                order=section_order,
                section_id=spec.section_id,
                slot=spec.slot,
                heading=spec.heading,
                purpose=spec.purpose,
                required=spec.required,
                items=items,
            )
        )

    return QualityNoteEnvelope(
        schema_version="1.0",
        task_id=task.task_id,
        source_fingerprint=task.source_fingerprint,
        content_pack_sha256=content_pack_sha256,
        template_id=template.template_id,
        template_sha256=_template_sha256(template),
        title=draft.title,
        sections=sections,
        ai_supplements=draft.ai_supplements,
    )


def render_quality_note(
    task: TaskRecord,
    content_pack: ContentPack,
    note: QualityNoteEnvelope,
    *,
    asset_prefix: str,
) -> str:
    """Render only program-owned Markdown and source-derived links/images."""
    evidence_by_id = content_pack.evidence_by_id()
    embedded_frames: set[str] = set()
    lines = [
        f"<!-- learnnest-quality-task-id: {task.task_id} -->",
        f"# {_escape(note.title)}",
        "",
    ]
    for section in note.sections:
        lines.extend([f"## {_escape(section.heading)}", ""])
        if section.purpose:
            lines.extend([f"> 学习目的：{_escape(section.purpose)}", ""])
        if not section.items:
            lines.extend(["- 本节没有可确认的内容。", ""])
            continue
        for item in section.items:
            if item.ai_supplement:
                lines.extend(
                    [
                        f"- {_escape(item.text)}",
                        "> AI 补充，不属于视频事实。",
                        "",
                    ]
                )
                continue
            prefix = f"{item.order}. " if section.slot == "tutorial_steps" else "- "
            lines.extend([f"{prefix}{_escape(item.text)}", ""])
            if item.locator is not None:
                lines.extend([f"链接：<{item.locator.url}>", ""])
            lines.extend([_evidence_comment(item.evidence_ids), ""])
            for evidence_id in item.evidence_ids:
                evidence = evidence_by_id[evidence_id]
                if evidence.kind != "frame" or evidence_id in embedded_frames:
                    continue
                embedded_frames.add(evidence_id)
                target = PurePosixPath(asset_prefix) / PurePosixPath(
                    evidence.artifact_path
                )
                lines.extend([f"![[{target}]]", ""])

    if note.ai_supplements:
        lines.extend(["## AI 补充", "", "> AI 补充，不属于视频事实。", ""])
        lines.extend(f"- {_escape(item.text)}" for item in note.ai_supplements)
        lines.append("")

    referenced_ids = _referenced_evidence_ids(note)
    lines.extend(["## 来源与追溯", "", f"- 任务：`{task.task_id}`"])
    lines.extend(
        [
            f"- 内容包：[[{PurePosixPath(asset_prefix) / 'content_pack.json'}|结构化证据]]",
            f"- 追溯材料：[[{PurePosixPath(asset_prefix) / 'trace.md'}|字幕、画面与 OCR]]",
            "",
            "<details>",
            f"<summary>展开证据 ID（{len(referenced_ids)} 项）</summary>",
            "",
        ]
    )
    lines.extend(f"- `{evidence_id}`" for evidence_id in referenced_ids)
    lines.extend(["", "</details>"])
    return "\n".join(lines) + "\n"


def quality_note_sha256(note: QualityNoteEnvelope) -> str:
    payload = json.dumps(
        note.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _template_sha256(template: ReaderTemplateManifest) -> str:
    from learnnest.reader_templates import reader_template_snapshot_sha256

    return reader_template_snapshot_sha256(template)


def _expand_unit_evidence_ids(
    unit_ids: Iterable[str], units_by_id: dict[str, object]
) -> list[str]:
    expanded: list[str] = []
    for unit_id in unit_ids:
        unit = units_by_id.get(unit_id)
        if unit is None:
            raise ValueError(f"unknown evidence unit: {unit_id}")
        for evidence_id in unit.evidence_ids:
            if evidence_id not in expanded:
                expanded.append(evidence_id)
    return expanded


def _derive_locator(
    slot: ReaderSlot,
    evidence_ids: list[str],
    evidence_by_id: dict[str, Evidence],
) -> QualityNoteLocator | None:
    if not slot.startswith("resource_"):
        return None
    matches: list[tuple[str, str]] = []
    for evidence_id in evidence_ids:
        text = evidence_by_id[evidence_id].text or ""
        matches.extend(
            (match.group(0).rstrip(".,;。；"), evidence_id)
            for match in _URL_PATTERN.finditer(text)
        )
    unique_urls = list(dict.fromkeys(url for url, _ in matches))
    if len(unique_urls) != 1:
        return None
    return QualityNoteLocator(
        url=unique_urls[0],
        evidence_ids=[evidence_id for _url, evidence_id in matches],
    )


def _referenced_evidence_ids(note: QualityNoteEnvelope) -> list[str]:
    result: list[str] = []
    for section in note.sections:
        for item in section.items:
            for evidence_id in item.evidence_ids:
                if evidence_id not in result:
                    result.append(evidence_id)
    return result


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _evidence_comment(evidence_ids: list[str]) -> str:
    return f"<!-- evidence: {', '.join(evidence_ids)} -->"


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )
