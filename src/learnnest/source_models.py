"""Versioned logical source contracts for local and URL inputs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from learnnest.note_types import ConcreteNoteType


class SourceItem(BaseModel):
    """One normalized logical input before media acquisition."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    input: str = Field(min_length=1)
    input_type: Literal["local_file", "url"]
    title: str | None = Field(default=None, min_length=1)
    content_type: str | None = Field(default=None, min_length=1)
    note_type: ConcreteNoteType | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    tags: list[str] = Field(default_factory=list)


class AcquiredSource(BaseModel):
    """One logical source resolved to a task-local or existing media file."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    source_input: str = Field(min_length=1)
    source_type: Literal["local_file", "url"]
    source_fingerprint: str = Field(min_length=1)
    media_path: str = Field(min_length=1)
    platform_id: str | None = Field(default=None, min_length=1)
    platform_title: str | None = Field(default=None, min_length=1)
    subtitle_path: str | None = Field(default=None, min_length=1)
