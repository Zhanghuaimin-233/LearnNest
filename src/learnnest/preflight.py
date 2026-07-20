"""Side-effect-free source planning for v0.4 dry-run."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from learnnest.source_models import SourceItem
from learnnest.sources import source_item_fingerprint
from learnnest.util import safe_title, task_directory_name


class PreflightIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["warning", "error"]
    code: str
    message: str
    source_index: int | None = None


class PreflightItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceItem
    fingerprint: str
    task_id: str
    planned_directory: str
    duplicate: bool = False


class PreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PreflightItem]
    issues: list[PreflightIssue]

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


def preflight_sources(
    sources: Sequence[SourceItem],
    output_root: Path,
    *,
    command_finder: Callable[[str], str | None] = shutil.which,
    writable_checker: Callable[[Path], bool] | None = None,
    today: date | None = None,
) -> PreflightResult:
    """Plan task identities and prerequisites without filesystem mutations."""
    root = output_root.expanduser().resolve()
    issues: list[PreflightIssue] = []
    if not root.exists():
        issues.append(
            PreflightIssue(
                severity="error",
                code="output_root_missing",
                message=f"output root does not exist: {root}",
            )
        )
    elif not root.is_dir():
        issues.append(
            PreflightIssue(
                severity="error",
                code="output_root_not_directory",
                message=f"output root is not a directory: {root}",
            )
        )
    else:
        checker = writable_checker or (lambda path: os.access(path, os.W_OK))
        if not checker(root):
            issues.append(
                PreflightIssue(
                    severity="error",
                    code="output_root_not_writable",
                    message=f"output root is not writable: {root}",
                )
            )

    commands = ["ffmpeg", "ffprobe"]
    if any(source.input_type == "url" for source in sources):
        commands.append("yt-dlp")
    for command in commands:
        if command_finder(command) is None:
            issues.append(
                PreflightIssue(
                    severity="error",
                    code=f"command_missing_{command.replace('-', '_')}",
                    message=f"required command is unavailable: {command}",
                )
            )

    selected_date = today or date.today()
    seen: set[str] = set()
    items: list[PreflightItem] = []
    for index, source in enumerate(sources):
        fingerprint = source_item_fingerprint(source)
        duplicate = fingerprint in seen
        seen.add(fingerprint)
        task_id = f"{selected_date:%Y%m%d}-{fingerprint[:8]}"
        title = _planned_title(source, fingerprint)
        directory = task_directory_name(title, task_id)
        items.append(
            PreflightItem(
                source=source,
                fingerprint=fingerprint,
                task_id=task_id,
                planned_directory=directory,
                duplicate=duplicate,
            )
        )
        if duplicate:
            issues.append(
                PreflightIssue(
                    severity="warning",
                    code="duplicate_in_batch",
                    message=f"source duplicates an earlier batch item: {source.input}",
                    source_index=index,
                )
            )
        planned_path = root / "视频学习素材" / directory
        if root.exists() and planned_path.exists():
            issues.append(
                PreflightIssue(
                    severity="warning",
                    code="planned_directory_exists",
                    message=f"planned task directory already exists: {planned_path}",
                    source_index=index,
                )
            )
    return PreflightResult(items=items, issues=issues)


def preflight_runtime(
    output_root: Path,
    *,
    require_douyin: bool = False,
    require_note: bool = False,
    require_podcast: bool = False,
    require_tts: bool = False,
    runtime_environ: Mapping[str, str],
    command_finder: Callable[[str], str | None] = shutil.which,
    writable_checker: Callable[[Path], bool] | None = None,
) -> PreflightResult:
    """Check the explicit workflow prerequisites without exposing secrets.

    Model installation is intentionally outside this checker. Provider model
    availability is verified separately by ``learnnest doctor`` in isolated
    workers, so this function never starts a download or a paid request.
    """
    base = preflight_sources(
        [],
        output_root,
        command_finder=command_finder,
        writable_checker=writable_checker,
    )
    issues = list(base.issues)
    if require_douyin:
        if command_finder("yt-dlp") is None:
            issues.append(
                PreflightIssue(
                    severity="error",
                    code="command_missing_yt_dlp",
                    message="required command is unavailable: yt-dlp",
                )
            )
        if not _runtime_value(runtime_environ, "DOUYIN_COOKIE"):
            issues.append(
                PreflightIssue(
                    severity="error",
                    code="runtime_missing_douyin_cookie",
                    message="DOUYIN_COOKIE is required for the Douyin monitor",
                )
            )

    mimo_configured = bool(_runtime_value(runtime_environ, "MIMO_API_KEY"))
    generic_note_keys = (
        "LEARNNEST_NOTE_API_KEY",
        "LEARNNEST_NOTE_BASE_URL",
        "LEARNNEST_NOTE_MODEL",
    )
    generic_note_values = {
        key: _runtime_value(runtime_environ, key) for key in generic_note_keys
    }
    generic_note_present = any(generic_note_values.values())
    generic_note_complete = all(generic_note_values.values())
    if require_note:
        if generic_note_present and not generic_note_complete:
            issues.append(
                PreflightIssue(
                    severity="error",
                    code="runtime_incomplete_note_provider",
                    message=(
                        "LEARNNEST_NOTE_API_KEY, LEARNNEST_NOTE_BASE_URL, and "
                        "LEARNNEST_NOTE_MODEL must be set together"
                    ),
                )
            )
        elif generic_note_complete and mimo_configured:
            issues.append(
                PreflightIssue(
                    severity="error",
                    code="runtime_conflicting_note_provider",
                    message=(
                        "generic note provider configuration cannot be combined "
                        "with MIMO_API_KEY"
                    ),
                )
            )
        elif not generic_note_complete and not mimo_configured:
            issues.append(
                PreflightIssue(
                    severity="error",
                    code="runtime_missing_note_provider",
                    message="a complete note provider configuration is required",
                )
            )
    if (require_podcast or require_tts) and not mimo_configured:
        issues.append(
            PreflightIssue(
                severity="error",
                code="runtime_missing_mimo_api_key",
                message="MIMO_API_KEY is required for podcast and TTS generation",
            )
        )
    return PreflightResult(items=base.items, issues=issues)


def _planned_title(source: SourceItem, fingerprint: str) -> str:
    if source.title is not None:
        return safe_title(source.title)
    if source.input_type == "local_file":
        return safe_title(Path(source.input).stem)
    return f"url-{fingerprint[:8]}"


def _runtime_value(environ: Mapping[str, str], key: str) -> str:
    return environ.get(key, "").strip()
