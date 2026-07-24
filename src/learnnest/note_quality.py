"""Deterministic quality signals kept separate from source validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from learnnest.evidence_unit_models import EvidenceUnitOrganization
from learnnest.models import ContentPack
from learnnest.note_models import QualityNoteEnvelope, QualityReviewerResponse


class QualityMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Literal[
        "narrative_coverage",
        "fact_coverage",
        "visual_utilization",
        "repetition",
        "practical_value",
        "review_value",
        "uncertainty",
        "citation_density",
        "visual_balance",
        "language_consistency",
    ]
    score: float = Field(ge=0, le=5)
    explanation: str = Field(min_length=1, max_length=500)


class QualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: Literal[
        "required_visual_missing",
        "repeated_content",
        "long_paragraph",
        "practice_missing",
        "review_missing",
        "uncertainty_missing",
        "unused_evidence_unit",
        "too_many_visuals",
        "reviewer_flagged",
        "output_language_mismatch",
        "citation_overload",
    ]
    severity: Literal["low", "medium", "high"]
    message: str = Field(min_length=1, max_length=500)
    section_id: str | None = Field(default=None, min_length=1)


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["passed", "flagged"]
    metrics: list[QualityMetric] = Field(min_length=1)
    issues: list[QualityIssue] = Field(default_factory=list)
    reviewer: QualityReviewerResponse | None = None


def analyze_quality(
    note: QualityNoteEnvelope,
    organization: EvidenceUnitOrganization,
    content_pack: ContentPack,
    *,
    reviewer: QualityReviewerResponse | None = None,
) -> QualityReport:
    """Return explainable warnings without mutating or deleting a source-valid note."""
    used_unit_ids = {
        unit_id
        for section in note.sections
        for item in section.items
        for unit_id in item.evidence_unit_ids
    }
    reader_units = {
        unit.unit_id: unit
        for unit in organization.units
        if unit.reader_relevance in {"core", "supporting"}
    }
    core_unit_ids = {
        unit.unit_id for unit in organization.units if unit.reader_relevance == "core"
    }
    issues: list[QualityIssue] = []
    source_language_ratio = _source_cjk_ratio(organization, content_pack)
    note_language_ratio = _note_cjk_ratio(note)
    language_mismatch = (
        source_language_ratio is not None
        and note_language_ratio is not None
        and (
            (source_language_ratio >= 0.45 and note_language_ratio <= 0.20)
            or (source_language_ratio <= 0.20 and note_language_ratio >= 0.45)
        )
    )
    if language_mismatch:
        issues.append(
            QualityIssue(
                code="output_language_mismatch",
                severity="medium",
                message=(
                    "正文主要语言与来源证据的主要语言不一致："
                    f"来源 CJK 比例 {source_language_ratio:.0%}，"
                    f"正文 CJK 比例 {note_language_ratio:.0%}。"
                ),
            )
        )
    required_visual_units = {
        unit.unit_id
        for unit in organization.units
        if unit.reader_relevance in {"core", "supporting"}
        and unit.visual_role == "required_for_understanding"
        and unit.frame_ids
    }
    used_visual_unit_ids = {
        item.visual_unit_id
        for section in note.sections
        for item in section.items
        if item.visual_unit_id is not None
    }
    cited_required_visual_units = required_visual_units & used_unit_ids
    required_visual_target = (
        min(3, (len(cited_required_visual_units) + 3) // 4)
        if cited_required_visual_units
        else 0
    )
    used_required_visual_units = cited_required_visual_units & used_visual_unit_ids
    missing_visual_count = max(
        required_visual_target - len(used_required_visual_units), 0
    )
    if missing_visual_count:
        issues.append(
            QualityIssue(
                code="required_visual_missing",
                severity="high",
                message=(
                    f"正文引用了 {len(cited_required_visual_units)} 个强视觉单元，"
                    f"至少需要 {required_visual_target} 张精选图片，"
                    f"当前仅使用 {len(used_required_visual_units)} 张。"
                ),
            )
        )

    unused_units = sorted(core_unit_ids - used_unit_ids)
    if unused_units:
        issues.append(
            QualityIssue(
                code="unused_evidence_unit",
                severity="low",
                message="部分语义证据单元未被引用：" + ", ".join(unused_units),
            )
        )

    texts = [
        item.markdown.strip().lower()
        for section in note.sections
        for item in section.items
        if not item.ai_supplement
    ]
    duplicate_count = len(texts) - len(set(texts))
    if duplicate_count:
        issues.append(
            QualityIssue(
                code="repeated_content",
                severity="medium",
                message=f"检测到 {duplicate_count} 条完全重复的正文。",
            )
        )
    long_items = [
        (section.section_id, item.order)
        for section in note.sections
        for item in section.items
        if len(item.markdown) > 800
    ]
    if long_items:
        issues.append(
            QualityIssue(
                code="long_paragraph",
                severity="medium",
                message="存在超过 800 字的正文块。",
            )
        )
    overloaded_items = [
        (section.section_id, item.order, len(item.evidence_unit_ids))
        for section in note.sections
        for item in section.items
        if len(item.evidence_unit_ids) > 8
    ]
    for section_id, _order, unit_count in overloaded_items:
        issues.append(
            QualityIssue(
                code="citation_overload",
                severity="medium",
                message=(
                    f"单个正文块引用了 {unit_count} 个语义证据单元；"
                    "来源闭包有效，但应拆分内容以避免证据倾倒。"
                ),
                section_id=section_id,
            )
        )

    slot_items = {
        section.slot: [item for item in section.items if not item.ai_supplement]
        for section in note.sections
    }
    if not slot_items.get("practice") and not slot_items.get("concept_practice"):
        practice_is_required = any(
            section.required
            for section in note.sections
            if section.slot in {"practice", "concept_practice"}
        )
        issues.append(
            QualityIssue(
                code="practice_missing",
                severity="medium" if practice_is_required else "low",
                message=(
                    "笔记缺少模板要求的实践或应用内容。"
                    if practice_is_required
                    else "笔记没有单独呈现实践或应用内容。"
                ),
            )
        )
    if not slot_items.get("review"):
        issues.append(
            QualityIssue(
                code="review_missing",
                severity="low",
                message="笔记没有快速复习内容。",
            )
        )
    if not slot_items.get("cautions"):
        issues.append(
            QualityIssue(
                code="uncertainty_missing",
                severity="low",
                message="笔记没有单独呈现注意事项或不确定性。",
            )
        )

    if reviewer is not None and reviewer.status == "flagged":
        issues.extend(
            QualityIssue(
                code="reviewer_flagged",
                severity=issue.severity,
                message=issue.message,
                section_id=issue.section_id,
            )
            for issue in reviewer.issues
        )

    total_units = max(len(reader_units), 1)
    used_frame_ids = {
        item.visual_evidence_id
        for section in note.sections
        for item in section.items
        if item.visual_evidence_id is not None
    }
    source_items = [
        item
        for section in note.sections
        for item in section.items
        if not item.ai_supplement and not item.derived_from_cited_note
    ]
    average_unit_refs = (
        sum(len(item.evidence_unit_ids) for item in source_items) / len(source_items)
        if source_items
        else 0.0
    )
    maximum_visuals = max(1, len(note.sections) // 2)
    if len(used_frame_ids) > maximum_visuals:
        issues.append(
            QualityIssue(
                code="too_many_visuals",
                severity="medium",
                message=(
                    f"显式图片达到 {len(used_frame_ids)} 张，超过当前正文建议上限 "
                    f"{maximum_visuals} 张。"
                ),
            )
        )
    visual_score = 5.0
    if required_visual_target:
        visual_score = 5.0 * (
            min(len(used_required_visual_units), required_visual_target)
            / required_visual_target
        )
    metrics = [
        QualityMetric(
            name="narrative_coverage",
            score=5.0 if slot_items.get("narrative") else 1.0,
            explanation="内容脉络章节已填写。"
            if slot_items.get("narrative")
            else "缺少内容脉络。",
        ),
        QualityMetric(
            name="fact_coverage",
            score=5.0 * len(used_unit_ids) / total_units,
            explanation=f"引用了 {len(used_unit_ids)}/{total_units} 个语义证据单元。",
        ),
        QualityMetric(
            name="visual_utilization",
            score=visual_score,
            explanation=f"显式使用了 {len(used_frame_ids)} 个画面证据。",
        ),
        QualityMetric(
            name="repetition",
            score=max(0.0, 5.0 - duplicate_count),
            explanation="重复正文越少分数越高。",
        ),
        QualityMetric(
            name="practical_value",
            score=5.0
            if slot_items.get("practice") or slot_items.get("concept_practice")
            else 2.0,
            explanation="已提供实践或应用内容。"
            if slot_items.get("practice") or slot_items.get("concept_practice")
            else "缺少实践内容。",
        ),
        QualityMetric(
            name="review_value",
            score=5.0 if slot_items.get("review") else 2.0,
            explanation="已提供复习内容。"
            if slot_items.get("review")
            else "缺少复习内容。",
        ),
        QualityMetric(
            name="uncertainty",
            score=5.0 if slot_items.get("cautions") else 3.0,
            explanation="已提供注意事项或局限。"
            if slot_items.get("cautions")
            else "未单独提供局限说明。",
        ),
        QualityMetric(
            name="citation_density",
            score=max(0.0, 5.0 - max(average_unit_refs - 1.0, 0.0) * 2.0),
            explanation=f"每个正文块平均引用 {average_unit_refs:.2f} 个证据单元。",
        ),
        QualityMetric(
            name="visual_balance",
            score=5.0
            if len(used_frame_ids) <= maximum_visuals
            else max(0.0, 5.0 - (len(used_frame_ids) - maximum_visuals)),
            explanation=(
                f"正文显式使用 {len(used_frame_ids)} 张图片，"
                f"建议上限为 {maximum_visuals} 张。"
            ),
        ),
        QualityMetric(
            name="language_consistency",
            score=1.0 if language_mismatch else 5.0,
            explanation=(
                "正文主要语言与来源证据不一致。"
                if language_mismatch
                else "正文主要语言与来源证据一致或来源语言不具备明确主导性。"
            ),
        ),
    ]
    status = (
        "flagged"
        if any(issue.severity in {"medium", "high"} for issue in issues)
        else "passed"
    )
    from learnnest.quality_note import quality_note_sha256

    return QualityReport(
        task_id=note.task_id,
        candidate_sha256=quality_note_sha256(note),
        status=status,
        metrics=metrics,
        issues=issues,
        reviewer=reviewer,
    )


def _source_cjk_ratio(
    organization: EvidenceUnitOrganization,
    content_pack: ContentPack,
) -> float | None:
    reader_evidence_ids = {
        evidence_id
        for unit in organization.units
        if unit.reader_relevance in {"core", "supporting"}
        for evidence_id in unit.evidence_ids
    }
    texts = [
        evidence.text
        for evidence in content_pack.evidence
        if evidence.id in reader_evidence_ids
        and evidence.kind in {"transcript", "ocr"}
        and evidence.text
    ]
    return _cjk_ratio("\n".join(texts))


def _note_cjk_ratio(note: QualityNoteEnvelope) -> float | None:
    texts = [note.title]
    texts.extend(
        item.markdown
        for section in note.sections
        for item in section.items
        if not item.ai_supplement
    )
    return _cjk_ratio("\n".join(texts))


def _cjk_ratio(text: str) -> float | None:
    cjk_count = sum("\u4e00" <= character <= "\u9fff" for character in text)
    latin_count = sum(
        ("a" <= character <= "z") or ("A" <= character <= "Z") for character in text
    )
    measured_count = cjk_count + latin_count
    if measured_count < 8:
        return None
    return cjk_count / measured_count


def quality_report_json_schema() -> dict[str, object]:
    return TypeAdapter(QualityReport).json_schema()
