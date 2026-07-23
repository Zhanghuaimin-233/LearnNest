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
        "reviewer_flagged",
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
    units_by_id = {unit.unit_id: unit for unit in organization.units}
    issues: list[QualityIssue] = []
    required_visual_units = {
        unit.unit_id
        for unit in organization.units
        if unit.visual_role == "required_for_understanding" and unit.frame_ids
    }
    missing_visual = sorted(required_visual_units - used_unit_ids)
    if missing_visual:
        issues.append(
            QualityIssue(
                code="required_visual_missing",
                severity="high",
                message="关键视觉证据单元未进入最终笔记：" + ", ".join(missing_visual),
            )
        )

    unused_units = sorted(set(units_by_id) - used_unit_ids)
    if unused_units:
        issues.append(
            QualityIssue(
                code="unused_evidence_unit",
                severity="low",
                message="部分语义证据单元未被引用：" + ", ".join(unused_units),
            )
        )

    texts = [
        item.text.strip().lower()
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
        if len(item.text) > 500
    ]
    if long_items:
        issues.append(
            QualityIssue(
                code="long_paragraph",
                severity="medium",
                message="存在超过 500 字的单段正文。",
            )
        )

    slot_items = {
        section.slot: [item for item in section.items if not item.ai_supplement]
        for section in note.sections
    }
    if not slot_items.get("practice") and not slot_items.get("concept_practice"):
        issues.append(
            QualityIssue(
                code="practice_missing",
                severity="medium",
                message="笔记没有明确的实践或应用内容。",
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

    total_units = max(len(organization.units), 1)
    referenced_evidence_ids = {
        evidence_id
        for section in note.sections
        for item in section.items
        for evidence_id in item.evidence_ids
    }
    frame_ids = {item.id for item in content_pack.evidence if item.kind == "frame"}
    used_frames = referenced_evidence_ids & frame_ids
    visual_score = 5.0
    if required_visual_units:
        visual_score = 5.0 * (
            len(required_visual_units - set(missing_visual))
            / len(required_visual_units)
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
            explanation=f"引用了 {len(used_frames)} 个画面证据。",
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


def quality_report_json_schema() -> dict[str, object]:
    return TypeAdapter(QualityReport).json_schema()
