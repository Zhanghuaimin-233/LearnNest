"""Pure helpers for stable task identities and human-facing names."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")
_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_TASK_DIRECTORY_TITLE_LIMIT = 40


def format_timestamp(milliseconds: int) -> str:
    """Render a non-negative millisecond offset as ``HH:MM:SS``."""
    if not isinstance(milliseconds, int) or isinstance(milliseconds, bool):
        raise TypeError("milliseconds must be an integer")
    if milliseconds < 0:
        raise ValueError("milliseconds must be non-negative")

    total_seconds = milliseconds // 1_000
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def safe_title(name: str) -> str:
    """Create a non-empty title that is safe as a single Windows path segment."""
    cleaned = _INVALID_FILENAME_CHARS.sub(" ", name)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(". ")
    if not cleaned:
        return "untitled"
    stem, separator, suffix = cleaned.partition(".")
    if stem.upper() in _RESERVED_FILENAMES:
        return f"{stem}_{separator}{suffix}"
    return cleaned


def task_directory_name(title: str, task_id: str) -> str:
    """Return a stable, bounded task-directory segment for deep artifact trees."""
    shortened = safe_title(title)[:_TASK_DIRECTORY_TITLE_LIMIT].rstrip(". ")
    return f"{shortened or 'untitled'}--{task_id[-8:]}"


def source_fingerprint(path: str | Path) -> str:
    """Hash the normalized absolute path, size and nanosecond modification time."""
    source = Path(path).expanduser().resolve(strict=True)
    stat = source.stat()
    normalized_path = os.path.normcase(str(source))
    material = f"{normalized_path}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()
    return hashlib.sha256(material).hexdigest()


def url_source_fingerprint(normalized_url: str) -> str:
    """Hash one already-normalized URL without changing its query semantics."""
    if not normalized_url:
        raise ValueError("normalized URL must not be empty")
    return hashlib.sha256(f"url\0{normalized_url}".encode("utf-8")).hexdigest()
