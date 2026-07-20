"""Pure parsing and normalization for v0.4 source inputs."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from learnnest.note_types import ConcreteNoteType, normalize_note_type
from learnnest.source_models import SourceItem
from learnnest.util import source_fingerprint, url_source_fingerprint

VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})


class SourceParseError(ValueError):
    """A stable, user-facing source parsing failure."""


class _SourceOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    input: str = Field(min_length=1)
    input_type: str | None = None
    title: str | None = Field(default=None, min_length=1)
    content_type: str | None = Field(default=None, min_length=1)
    note_type: ConcreteNoteType | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("note_type", mode="before")
    @classmethod
    def normalize_note_type_override(cls, value: object) -> object:
        if isinstance(value, str):
            return normalize_note_type(value)
        return value


def parse_source(value: str, *, base_dir: Path | None = None) -> SourceItem:
    """Normalize one URL or existing local video without side effects."""
    candidate = value.strip()
    if not candidate:
        raise SourceParseError("source input must not be empty")
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() in {"http", "https"}:
        return SourceItem(
            input=_normalize_url(parsed),
            input_type="url",
        )
    if parsed.scheme and len(parsed.scheme) > 1:
        raise SourceParseError(f"unsupported source URL scheme: {parsed.scheme}")

    path = Path(candidate)
    if not path.is_absolute():
        path = (base_dir or Path.cwd()) / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SourceParseError(f"local source does not exist: {path}") from error
    if not resolved.is_file():
        raise SourceParseError(f"local source is not a file: {resolved}")
    if resolved.suffix.lower() not in VIDEO_EXTENSIONS:
        raise SourceParseError(f"unsupported local video type: {resolved.suffix}")
    return SourceItem(input=str(resolved), input_type="local_file")


def parse_tasks_file(path: Path) -> list[SourceItem]:
    """Parse TXT or JSONL inputs relative to the task file directory."""
    task_path = path.resolve(strict=True)
    if not task_path.is_file():
        raise SourceParseError(f"tasks path is not a file: {task_path}")
    try:
        lines = task_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise SourceParseError(
            f"tasks file could not be read as UTF-8: {task_path.name}"
        ) from error

    sources: list[SourceItem] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if task_path.suffix.lower() == ".jsonl":
            sources.append(
                _parse_jsonl_line(task_path, line_number, line, task_path.parent)
            )
        else:
            try:
                sources.append(parse_source(line, base_dir=task_path.parent))
            except SourceParseError as error:
                raise SourceParseError(
                    f"{task_path.name}:{line_number}: {error}"
                ) from error
    if not sources:
        raise SourceParseError(f"tasks file contains no sources: {task_path.name}")
    return sources


def scan_input_directory(path: Path, *, recursive: bool = False) -> list[SourceItem]:
    """Return supported videos in deterministic order."""
    directory = path.resolve(strict=True)
    if not directory.is_dir():
        raise SourceParseError(f"input directory is not a directory: {directory}")
    iterator = directory.rglob("*") if recursive else directory.iterdir()
    videos = sorted(
        (
            candidate.resolve()
            for candidate in iterator
            if candidate.is_file() and candidate.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=lambda candidate: candidate.as_posix().casefold(),
    )
    if not videos:
        raise SourceParseError(
            f"input directory contains no supported videos: {directory}"
        )
    return [SourceItem(input=str(video), input_type="local_file") for video in videos]


def collect_sources(
    *,
    input_value: str | None = None,
    tasks_path: Path | None = None,
    input_dir: Path | None = None,
    recursive: bool = False,
) -> list[SourceItem]:
    """Resolve exactly one CLI input mode into normalized SourceItems."""
    selected = sum(value is not None for value in (input_value, tasks_path, input_dir))
    if selected != 1:
        raise SourceParseError(
            "exactly one of INPUT, --tasks, or --input-dir is required"
        )
    if input_value is not None:
        return [parse_source(input_value)]
    if tasks_path is not None:
        return parse_tasks_file(tasks_path)
    assert input_dir is not None
    return scan_input_directory(input_dir, recursive=recursive)


def source_item_fingerprint(source: SourceItem) -> str:
    """Return the stable identity hash for one normalized logical source."""
    if source.input_type == "url":
        return url_source_fingerprint(source.input)
    return source_fingerprint(source.input)


def _parse_jsonl_line(
    task_path: Path,
    line_number: int,
    line: str,
    base_dir: Path,
) -> SourceItem:
    try:
        payload = json.loads(line)
        overrides = _SourceOverrides.model_validate(payload)
        normalized = parse_source(overrides.input, base_dir=base_dir)
    except (json.JSONDecodeError, ValidationError, SourceParseError) as error:
        raise SourceParseError(f"{task_path.name}:{line_number}: {error}") from error
    if (
        overrides.input_type is not None
        and overrides.input_type != normalized.input_type
    ):
        raise SourceParseError(
            f"{task_path.name}:{line_number}: input_type does not match input"
        )
    return normalized.model_copy(
        update={
            "title": overrides.title,
            "content_type": overrides.content_type,
            "note_type": overrides.note_type,
            "tags": overrides.tags,
        }
    )


def _normalize_url(parsed: SplitResult) -> str:
    if not parsed.hostname:
        raise SourceParseError("source URL requires a host")
    if parsed.username is not None or parsed.password is not None:
        raise SourceParseError("source URL must not contain credentials")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as error:
        raise SourceParseError("source URL has an invalid port") from error
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parsed.path, parsed.query, ""))
