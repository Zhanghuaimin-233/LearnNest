"""Minimal WebVTT/SRT parsing into the existing transcript contract."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any


class SubtitleError(ValueError):
    """An invalid or unusable platform subtitle file."""


_TIMESTAMP = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})")
_TAGS = re.compile(r"<[^>]+>")


def parse_subtitle_file(path: Path) -> dict[str, Any]:
    """Parse valid cues from one UTF-8 WebVTT or SRT file."""
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise SubtitleError("subtitle could not be read as UTF-8") from error
    segments: list[dict[str, Any]] = []
    blocks = re.split(r"(?:\r?\n){2,}", content.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next(
            (index for index, line in enumerate(lines) if "-->" in line),
            None,
        )
        if timing_index is None:
            continue
        start_text, end_part = lines[timing_index].split("-->", maxsplit=1)
        end_text = end_part.strip().split(maxsplit=1)[0]
        start_ms = _parse_timestamp(start_text.strip())
        end_ms = _parse_timestamp(end_text)
        if end_ms < start_ms:
            raise SubtitleError("subtitle cue end precedes start")
        text = " ".join(lines[timing_index + 1 :])
        text = html.unescape(_TAGS.sub("", text))
        text = " ".join(text.split())
        if not text:
            continue
        segments.append(
            {
                "id": f"tr_{len(segments) + 1:04d}",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
                "confidence": None,
            }
        )
    if not segments:
        raise SubtitleError("subtitle contains no valid cues")
    return {
        "schema_version": "1.0",
        "provider": "yt-dlp",
        "model": "platform-subtitle",
        "language": None,
        "segments": segments,
    }


def _parse_timestamp(value: str) -> int:
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise SubtitleError(f"invalid subtitle timestamp: {value}")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    milliseconds = int(match.group(4))
    if minutes >= 60 or seconds >= 60:
        raise SubtitleError(f"invalid subtitle timestamp: {value}")
    return ((hours * 60 + minutes) * 60 + seconds) * 1_000 + milliseconds
