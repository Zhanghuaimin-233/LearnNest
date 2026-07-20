"""Conservative Windows path checks for provider-created artifact bundles."""

from __future__ import annotations

from pathlib import Path

from learnnest.util import safe_title


WINDOWS_PATH_BUDGET = 240
_SAMPLE_RUN_ID = "20260720T123456123456Z-12345678"


class ArtifactPathBudgetError(ValueError):
    """A provider stage cannot safely create all of its artifact paths."""


def assert_stage_path_budget(
    task_dir: Path,
    output_root: Path | None,
    *,
    stage: str,
    task_title: str,
    task_id: str,
) -> None:
    """Reject an unsafe provider path before the provider receives a request."""
    task_root = task_dir.resolve()
    candidates = _stage_candidates(task_root, output_root, stage, task_title, task_id)
    longest = max(candidates, key=lambda path: len(str(path)))
    if len(str(longest)) > WINDOWS_PATH_BUDGET:
        raise ArtifactPathBudgetError(
            f"{stage} output path exceeds the Windows safety budget "
            f"({len(str(longest))}>{WINDOWS_PATH_BUDGET}): {longest}"
        )


def _stage_candidates(
    task_dir: Path,
    output_root: Path | None,
    stage: str,
    task_title: str,
    task_id: str,
) -> list[Path]:
    bundles = {
        "note": ("generated_notes", "generation.json"),
        "podcast_script": ("generated_podcasts", "podcast_script.json"),
        "tts": ("generated_audio", "generation.json"),
    }
    try:
        directory, filename = bundles[stage]
    except KeyError as error:
        raise ValueError(
            f"unsupported provider stage for path budget: {stage}"
        ) from error
    temporary = task_dir / directory / f".{_SAMPLE_RUN_ID}" / filename
    candidates = [temporary]
    if output_root is not None:
        suffix = task_id[-8:]
        name = f"{safe_title(task_title)}--{suffix}"
        delivery_directory = "视频学习笔记" if stage == "note" else "视频学习音频"
        extension = ".md" if stage == "note" else ".mp3"
        candidates.append(
            output_root.resolve() / delivery_directory / f"{name}{extension}"
        )
    return candidates
