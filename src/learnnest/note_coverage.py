"""Source-only coverage plan contract for independent note review."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from learnnest.models import ContentPack
from learnnest.note_models import AnyGeneratedNote
from learnnest.note_review import NoteReview, build_statement_manifest
from learnnest.note_validation import iter_factual_statements

NonEmptyString = Annotated[str, Field(min_length=1)]


class _CoveragePlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CoveragePlanUnit(_CoveragePlanModel):
    """One central source unit that a later review must account for."""

    label: NonEmptyString
    source_evidence_ids: list[NonEmptyString] = Field(
        min_length=1,
        max_length=3,
        description=(
            "One to three minimal frozen source anchors for this local unit, "
            "not an exhaustive citation list."
        ),
    )
    rationale: NonEmptyString


class CoveragePlanVisual(_CoveragePlanModel):
    """One explicit use-or-omit decision for a source frame."""

    frame_evidence_id: NonEmptyString
    supporting_ocr_evidence_ids: list[NonEmptyString] = Field(
        max_length=3,
        description=(
            "At most three OCR anchors needed to identify a used visual; omit "
            "decisions use an empty list."
        ),
    )
    disposition: Literal["use", "omit"]
    unit_label: NonEmptyString | None
    rationale: NonEmptyString

    @model_validator(mode="after")
    def disposition_requires_matching_unit_label(self) -> CoveragePlanVisual:
        if self.disposition == "use" and self.unit_label is None:
            raise ValueError("used visual requires unit_label")
        if self.disposition == "omit" and self.unit_label is not None:
            raise ValueError("omitted visual requires null unit_label")
        if self.disposition == "omit" and self.supporting_ocr_evidence_ids:
            raise ValueError("omitted visual requires no supporting OCR evidence")
        return self


class CoveragePlan(_CoveragePlanModel):
    """Version 1.1 source-only plan frozen before candidate review."""

    schema_version: Literal["1.1"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    units: list[CoveragePlanUnit] = Field(min_length=1)
    visuals: list[CoveragePlanVisual]


COVERAGE_PLAN_ADAPTER = TypeAdapter(CoveragePlan)


def parse_coverage_plan(raw_text: str) -> CoveragePlan:
    """Parse a coverage plan without accepting prose or unknown fields."""
    return COVERAGE_PLAN_ADAPTER.validate_json(raw_text)


def generated_coverage_plan_json_schema() -> dict[str, object]:
    """Return the strict JSON schema used to generate a source-only plan."""
    return COVERAGE_PLAN_ADAPTER.json_schema()


def validate_coverage_plan(plan: CoveragePlan, content_pack: ContentPack) -> list[str]:
    """Validate plan identity and ensure every unit uses source evidence only."""
    errors: list[str] = []
    if plan.task_id != content_pack.task_id:
        errors.append("coverage plan task_id does not match content pack")
    if plan.source_fingerprint != content_pack.source_fingerprint:
        errors.append("coverage plan source_fingerprint does not match content pack")

    unit_labels = [unit.label for unit in plan.units]
    for duplicate in _duplicates(unit_labels):
        errors.append(f"coverage plan has duplicate unit label: {duplicate}")

    evidence_by_id = {item.id: item for item in content_pack.evidence}
    seen_unknown: set[str] = set()
    seen_ai_supplement: set[str] = set()
    for unit in plan.units:
        for duplicate in _duplicates(unit.source_evidence_ids):
            errors.append(
                f"coverage plan has duplicate source evidence id: {duplicate}"
            )
        for evidence_id in unit.source_evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None and evidence_id not in seen_unknown:
                errors.append(
                    f"coverage plan has unknown source evidence id: {evidence_id}"
                )
                seen_unknown.add(evidence_id)
            elif (
                evidence is not None
                and evidence.kind == "ai_supplement"
                and evidence_id not in seen_ai_supplement
            ):
                errors.append(
                    f"coverage plan cannot use AI supplement evidence: {evidence_id}"
                )
                seen_ai_supplement.add(evidence_id)

    visual_frame_ids = [visual.frame_evidence_id for visual in plan.visuals]
    for duplicate in _duplicates(visual_frame_ids):
        errors.append(f"coverage plan has duplicate frame visual decision: {duplicate}")
    decided_frame_ids = set(visual_frame_ids)
    for evidence in content_pack.evidence:
        if evidence.kind == "frame" and evidence.id not in decided_frame_ids:
            errors.append(
                f"coverage plan is missing frame visual decision: {evidence.id}"
            )

    unit_label_counts = {label: unit_labels.count(label) for label in set(unit_labels)}
    for visual in plan.visuals:
        frame_evidence = evidence_by_id.get(visual.frame_evidence_id)
        if frame_evidence is None:
            errors.append(
                "coverage plan has unknown frame visual evidence id: "
                f"{visual.frame_evidence_id}"
            )
        elif frame_evidence.kind == "ai_supplement":
            errors.append(
                "coverage plan visual cannot use AI supplement evidence: "
                f"{visual.frame_evidence_id}"
            )
        elif frame_evidence.kind != "frame":
            errors.append(
                "coverage plan visual evidence is not a frame: "
                f"{visual.frame_evidence_id}"
            )

        if visual.disposition == "use" and (
            visual.unit_label is None
            or unit_label_counts.get(visual.unit_label, 0) != 1
        ):
            errors.append(
                "coverage plan visual unit label does not resolve uniquely: "
                f"{visual.unit_label}"
            )

        for duplicate in _duplicates(visual.supporting_ocr_evidence_ids):
            errors.append(
                "coverage plan visual has duplicate supporting OCR evidence id: "
                f"{duplicate}"
            )
        for evidence_id in visual.supporting_ocr_evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                errors.append(
                    "coverage plan visual has unknown supporting OCR evidence id: "
                    f"{evidence_id}"
                )
            elif evidence.kind == "ai_supplement":
                errors.append(
                    "coverage plan visual cannot use AI supplement evidence: "
                    f"{evidence_id}"
                )
            elif evidence.kind != "ocr":
                errors.append(
                    "coverage plan supporting visual evidence is not OCR: "
                    f"{evidence_id}"
                )
            elif evidence.frame_id != visual.frame_evidence_id:
                errors.append(
                    "coverage plan supporting OCR parent does not match frame: "
                    f"{evidence_id}"
                )
    return errors


def validate_candidate_visuals_against_plan(
    candidate: AnyGeneratedNote,
    plan: CoveragePlan,
) -> list[str]:
    """Enforce frozen visual choices at the first rendered frame citation."""
    statement_evidence = [
        set(statement.evidence_ids) for statement in iter_factual_statements(candidate)
    ]
    errors: list[str] = []
    for visual in plan.visuals:
        if visual.disposition == "omit":
            if any(
                visual.frame_evidence_id in evidence_ids
                for evidence_ids in statement_evidence
            ):
                errors.append(
                    "candidate cites frame planned for omission: "
                    f"{visual.frame_evidence_id}"
                )
            continue
        required = {
            visual.frame_evidence_id,
            *visual.supporting_ocr_evidence_ids,
        }
        first_frame_evidence = next(
            (
                evidence_ids
                for evidence_ids in statement_evidence
                if visual.frame_evidence_id in evidence_ids
            ),
            None,
        )
        if first_frame_evidence is None or not required.issubset(first_frame_evidence):
            errors.append(
                "candidate does not cite planned visual evidence together: "
                f"{visual.frame_evidence_id}"
            )
            continue
        mapped_unit = next(
            (unit for unit in plan.units if unit.label == visual.unit_label),
            None,
        )
        if mapped_unit is not None and not set(
            mapped_unit.source_evidence_ids
        ).intersection(first_frame_evidence):
            errors.append(
                "candidate does not cite a mapped unit anchor with planned visual "
                f"evidence: {visual.frame_evidence_id}"
            )
    return errors


def validate_candidate_source_coverage_against_plan(
    candidate: AnyGeneratedNote,
    plan: CoveragePlan,
) -> list[str]:
    """Require each local plan unit's source anchors in one factual statement."""
    statement_evidence = [
        set(statement.evidence_ids) for statement in iter_factual_statements(candidate)
    ]
    errors: list[str] = []
    for index, unit in enumerate(plan.units, start=1):
        required = set(unit.source_evidence_ids)
        if not any(required.issubset(ids) for ids in statement_evidence):
            errors.append(
                "candidate does not cite coverage unit source evidence together: "
                f"{index}"
            )
    return errors


