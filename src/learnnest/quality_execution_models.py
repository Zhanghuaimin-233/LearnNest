"""Persisted plan and state contracts for explicit quality-first execution."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from learnnest.execution_models import (
    QualityFailureCode,
    QualityFailurePhase,
    QualityRetryability,
)

QualityReviewMode = Literal["none", "report", "gate"]
QualityRole = Literal["organizer", "writer", "reviewer"]
QualityRoleStatus = Literal["pending", "running", "completed", "failed", "skipped"]
QualityTaskStatus = Literal[
    "planned",
    "organized",
    "source_valid",
    "quality_reported",
    "quality_gate_passed",
    "active",
    "failed",
]
QualityActivationDecision = Literal[
    "not_decided",
    "activate_quality_note",
    "retain_previous_active",
]


class _QualityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class QualityShardPlan(_QualityModel):
    shard_id: str = Field(pattern=r"^shard_[0-9]{4,}$")
    atom_ids: list[str] = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class QualityTaskPlan(_QualityModel):
    task_id: str = Field(min_length=1)
    task_dir: str = Field(min_length=1)
    content_pack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    organization_input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    template_snapshot: dict[str, object]
    template_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    shard_size: int = Field(ge=1)
    shards: list[QualityShardPlan] = Field(min_length=1)
    organizer_provider: str = Field(min_length=1)
    organizer_model: str = Field(min_length=1)
    reused_organization_path: str | None = Field(default=None, min_length=1)
    reused_organization_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    writer_provider: str = Field(min_length=1)
    writer_model: str = Field(min_length=1)
    reviewer_provider: str | None = Field(default=None, min_length=1)
    reviewer_model: str | None = Field(default=None, min_length=1)
    review_mode: QualityReviewMode
    max_calls: int = Field(ge=1)

    @model_validator(mode="after")
    def call_budget_is_explicit(self) -> QualityTaskPlan:
        reused = self.reused_organization_sha256 is not None
        if reused != (self.reused_organization_path is not None):
            raise ValueError("reused organization path and SHA must be set together")
        organizer_calls = 0 if reused else len(self.shards)
        expected = organizer_calls + 1 + (1 if self.review_mode != "none" else 0)
        if self.max_calls != expected:
            raise ValueError("quality task max_calls must match its role plan")
        if self.review_mode == "none" and (
            self.reviewer_provider is not None or self.reviewer_model is not None
        ):
            raise ValueError("reviewer configuration is not allowed in none mode")
        if self.review_mode != "none" and (
            not self.reviewer_provider or not self.reviewer_model
        ):
            raise ValueError("reviewer configuration is required for report or gate")
        return self


class QualityExecutionPlan(_QualityModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    created_at: datetime
    tasks: list[QualityTaskPlan] = Field(min_length=1)
    total_max_calls: int = Field(ge=1)

    @model_validator(mode="after")
    def plan_is_coherent(self) -> QualityExecutionPlan:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("quality plan created_at must be timezone-aware")
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("quality plan task IDs must be unique")
        if self.total_max_calls != sum(task.max_calls for task in self.tasks):
            raise ValueError("quality plan total_max_calls is inconsistent")
        return self


class QualityRoleState(_QualityModel):
    role: QualityRole
    max_calls: int = Field(ge=0)
    actual_call_count: int = Field(ge=0)
    status: QualityRoleStatus
    output_path: str | None = Field(default=None, min_length=1)
    failure_phase: QualityFailurePhase | None = None
    failure_code: QualityFailureCode | None = None
    retryability: QualityRetryability | None = None
    safe_summary: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def role_state_is_coherent(self) -> QualityRoleState:
        if self.actual_call_count > self.max_calls:
            raise ValueError("quality role call count exceeds its plan")
        if self.status == "failed" and (
            self.failure_phase is None or self.failure_code is None
        ):
            raise ValueError("failed quality role requires structured failure")
        return self


class QualityTaskState(_QualityModel):
    task_id: str = Field(min_length=1)
    status: QualityTaskStatus
    organizer: QualityRoleState
    writer: QualityRoleState
    reviewer: QualityRoleState
    activation_decision: QualityActivationDecision = "not_decided"
    candidate_path: str | None = Field(default=None, min_length=1)
    quality_report_path: str | None = Field(default=None, min_length=1)
    failure_phase: QualityFailurePhase | None = None
    failure_code: QualityFailureCode | None = None
    retryability: QualityRetryability | None = None
    safe_summary: str | None = Field(default=None, min_length=1, max_length=500)


class QualityPlanState(_QualityModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: str = Field(min_length=1)
    tasks: list[QualityTaskState] = Field(min_length=1)

    @model_validator(mode="after")
    def state_task_ids_are_unique(self) -> QualityPlanState:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("quality state task IDs must be unique")
        return self
