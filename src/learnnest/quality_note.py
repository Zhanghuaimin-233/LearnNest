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
_DERIVED_READER_SLOTS: set[ReaderSlot] = {
    "summary",
    "why_learn",
    "narrative",
    "practice",
    "cautions",
    "review",
    "concept_analogy",
    "concept_practice",
    "resource_audience",
    "resource_actions",
    "tutorial_checks",
    "tutorial_faq",
}


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

    cited_unit_ids = {
        unit_id
        for section in draft.sections
        for item in section.items
        if not item.ai_supplement
        for unit_id in item.evidence_unit_ids
    }
    reader_unit_ids = {
        unit.unit_id
        for unit in organization.units
        if unit.reader_relevance in {"core", "supporting"}
    }
    missing_reader_units = sorted(reader_unit_ids - cited_unit_ids)
    if missing_reader_units:
        raise ValueError(
            "Writer omitted reader-relevant evidence units: "
            + ", ".join(missing_reader_units)
        )

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
                        markdown=item.markdown,
                        ai_supplement=True,
                    )
                )
                continue
            if not unit_ids:
                if spec.slot in _DERIVED_READER_SLOTS:
                    items.append(
                        QualityNoteItem(
                            order=item_order,
                            markdown=item.markdown,
                            derived_from_cited_note=True,
                        )
                    )
                    continue
                raise ValueError(
                    f"source item has no evidence units: {spec.slot} item {item_order}"
                )
            cited_units = [units_by_id.get(unit_id) for unit_id in unit_ids]
            if any(unit is None for unit in cited_units):
                unknown = next(
                    unit_id
                    for unit_id, unit in zip(unit_ids, cited_units, strict=True)
                    if unit is None
                )
                raise ValueError(f"unknown evidence unit: {unknown}")
            if any(
                unit.reader_relevance not in {"core", "supporting"}
                for unit in cited_units
            ):
                raise ValueError("Writer cited a background or noise evidence unit")
            expanded_ids = _expand_unit_evidence_ids(unit_ids, units_by_id)
            citation_ids = _expand_unit_citation_ids(unit_ids, units_by_id)
            if any(evidence_id not in evidence_by_id for evidence_id in expanded_ids):
                raise ValueError("quality note unit closure contains unknown evidence")
            visual_evidence_id: str | None = None
            if item.visual_unit_id is not None:
                if item.visual_unit_id not in unit_ids:
                    raise ValueError("visual unit must be one of the cited units")
                visual_unit = units_by_id[item.visual_unit_id]
                visual_evidence_id = visual_unit.visual_anchor_id
                if visual_evidence_id is None:
                    raise ValueError("selected visual unit has no visual anchor")
            items.append(
                QualityNoteItem(
                    order=item_order,
                    markdown=item.markdown,
                    evidence_unit_ids=unit_ids,
                    citation_evidence_ids=citation_ids,
                    evidence_ids=expanded_ids,
                    visual_unit_id=item.visual_unit_id,
                    visual_evidence_id=visual_evidence_id,
                    locator=_derive_locator(spec.slot, citation_ids, evidence_by_id),
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
        schema_version="2.0",
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
    organization: EvidenceUnitOrganization,
    *,
    asset_prefix: str,
    provenance_path: str = "note.provenance.json",
) -> str:
    """Render portable reader Markdown with compact citations and explicit visuals."""
    evidence_by_id = content_pack.evidence_by_id()
    units_by_id = {unit.unit_id: unit for unit in organization.units}
    embedded_frames: set[str] = set()
    citation_notes: list[tuple[int, QualityNoteItem]] = []
    lines = [
        f"<!-- learnnest-quality-task-id: {task.task_id} -->",
        f"# {note.title}",
        "",
    ]
    for section in note.sections:
        if not section.items:
            continue
        lines.extend([f"## {section.heading}", ""])
        for item in section.items:
            if item.ai_supplement:
                lines.extend(
                    [
                        item.markdown,
                        "",
                        "> AI 补充，不属于视频事实。",
                        "",
                    ]
                )
                continue
            if item.derived_from_cited_note:
                lines.extend([item.markdown, ""])
                continue
            citation_number = len(citation_notes) + 1
            citation_notes.append((citation_number, item))
            lines.extend([_append_citation_marker(item.markdown, citation_number), ""])
            if item.locator is not None:
                lines.extend([f"资源链接：<{item.locator.url}>", ""])
            evidence_id = item.visual_evidence_id
            if evidence_id is not None and evidence_id not in embedded_frames:
                evidence = evidence_by_id[evidence_id]
                if evidence.kind != "frame":
                    raise ValueError("visual evidence must be a frame")
                embedded_frames.add(evidence_id)
                target = PurePosixPath(asset_prefix) / PurePosixPath(
                    evidence.artifact_path
                )
                lines.extend([f"![关键画面](<{target}>)", ""])

    if note.ai_supplements:
        lines.extend(["## AI 补充", "", "> AI 补充，不属于视频事实。", ""])
        lines.extend(f"- {item.text}" for item in note.ai_supplements)
        lines.append("")

    if citation_notes:
        lines.extend(["## 注释", ""])
        for number, item in citation_notes:
            units = [units_by_id[unit_id] for unit_id in item.evidence_unit_ids]
            time_range = _format_time_range(
                min(unit.start_ms for unit in units),
                max(unit.end_ms for unit in units),
            )
            anchor_ids = _unique(
                evidence_id
                for unit in units
                for evidence_id in unit.citation_anchor_ids
            )[:3]
            anchors = "、".join(f"`{evidence_id}`" for evidence_id in anchor_ids)
            lines.extend(
                [
                    f"[^{number}]: 视频 {time_range}；锚点 {anchors}。"
                    "完整证据闭包见追溯文件。",
                    "",
                ]
            )

    content_pack_target = PurePosixPath(asset_prefix) / "content_pack.json"
    trace_target = PurePosixPath(asset_prefix) / "trace.md"
    lines.extend(
        [
            "## 来源与追溯",
            "",
            f"- 任务：`{task.task_id}`",
            f"- [结构化证据](<{content_pack_target}>)",
            f"- [字幕、画面与 OCR](<{trace_target}>)",
            f"- [完整证据闭包](<{PurePosixPath(provenance_path)}>)",
        ]
    )
    return "\n".join(lines) + "\n"


def build_quality_note_provenance(
    note: QualityNoteEnvelope,
    organization: EvidenceUnitOrganization,
) -> dict[str, object]:
    """Build the machine-facing full evidence closure kept outside reader Markdown."""
    units_by_id = {unit.unit_id: unit for unit in organization.units}
    sections: list[dict[str, object]] = []
    for section in note.sections:
        items: list[dict[str, object]] = []
        for item in section.items:
            if item.ai_supplement:
                continue
            items.append(
                {
                    "order": item.order,
                    "evidence_unit_ids": item.evidence_unit_ids,
                    "citation_evidence_ids": item.citation_evidence_ids,
                    "evidence_ids": item.evidence_ids,
                    "visual_unit_id": item.visual_unit_id,
                    "visual_evidence_id": item.visual_evidence_id,
                    "derived_from_cited_note": item.derived_from_cited_note,
                }
            )
        if items:
            sections.append({"section_id": section.section_id, "items": items})
    referenced_unit_ids = _unique(
        unit_id
        for section in note.sections
        for item in section.items
        for unit_id in item.evidence_unit_ids
    )
    return {
        "schema_version": "1.0",
        "task_id": note.task_id,
        "source_fingerprint": note.source_fingerprint,
        "content_pack_sha256": note.content_pack_sha256,
        "quality_note_sha256": quality_note_sha256(note),
        "sections": sections,
        "evidence_units": [
            units_by_id[unit_id].model_dump(mode="json")
            for unit_id in referenced_unit_ids
        ],
    }


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


def _expand_unit_citation_ids(
    unit_ids: Iterable[str], units_by_id: dict[str, object]
) -> list[str]:
    anchors: list[str] = []
    for unit_id in unit_ids:
        unit = units_by_id.get(unit_id)
        if unit is None:
            raise ValueError(f"unknown evidence unit: {unit_id}")
        for evidence_id in unit.citation_anchor_ids:
            if evidence_id not in anchors:
                anchors.append(evidence_id)
    return anchors


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


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _format_time_range(start_ms: int, end_ms: int) -> str:
    return f"{_format_timestamp(start_ms)}–{_format_timestamp(end_ms)}"


def _append_citation_marker(markdown: str, number: int) -> str:
    lines = markdown.rstrip().splitlines()
    if not lines:
        return f"[^{number}]"
    if lines[-1].lstrip().startswith(("```", "~~~")):
        return f"{markdown.rstrip()}\n\n[^{number}]"
    lines[-1] = f"{lines[-1].rstrip()}[^{number}]"
    return "\n".join(lines)


def _format_timestamp(value_ms: int) -> str:
    total_seconds = max(value_ms, 0) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"