def validate_review_against_plan(
    review: NoteReview,
    plan: CoveragePlan,
    candidate: AnyGeneratedNote | None = None,
) -> list[str]:
    """Require reviewer coverage units to preserve the frozen plan exactly."""
    if len(review.coverage_units) != len(plan.units):
        return ["note review coverage unit count does not match plan"]

    errors: list[str] = []
    for index, (review_unit, plan_unit) in enumerate(
        zip(review.coverage_units, plan.units, strict=True), start=1
    ):
        if review_unit.label != plan_unit.label:
            errors.append(
                f"note review coverage unit {index} label does not match plan"
            )
        if review_unit.source_evidence_ids != plan_unit.source_evidence_ids:
            errors.append(
                "note review coverage unit "
                f"{index} source_evidence_ids do not match plan"
            )
    if candidate is None:
        return errors

    manifest = build_statement_manifest(candidate)
    evidence_by_path = {
        str(entry["candidate_path"]): set(entry["evidence_ids"]) for entry in manifest
    }
    for index, (review_unit, plan_unit) in enumerate(
        zip(review.coverage_units, plan.units, strict=True), start=1
    ):
        if review_unit.status == "missing":
            continue
        required = set(plan_unit.source_evidence_ids)
        if not any(
            required.issubset(evidence_by_path.get(path, set()))
            for path in review_unit.candidate_paths
        ):
            errors.append(
                "note review coverage unit "
                f"{index} candidate statement does not cite all plan source evidence"
            )

    review_unit_by_label = {unit.label: unit for unit in review.coverage_units}
    plan_unit_by_label = {unit.label: unit for unit in plan.units}
    for visual in plan.visuals:
        if visual.disposition == "omit" or visual.unit_label is None:
            continue
        first_path = next(
            (
                str(entry["candidate_path"])
                for entry in manifest
                if visual.frame_evidence_id in entry["evidence_ids"]
            ),
            None,
        )
        mapped_paths = set(
            review_unit_by_label[visual.unit_label].candidate_paths
            if visual.unit_label in review_unit_by_label
            else []
        )
        if first_path not in mapped_paths:
            errors.append(
                "planned visual first citation is outside mapped coverage unit: "
                f"{visual.frame_evidence_id}"
            )
            continue
        mapped_unit = plan_unit_by_label.get(visual.unit_label)
        if mapped_unit is not None and not set(
            mapped_unit.source_evidence_ids
        ).intersection(evidence_by_path.get(first_path, set())):
            errors.append(
                "planned visual first citation does not cite a mapped unit anchor: "
                f"{visual.frame_evidence_id}"
            )
    return errors


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates
