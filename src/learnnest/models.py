"""Versioned, serializable contracts shared by pipeline stages."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from learnnest.execution_models import SourceIdentities, TaskAttempt
from learnnest.note_types import ConcreteNoteType


class StageStatus(StrEnum):
    """A pipeline stage's lifecycle status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


StageName = Literal[
    "source",
    "transcript",
    "frames",
    "ocr",
    "evidence",
    "content_pack",
    "note",
    "publish",
    "podcast_script",
    "tts",
]

TaskProfile = Literal["evidence", "note", "full"]


class TaskRecord(BaseModel):
    """The stable task metadata later persisted as ``task.json``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0", "2.0"] = "2.0"
    task_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    source_input: str | None = Field(default=None, min_length=1)
    source_type: Literal["local_file", "url"] = "local_file"
    media_path: str | None = Field(default=None, min_length=1)
    source_fingerprint: str = Field(min_length=1)
    title: str = Field(min_length=1)
    profile: TaskProfile = "evidence"
    note_type_override: ConcreteNoteType | None = None
    stages: dict[StageName, StageStatus] = Field(default_factory=dict)
    artifacts: dict[StageName, list[Annotated[str, Field(min_length=1)]]] = Field(
        default_factory=dict
    )
    provider: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    providers: dict[str, Annotated[str, Field(min_length=1)]] = Field(
        default_factory=dict
    )
    models: dict[str, Annotated[str, Field(min_length=1)]] = Field(default_factory=dict)
    error_summary: str | None = None
    identities: SourceIdentities | None = None
    duplicate_of_task_id: str | None = Field(default=None, min_length=1)
    active_attempt_id: str | None = Field(default=None, min_length=1)
    attempts: list[TaskAttempt] = Field(default_factory=list)

    @model_validator(mode="after")
    def artifact_lists_must_be_declared(self) -> TaskRecord:
        if self.source_input is None:
            self.source_input = self.source_path
        if self.source_type == "local_file" and self.media_path is None:
            self.media_path = self.source_path
        if self.identities is None:
            self.identities = SourceIdentities(
                normalized_source=self.source_input or self.source_path
            )
        compact_stages = {"transcript", "ocr", "evidence"}
        if any(
            not paths and stage not in compact_stages
            for stage, paths in self.artifacts.items()
        ):
            raise ValueError("artifact paths must not be empty")
        attempt_ids = [attempt.attempt_id for attempt in self.attempts]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("attempt_id values must be unique")
        ordinals = [attempt.ordinal for attempt in self.attempts]
        if ordinals != list(range(1, len(self.attempts) + 1)):
            raise ValueError("attempt ordinals must be contiguous from 1")
        running = [attempt for attempt in self.attempts if attempt.status == "running"]
        if self.active_attempt_id is None:
            if running:
                raise ValueError("running attempt requires active_attempt_id")
        elif len(running) != 1 or running[0].attempt_id != self.active_attempt_id:
            raise ValueError(
                "active_attempt_id must reference the only running attempt"
            )
        return self


class TranscriptSegment(BaseModel):
    """A time-bounded ASR result before it is converted to evidence."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def end_must_not_precede_start(self) -> TranscriptSegment:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must not precede start_ms")
        return self


EvidenceKind = Literal["transcript", "frame", "ocr", "ai_supplement"]
_EVIDENCE_KIND_BY_ID_PREFIX: dict[str, EvidenceKind] = {
    "tr": "transcript",
    "fr": "frame",
    "ocr": "ocr",
    "ai": "ai_supplement",
}


class Evidence(BaseModel):
    """One attributable fact whose source type remains explicit."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^(tr|fr|ocr|ai)_[0-9]{4,}$")
    kind: EvidenceKind
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    text: str | None = Field(default=None, min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    artifact_path: str = Field(min_length=1)
    frame_id: str | None = Field(default=None, min_length=1)
    related_evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_fields(self) -> Evidence:
        prefix = self.id.split("_", 1)[0]
        expected_kind = _EVIDENCE_KIND_BY_ID_PREFIX[prefix]
        if self.kind != expected_kind:
            raise ValueError(
                f"evidence id prefix must match kind: {prefix} requires {expected_kind}"
            )
        if self.end_ms is not None and self.start_ms is not None:
            if self.end_ms < self.start_ms:
                raise ValueError("end_ms must not precede start_ms")
        if self.kind == "transcript" and (
            self.start_ms is None or self.end_ms is None or self.text is None
        ):
            raise ValueError("transcript evidence requires timestamps and text")
        if self.kind == "frame" and self.start_ms is None:
            raise ValueError("frame evidence requires start_ms")
        if self.kind == "ocr" and (self.frame_id is None or self.text is None):
            raise ValueError("ocr evidence requires frame_id and text")
        return self


class ContentPack(BaseModel):
    """The only structured evidence input exposed to LLMs and external agents."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = "1.0"
    task_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)

    def evidence_by_id(self) -> dict[str, Evidence]:
        """Return the canonical evidence lookup used by derived workflows."""
        return {item.id: item for item in self.evidence}

    @model_validator(mode="after")
    def evidence_ids_must_be_unique_and_resolvable(self) -> ContentPack:
        evidence_ids = [item.id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence ids must be unique")

        known_ids = set(evidence_ids)
        evidence_by_id = {item.id: item for item in self.evidence}
        for item in self.evidence:
            references = [*item.related_evidence_ids]
            if item.frame_id is not None:
                references.append(item.frame_id)
            for reference in references:
                if reference not in known_ids:
                    raise ValueError(f"unknown evidence id: {reference}")
            if item.kind == "ocr" and item.frame_id is not None:
                if evidence_by_id[item.frame_id].kind != "frame":
                    raise ValueError("ocr frame_id must reference frame evidence")
            if item.kind == "frame":
                for reference in item.related_evidence_ids:
                    if evidence_by_id[reference].kind != "ocr":
                        raise ValueError(
                            "frame related_evidence_ids must reference ocr evidence"
                        )
        return self
