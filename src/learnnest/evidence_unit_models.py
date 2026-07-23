"""Versioned contracts for quality-first semantic evidence units."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvidenceUnitType = Literal[
    "background",
    "concept",
    "relationship",
    "step",
    "case",
    "comparison",
    "conclusion",
    "resource",
    "practice_mapping",
]
VisualRole = Literal["required_for_understanding", "useful", "none"]


class _EvidenceUnitModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceAtom(_EvidenceUnitModel):
    """One lossless, deterministic projection of a source evidence item."""

    evidence_id: str = Field(pattern=r"^(tr|fr|ocr)_[0-9]{4,}$")
    kind: Literal["transcript", "frame", "ocr"]
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    text: str | None = Field(default=None, min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    artifact_path: str = Field(min_length=1)
    frame_id: str | None = Field(default=None, min_length=1)
    related_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def fields_match_kind(self) -> EvidenceAtom:
        prefix = self.evidence_id.split("_", 1)[0]
        if prefix == "tr" and self.kind != "transcript":
            raise ValueError("transcript atom id must match kind")
        if prefix == "fr" and self.kind != "frame":
            raise ValueError("frame atom id must match kind")
        if prefix == "ocr" and self.kind != "ocr":
            raise ValueError("ocr atom id must match kind")
        if self.end_ms is not None and self.start_ms is not None:
            if self.end_ms < self.start_ms:
                raise ValueError("end_ms must not precede start_ms")
        if self.kind == "transcript" and (
            self.start_ms is None or self.end_ms is None or self.text is None
        ):
            raise ValueError("transcript atom requires timestamps and text")
        if self.kind == "frame" and self.start_ms is None:
            raise ValueError("frame atom requires start_ms")
        if self.kind == "ocr" and (self.frame_id is None or self.text is None):
            raise ValueError("ocr atom requires frame_id and text")
        return self


class EvidenceUnitShard(_EvidenceUnitModel):
    """A deterministic input boundary for one Organizer call."""

    shard_id: str = Field(pattern=r"^shard_[0-9]{4,}$")
    atom_ids: list[str] = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def shard_is_coherent(self) -> EvidenceUnitShard:
        if self.end_ms < self.start_ms:
            raise ValueError("shard end_ms must not precede start_ms")
        if len(self.atom_ids) != len(set(self.atom_ids)):
            raise ValueError("shard atom_ids must be unique")
        return self


class EvidenceUnit(_EvidenceUnitModel):
    """A semantic organization layer that always retains raw evidence closure."""

    unit_id: str = Field(pattern=r"^eu_[0-9]{4,}$")
    shard_id: str = Field(pattern=r"^shard_[0-9]{4,}$")
    unit_type: EvidenceUnitType
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    topic_labels: list[str] = Field(default_factory=list, max_length=12)
    outline: str = Field(min_length=1, max_length=1_000)
    raw_evidence_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    transcript_ids: list[str] = Field(default_factory=list)
    frame_ids: list[str] = Field(default_factory=list)
    ocr_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceAtom] = Field(min_length=1)
    visual_role: VisualRole
    visual_reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def unit_is_coherent(self) -> EvidenceUnit:
        if self.end_ms < self.start_ms:
            raise ValueError("unit end_ms must not precede start_ms")
        for field_name in (
            "raw_evidence_ids",
            "evidence_ids",
            "transcript_ids",
            "frame_ids",
            "ocr_ids",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
        if len(self.topic_labels) != len(set(self.topic_labels)):
            raise ValueError("topic_labels must be unique")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if evidence_ids != self.evidence_ids:
            raise ValueError("evidence_ids must match evidence atoms")
        if set(self.raw_evidence_ids) - set(self.evidence_ids):
            raise ValueError("raw_evidence_ids must be included in evidence_ids")
        if set(self.transcript_ids) | set(self.frame_ids) | set(self.ocr_ids) != set(
            self.evidence_ids
        ):
            raise ValueError("typed evidence IDs must cover evidence_ids exactly")
        parent_frames = {
            item.frame_id
            for item in self.evidence
            if item.kind == "ocr" and item.frame_id is not None
        }
        if not parent_frames.issubset(self.frame_ids):
            raise ValueError("OCR evidence requires its parent frame in frame_ids")
        if self.visual_role == "none" and self.visual_reason is not None:
            raise ValueError("visual_reason is only valid for a visual role")
        if self.visual_role != "none" and not self.visual_reason:
            raise ValueError("visual role requires visual_reason")
        return self


class EvidenceUnitOrganization(_EvidenceUnitModel):
    """The persisted, source-bound result of all Organizer calls for one task."""

    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=1)
    content_pack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    units: list[EvidenceUnit] = Field(min_length=1)
    shard_ids: list[str] = Field(min_length=1)
    normalizations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def organization_is_coherent(self) -> EvidenceUnitOrganization:
        unit_ids = [unit.unit_id for unit in self.units]
        if len(unit_ids) != len(set(unit_ids)):
            raise ValueError("unit_id values must be unique")
        if len(self.shard_ids) != len(set(self.shard_ids)):
            raise ValueError("shard_ids must be unique")
        if any(unit.shard_id not in self.shard_ids for unit in self.units):
            raise ValueError("unit shard_id is not declared")
        return self


class EvidenceUnitProposal(_EvidenceUnitModel):
    """Provider-only Organizer output; program identity fields are excluded."""

    raw_evidence_ids: list[str] = Field(min_length=1)
    unit_type: EvidenceUnitType
    topic_labels: list[str] = Field(default_factory=list, max_length=12)
    outline: str = Field(min_length=1, max_length=1_000)
    visual_role: VisualRole
    visual_reason: str | None = Field(default=None, max_length=300)


class EvidenceUnitOrganizationResponse(_EvidenceUnitModel):
    """Strict JSON boundary returned by an Organizer provider."""

    schema_version: Literal["1.0"] = "1.0"
    units: list[EvidenceUnitProposal] = Field(min_length=1)
