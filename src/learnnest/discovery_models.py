"""Versioned facts for discovered links waiting for independent acquisition."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from learnnest.execution_models import FailureInfo
from learnnest.source_models import SourceItem


DiscoveryStatus = Literal["pending", "running", "downloaded", "failed"]
ContentKind = Literal["video", "image_text"]


class DiscoveredLink(BaseModel):
    """One safe, stable logical link produced by a source monitor."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    platform: Literal["douyin"] = "douyin"
    platform_id: str = Field(min_length=1)
    source: SourceItem
    content_kind: ContentKind = "video"
    folder_ids: list[str] = Field(default_factory=list)
    folder_names: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_stable_source(self) -> DiscoveredLink:
        if self.source.input_type != "url":
            raise ValueError("discovered Douyin links must use URL sources")
        expected = f"https://www.douyin.com/video/{self.platform_id}"
        if self.source.input != expected:
            raise ValueError(
                "discovered Douyin source must be the canonical video page URL"
            )
        parsed = urlsplit(self.source.input)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.douyin.com"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "discovered Douyin source must not contain signed URL data"
            )
        if (
            self.content_kind == "image_text"
            and self.source.content_type != "image_text"
        ):
            raise ValueError("image-text discovery must preserve source content_type")
        if len(self.folder_ids) != len(set(self.folder_ids)):
            raise ValueError("folder_ids must not contain duplicates")
        if any(not folder_id for folder_id in self.folder_ids):
            raise ValueError("folder_ids must not contain empty values")
        if not set(self.folder_names).issubset(self.folder_ids):
            raise ValueError("folder_names keys must be present in folder_ids")
        return self


class DiscoveryRecord(BaseModel):
    """Mutable download state for one discovered logical link."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0", "1.1"] = "1.1"
    discovery_id: str = Field(min_length=1)
    link: DiscoveredLink
    status: DiscoveryStatus = "pending"
    first_seen_at: datetime
    updated_at: datetime
    observed_position: int | None = Field(default=None, ge=0)
    last_observed_at: datetime | None = None
    lease_until: datetime | None = None
    attempt_count: int = Field(default=0, ge=0, le=4)
    task_id: str | None = Field(default=None, min_length=1)
    artifact_path: str | None = Field(default=None, min_length=1)
    failure: FailureInfo | None = None

    @field_validator("first_seen_at", "updated_at", "last_observed_at", "lease_until")
    @classmethod
    def require_timezone_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("discovery timestamps must be timezone-aware")
        return value

    @field_validator("artifact_path")
    @classmethod
    def require_relative_artifact_path(cls, value: str | None) -> str | None:
        if value is not None:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("artifact_path must stay relative to the output root")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> DiscoveryRecord:
        if self.attempt_count == 0 and self.status in {
            "running",
            "downloaded",
            "failed",
        }:
            self.attempt_count = 1
        if self.updated_at < self.first_seen_at:
            raise ValueError("updated_at must not precede first_seen_at")
        if (self.observed_position is None) != (self.last_observed_at is None):
            raise ValueError(
                "observed_position and last_observed_at must be set together"
            )
        if (
            self.last_observed_at is not None
            and self.last_observed_at < self.first_seen_at
        ):
            raise ValueError("last_observed_at must not precede first_seen_at")
        if self.status == "running" and self.lease_until is None:
            raise ValueError("running discovery records require a lease")
        if self.status != "running" and self.lease_until is not None:
            raise ValueError("only running discovery records may hold a lease")
        if self.status == "downloaded" and self.task_id is None:
            if self.artifact_path is None:
                raise ValueError(
                    "downloaded discovery records require task_id or artifact_path"
                )
        if self.status != "downloaded" and self.artifact_path is not None:
            raise ValueError("only downloaded discovery records may hold artifact_path")
        if self.status != "failed" and self.failure is not None:
            raise ValueError("only failed discovery records may hold failure")
        return self


class DiscoveryManifest(BaseModel):
    """The schedule-owned JSON ledger consumed by the download command."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0", "1.1"] = "1.1"
    schedule_id: str = Field(min_length=1)
    updated_at: datetime
    latest_observation_at: datetime | None = None
    records: list[DiscoveryRecord] = Field(default_factory=list)

    @field_validator("updated_at", "latest_observation_at")
    @classmethod
    def require_timezone_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("discovery manifest timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def records_must_be_unique(self) -> DiscoveryManifest:
        ids = [record.discovery_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("discovery_id values must be unique")
        return self


def discovery_id(link: DiscoveredLink) -> str:
    """Build an immutable logical identity from platform and platform ID."""
    digest = hashlib.sha256(
        f"{link.platform}\0{link.platform_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"douyin-{digest}"
