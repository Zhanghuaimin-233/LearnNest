"""Versioned execution identity, attempt, and failure contracts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ExecutionStage = Literal[
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
AttemptReason = Literal["initial", "resume", "retry", "force", "scheduled"]
AttemptStatus = Literal[
    "running",
    "completed",
    "failed",
    "interrupted",
    "skipped_duplicate",
]
FailureDisposition = Literal["retryable", "manual", "terminal"]
QualityFailurePhase = Literal[
    "input",
    "organization",
    "writing",
    "source_validation",
    "quality",
    "publication",
    "recovery",
]
QualityFailureCode = Literal[
    "input_missing",
    "plan_mismatch",
    "provider_error",
    "invalid_response",
    "unknown_evidence_id",
    "cross_source_reference",
    "call_budget_exhausted",
    "source_validation_failed",
    "quality_rejected",
    "publication_recovery_failed",
    "recovery_not_possible",
]
QualityRetryability = Literal["local_recovery", "requires_new_plan", "terminal"]

_SENSITIVE_PATTERN = re.compile(
    r"(?i)(authorization\s*:|bearer\s+|api[_ -]?key\s*[:=]\s*\S+|cookie\s*:|sk-[a-z0-9_-]{8,})"
)


class _ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceIdentities(_ExecutionModel):
    normalized_source: str = Field(min_length=1)
    platform: str | None = Field(default=None, min_length=1)
    platform_id: str | None = Field(default=None, min_length=1)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FailureInfo(_ExecutionModel):
    code: str = Field(min_length=1)
    category: str = Field(min_length=1)
    disposition: FailureDisposition
    safe_summary: str = Field(min_length=1, max_length=500)

    @field_validator("safe_summary")
    @classmethod
    def reject_sensitive_summary(cls, value: str) -> str:
        if _SENSITIVE_PATTERN.search(value):
            raise ValueError("safe_summary contains sensitive material")
        return value


class TaskAttempt(_ExecutionModel):
    attempt_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ordinal: int = Field(ge=1)
    reason: AttemptReason
    from_stage: ExecutionStage
    status: AttemptStatus
    started_at: datetime
    finished_at: datetime | None = None
    failed_stage: ExecutionStage | None = None
    batch_id: str | None = Field(default=None, min_length=1)
    failure: FailureInfo | None = None
    next_retry_at: datetime | None = None

    @field_validator("started_at", "finished_at", "next_retry_at")
    @classmethod
    def require_timezone_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("execution timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> TaskAttempt:
        if self.status == "running":
            if self.finished_at is not None:
                raise ValueError("running attempt must not have finished_at")
        elif self.finished_at is None:
            raise ValueError("finished attempt requires finished_at")
        if self.status == "failed" and (
            self.failed_stage is None or self.failure is None
        ):
            raise ValueError("failed attempt requires failed_stage and failure")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self
