"""Secret-free policy and execution facts for paid local automation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from learnnest.assisted_note_models import AssistedConnectionSnapshot


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AutomationBudget(_Model):
    """Maximum provider requests, including configured retry attempts."""

    writer_per_day: int = Field(default=20, ge=0, le=200)
    reviewer_per_day: int = Field(default=20, ge=0, le=200)
    podcast_per_day: int = Field(default=20, ge=0, le=200)
    tts_per_day: int = Field(default=20, ge=0, le=200)


class AutomationPolicy(_Model):
    """One versioned, explicitly authorized automatic delivery policy."""

    schema_version: Literal["1.0"] = "1.0"
    enabled: bool = False
    authorized_at: datetime | None = None
    schedule_id: str = Field(min_length=1, max_length=64)
    writer: AssistedConnectionSnapshot
    reviewer: AssistedConnectionSnapshot
    dossier_schema_version: Literal["1.1"] = "1.1"
    max_items_per_tick: int = Field(default=1, ge=1, le=20)
    paid_retry_limit: int = Field(default=1, ge=0, le=3)
    budget: AutomationBudget = Field(default_factory=AutomationBudget)


class AutomationAttempt(_Model):
    """A secret-free paid-stage attempt retained for cost and recovery decisions."""

    stage: Literal["writer", "reviewer", "podcast", "tts"]
    attempt: int = Field(ge=1, le=4)
    status: Literal["running", "completed", "failed", "unknown"]
    started_at: datetime
    completed_at: datetime | None = None
    safe_summary: str | None = None
    response_path: str | None = None


class AutomationTaskState(_Model):
    task_id: str = Field(min_length=1)
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_path: str | None = None
    attempts: list[AutomationAttempt] = Field(default_factory=list)
    status: Literal[
        "pending",
        "writer_failed",
        "review_failed",
        "model_reviewed",
        "podcast_failed",
        "tts_failed",
        "completed",
        "needs_attention",
    ] = "pending"


class AutomationStatus(_Model):
    policy: AutomationPolicy
    policy_sha256: str
    last_tick_at: datetime | None = None
    last_tick_summary: str | None = None
