"""Versioned serializable contracts for recoverable batch execution."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from learnnest.discovery_models import DiscoveredLink
from learnnest.execution_models import FailureInfo
from learnnest.models import TaskProfile
from learnnest.source_models import SourceItem

BatchKind = Literal["manual", "scan", "scheduled"]
BatchPurpose = Literal["processing", "discovery"]
BatchStatus = Literal["pending", "running", "partial", "completed", "interrupted"]
BatchItemStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "skipped_duplicate",
]


class BatchItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    position: int = Field(ge=1)
    source: SourceItem
    input: str = Field(min_length=1)
    input_type: Literal["local_file", "url"]
    source_fingerprint: str = Field(min_length=1)
    status: BatchItemStatus
    task_id: str | None = Field(default=None, min_length=1)
    attempt_id: str | None = Field(default=None, min_length=1)
    error: str | None = Field(default=None, min_length=1, max_length=500)
    failure: FailureInfo | None = None

    @model_validator(mode="after")
    def source_must_match_flat_identity(self) -> BatchItemResult:
        if self.source.input != self.input or self.source.input_type != self.input_type:
            raise ValueError("batch source must match flat input identity")
        return self


class BatchManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["2.0"] = "2.0"
    batch_id: str = Field(min_length=1)
    kind: BatchKind = "manual"
    status: BatchStatus
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    profile: TaskProfile
    purpose: BatchPurpose = "processing"
    schedule_id: str | None = Field(default=None, min_length=1)
    cursor_before: str | None = Field(default=None, min_length=1)
    cursor_after: str | None = Field(default=None, min_length=1)
    discovery_links: list[DiscoveredLink] = Field(default_factory=list)
    results: list[BatchItemResult]

    @field_validator("created_at", "updated_at", "finished_at")
    @classmethod
    def require_timezone_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("batch timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_manifest_state(self) -> BatchManifest:
        positions = [item.position for item in self.results]
        if positions != list(range(1, len(self.results) + 1)):
            raise ValueError("batch item positions must be contiguous from 1")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.status in {"pending", "running"}:
            if self.finished_at is not None:
                raise ValueError("unfinished batch must not have finished_at")
        elif self.finished_at is None:
            raise ValueError("finished batch requires finished_at")
        if self.finished_at is not None and self.finished_at < self.created_at:
            raise ValueError("finished_at must not precede created_at")
        if self.status == "completed" and any(
            item.status in {"pending", "running", "failed"} for item in self.results
        ):
            raise ValueError(
                "completed batch cannot contain unfinished or failed items"
            )
        if self.status == "partial" and (
            not any(item.status == "failed" for item in self.results)
            or any(item.status in {"pending", "running"} for item in self.results)
        ):
            raise ValueError(
                "partial batch requires final items and at least one failure"
            )
        return self

    @property
    def completed_count(self) -> int:
        return sum(result.status == "completed" for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(result.status == "failed" for result in self.results)

    @property
    def skipped_count(self) -> int:
        return sum(result.status == "skipped_duplicate" for result in self.results)
