"""Versioned file facts for foreground scan schedules."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from learnnest.execution_models import FailureInfo
from learnnest.models import TaskProfile


class FolderScheduleSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["folder"] = "folder"
    path: str = Field(min_length=1)
    recursive: bool = False


class DouyinScheduleSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["douyin"] = "douyin"
    url: str = Field(min_length=1)
    include_default_video: bool = True
    folder_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_folder_ids(self) -> DouyinScheduleSource:
        if self.folder_ids is not None:
            if len(self.folder_ids) != len(set(self.folder_ids)):
                raise ValueError("Douyin folder_ids must not contain duplicates")
            if any(not folder_id for folder_id in self.folder_ids):
                raise ValueError("Douyin folder_ids must not contain empty values")
        if not self.include_default_video and self.folder_ids == []:
            raise ValueError("Douyin schedule must include video or a folder")
        return self


ScheduleSource = Annotated[
    FolderScheduleSource | DouyinScheduleSource,
    Field(discriminator="kind"),
]


class ManualTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["manual"] = "manual"


class IntervalTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["interval"] = "interval"
    every_seconds: int = Field(ge=60)


ScheduleTrigger = Annotated[
    ManualTrigger | IntervalTrigger,
    Field(discriminator="kind"),
]


class ScheduleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    schedule_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$",
    )
    status: Literal["enabled", "disabled", "blocked"]
    source: ScheduleSource
    trigger: ScheduleTrigger
    profile: TaskProfile = "evidence"
    cursor: str | None = None
    active_batch_id: str | None = Field(default=None, min_length=1)
    last_batch_id: str | None = Field(default=None, min_length=1)
    last_tick_at: datetime | None = None
    next_tick_at: datetime | None = None
    last_failure: FailureInfo | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "last_tick_at",
        "next_tick_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def require_timezone_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("schedule timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_schedule_state(self) -> ScheduleRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.trigger.kind == "manual" and self.next_tick_at is not None:
            raise ValueError("manual schedule cannot have next_tick_at")
        return self
