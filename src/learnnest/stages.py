"""Single source of truth for pipeline stage order and artifact contracts."""

from __future__ import annotations

from learnnest.models import StageName

STAGES: tuple[StageName, ...] = (
    "source",
    "transcript",
    "frames",
    "ocr",
    "evidence",
    "content_pack",
    "note",
    "publish",
    "podcast_script",
    "tts",
)

PAID_STAGES: frozenset[StageName] = frozenset({"note", "podcast_script", "tts"})

_STAGE_ARTIFACTS: dict[StageName, tuple[str, ...]] = {
    "source": ("source.json", "process_report.md"),
    # transcript/OCR/evidence are compacted into content_pack.json after a
    # successful deterministic run.  Their completed stage records therefore
    # intentionally have no persistent standalone artifacts.
    "transcript": (),
    "frames": ("frames", "frames.json"),
    "ocr": (),
    "evidence": (),
    "content_pack": ("content_pack.json", "trace.md"),
    "note": ("note.json", "note.md"),
    "publish": ("note.md",),
    "podcast_script": (
        "podcast_script.json",
        "speech.txt",
    ),
    "tts": ("audio.json", "audio.wav"),
}


def stage_artifacts(stage: StageName) -> tuple[str, ...]:
    try:
        return _STAGE_ARTIFACTS[stage]
    except KeyError as error:
        raise ValueError(f"unknown stage: {stage}") from error


def stage_index(stage: StageName) -> int:
    try:
        return STAGES.index(stage)
    except ValueError as error:
        raise ValueError(f"unknown stage: {stage}") from error


def downstream_stages(stage: StageName) -> tuple[StageName, ...]:
    return STAGES[stage_index(stage) :]
