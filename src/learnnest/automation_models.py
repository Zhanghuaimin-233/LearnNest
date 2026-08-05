"""Secret-free policy and execution facts for paid local automation."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from learnnest.assisted_note_models import AssistedConnectionSnapshot


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AutomationBudget(_Model):
    """One UTC-day provider-call budget shared by every paid stage and task."""

    provider_calls_per_day: int = Field(default=80, ge=0, le=800)
    # Read-only compatibility fields.  They are accepted when loading old
    # policy JSON but excluded from every new serialized policy.
    writer_per_day: int = Field(default=20, ge=0, le=200, exclude=True)
    reviewer_per_day: int = Field(default=20, ge=0, le=200, exclude=True)
    podcast_per_day: int = Field(default=20, ge=0, le=200, exclude=True)
    tts_per_day: int = Field(default=20, ge=0, le=200, exclude=True)
    budget_group_calls_per_day: dict[
        Literal["note", "podcast", "tts", "asr", "ocr"], int
    ] = Field(
        default_factory=lambda: {
            "note": 20,
            "podcast": 20,
            "tts": 20,
            "asr": 20,
            "ocr": 20,
        }
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_budgets(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        has_explicit_groups = "budget_group_calls_per_day" in data
        has_legacy_groups = any(
            field in data
            for field in (
                "writer_per_day",
                "reviewer_per_day",
                "podcast_per_day",
                "tts_per_day",
            )
        )
        if not has_explicit_groups:
            data["budget_group_calls_per_day"] = {
                "note": min(
                    int(data.get("writer_per_day", 20)),
                    int(data.get("reviewer_per_day", 20)),
                ),
                "podcast": int(data.get("podcast_per_day", 20)),
                "tts": int(data.get("tts_per_day", 20)),
                "asr": 20,
                "ocr": 20,
            }
        groups = data["budget_group_calls_per_day"]
        if not isinstance(groups, Mapping):
            raise ValueError("legacy provider budget groups are invalid")
        strictest_group_cap = min(int(value) for value in groups.values())
        if "provider_calls_per_day" not in data:
            data["provider_calls_per_day"] = strictest_group_cap
        elif has_legacy_groups:
            data["provider_calls_per_day"] = min(
                int(data["provider_calls_per_day"]), strictest_group_cap
            )
        return data


class AutomationPolicy(_Model):
    """One versioned, explicitly authorized automatic delivery policy."""

    schema_version: Literal["1.1"] = "1.1"
    enabled: bool = False
    authorized_at: datetime | None = None
    schedule_id: str = Field(min_length=1, max_length=64)
    writer: AssistedConnectionSnapshot
    reviewer: AssistedConnectionSnapshot
    provider_settings_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    dossier_schema_version: Literal["1.1"] = "1.1"
    max_items_per_tick: int = Field(default=1, ge=1, le=20)
    retries_per_stage: int = Field(default=3, ge=0, le=3)
    # Compatibility input/accessor for callers written against policy 1.0.
    # It is never written to new JSON and is kept synchronized by model_copy.
    paid_retry_limit: int = Field(default=3, ge=0, le=3, exclude=True)
    budget: AutomationBudget = Field(default_factory=AutomationBudget)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_policy(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        retries = data.get("retries_per_stage", data.get("paid_retry_limit", 3))
        data["retries_per_stage"] = retries
        data["paid_retry_limit"] = retries
        data["schema_version"] = "1.1"
        return data

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> AutomationPolicy:
        updates = dict(update or {})
        if "paid_retry_limit" in updates and "retries_per_stage" not in updates:
            updates["retries_per_stage"] = updates["paid_retry_limit"]
        elif "retries_per_stage" in updates and "paid_retry_limit" not in updates:
            updates["paid_retry_limit"] = updates["retries_per_stage"]
        return super().model_copy(update=updates, deep=deep)


class AutomationAttempt(_Model):
    """A secret-free paid-stage attempt retained for cost and recovery decisions."""

    call_id: str | None = Field(default=None, min_length=1)
    billing: Literal["paid", "local"] = "paid"
    stage: Literal["writer", "reviewer", "podcast", "tts"]
    attempt: int = Field(ge=1, le=4)
    status: Literal["running", "completed", "failed", "unknown"]
    started_at: datetime
    completed_at: datetime | None = None
    disposition: (
        Literal["retryable", "manual", "terminal", "unknown", "budget_exhausted"] | None
    ) = None
    safe_summary: str | None = None
    response_path: str | None = None
    next_retry_at: datetime | None = None

    @field_validator("started_at", "completed_at", "next_retry_at")
    @classmethod
    def require_timezone_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("automation attempt timestamps must be timezone-aware")
        return value


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
    blocked_reason: (
        Literal[
            "provider_budget_exhausted",
            "unknown_result",
            "non_retryable_failure",
            "stage_attempt_limit",
            "retry_wait",
        ]
        | None
    ) = None


class AutomationStatus(_Model):
    schema_version: Literal["1.1"] = "1.1"
    policy: AutomationPolicy
    policy_sha256: str
    last_tick_at: datetime | None = None
    last_tick_summary: str | None = None
