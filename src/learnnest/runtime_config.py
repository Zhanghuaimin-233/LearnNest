"""Runtime-only configuration loading without persisting credentials."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


_RUNTIME_KEYS = frozenset(
    {
        "DOUYIN_COOKIE",
        "MIMO_API_KEY",
        "LEARNNEST_NOTE_API_KEY",
        "LEARNNEST_NOTE_BASE_URL",
        "LEARNNEST_NOTE_MODEL",
        "LEARNNEST_NOTE_PROVIDER",
        "LEARNNEST_NOTE_JSON_MODE",
        "LEARNNEST_NOTE_WRITER_STRATEGY",
        "LEARNNEST_NOTE_SAFE_INPUT_TOKENS",
        "DEEPSEEK_API_KEY",
        "HUGGINGFACE_HUB_CACHE",
    }
)


def load_runtime_environment(
    working_directory: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return whitelisted runtime values, with process environment precedence.

    The local ``.env`` is an operator convenience only. Its values remain in
    memory and are never attached to task, schedule, batch, or log facts.
    A single raw line remains supported as a legacy Douyin cookie file.
    """
    process = os.environ if environ is None else environ
    file_values = _load_local_env(Path(working_directory) / ".env")
    values: dict[str, str] = {}
    for key in _RUNTIME_KEYS:
        value = process.get(key, "").strip()
        if not value:
            value = file_values.get(key, "").strip()
        if value:
            values[key] = value
    return values


def _load_local_env(path: Path) -> dict[str, str]:
    try:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except (OSError, UnicodeError):
        return {}
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if separator and key in _RUNTIME_KEYS:
            values[key] = _unquote(value.strip())
    if len(lines) == 1 and not values:
        values["DOUYIN_COOKIE"] = lines[0]
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
