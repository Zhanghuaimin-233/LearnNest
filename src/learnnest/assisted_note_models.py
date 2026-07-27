"""Persisted contracts for the isolated assisted-draft note workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AssistedConnectionSnapshot(_Model):
    """Secret-free identity frozen into an assisted-draft plan."""

    connection_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    provider: str = Field(min_length=1)
    endpoint_identity: str = Field(min_length=1)
    model: str = Field(min_length=1)
    adapter_revision: str = Field(min_length=1, max_length=32)


class AssistedTaskPlan(_Model):
    task_id: str = Field(min_length=1)
    task_dir: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=1)
    content_pack_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dossier_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_calls: Literal[2] = 2


class AssistedExecutionPlan(_Model):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: str = Field(pattern=r"^assisted-[A-Za-z0-9._-]+$")
    created_at: datetime
    writer: AssistedConnectionSnapshot
    reviewer: AssistedConnectionSnapshot
    tasks: list[AssistedTaskPlan] = Field(min_length=1)
    total_max_calls: int = Field(ge=2)


AssistedRoleStatus = Literal["pending", "running", "completed", "failed"]
AssistedTaskStatus = Literal[
    "planned",
    "draft_ready",
    "model_reviewed",
    "writer_failed",
    "review_failed",
    "local_recovery_failed",
]


class AssistedRoleState(_Model):
    role: Literal["writer", "reviewer"]
    max_calls: Literal[1] = 1
    actual_call_count: int = Field(default=0, ge=0, le=1)
    status: AssistedRoleStatus = "pending"
    output_path: str | None = None
    safe_summary: str | None = None


class AssistedTaskState(_Model):
    task_id: str = Field(min_length=1)
    status: AssistedTaskStatus = "planned"
    writer: AssistedRoleState
    reviewer: AssistedRoleState
    candidate_path: str | None = None
    reviewed_path: str | None = None
    safe_summary: str | None = None


class AssistedPlanState(_Model):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: str = Field(min_length=1)
    tasks: list[AssistedTaskState] = Field(min_length=1)
