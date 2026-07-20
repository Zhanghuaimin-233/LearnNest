"""Versioned structured contract for speech-oriented podcast scripts."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _validate_speech_text(value: str) -> str:
    forbidden = (
        "\n",
        "\r",
        "[[",
        "]]",
        "```",
        "`",
        "http://",
        "https://",
    )
    if any(token in value for token in forbidden):
        raise ValueError("speech text must not contain Markdown, links, or line breaks")
    if re.search(r"\b(?:tr|fr|ocr|ai)_\d{4,}\b", value):
        raise ValueError("speech text must not contain evidence ids")
    return value


def _validate_title(value: str) -> str:
    forbidden = ("\n", "\r", "[[", "]]", "`", "http://", "https://")
    if any(token in value for token in forbidden):
        raise ValueError("title must not contain markup or links")
    return value


NonEmptyString = Annotated[str, Field(min_length=1)]
SpeechText = Annotated[
    str,
    Field(min_length=1),
    AfterValidator(_validate_speech_text),
]
SafeTitle = Annotated[
    str,
    Field(min_length=1),
    AfterValidator(_validate_title),
]


class _PodcastModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PodcastSegment(_PodcastModel):
    order: int = Field(ge=1)
    kind: Literal["intro", "body", "recap", "outro"]
    text: SpeechText
    evidence_ids: list[NonEmptyString] = Field(min_length=1)


class PodcastSupplement(_PodcastModel):
    text: SpeechText


class PodcastScript(_PodcastModel):
    schema_version: Literal["1.0"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    note_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: SafeTitle
    segments: list[PodcastSegment] = Field(min_length=3)
    ai_supplements: list[PodcastSupplement]

    @model_validator(mode="after")
    def validate_listening_structure(self) -> PodcastScript:
        orders = [segment.order for segment in self.segments]
        if orders != list(range(1, len(self.segments) + 1)):
            raise ValueError("podcast segment order must start at 1 and be contiguous")
        if self.segments[0].kind != "intro":
            raise ValueError("podcast first segment must be intro")
        if self.segments[-1].kind != "outro":
            raise ValueError("podcast last segment must be outro")
        if any(segment.kind == "intro" for segment in self.segments[1:]):
            raise ValueError("podcast intro must appear only once at the start")
        if any(segment.kind == "outro" for segment in self.segments[:-1]):
            raise ValueError("podcast outro must appear only once at the end")
        if not any(segment.kind == "body" for segment in self.segments):
            raise ValueError("podcast requires at least one body segment")
        return self
