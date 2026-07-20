from __future__ import annotations

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from learnnest.cli import app
from learnnest.models import ContentPack, Evidence, TaskRecord, TranscriptSegment


def test_cli_help_is_available_before_commands_are_registered() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0


def test_task_and_transcript_models_preserve_stable_source_metadata() -> None:
    task = TaskRecord(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        provider="faster-whisper",
        model="large-v3",
    )
    segment = TranscriptSegment(
        id="tr_0001",
        start_ms=0,
        end_ms=1_000,
        text="打开设置。",
        confidence=0.94,
    )

    assert task.task_id == "20260711-a1b2c3d4"
    assert task.model_dump()["provider"] == "faster-whisper"
    assert task.model_dump()["model"] == "large-v3"
    assert segment.model_dump()["start_ms"] == 0


def test_content_pack_rejects_duplicate_evidence_ids() -> None:
    evidence = Evidence(
        id="tr_0001",
        kind="transcript",
        start_ms=0,
        end_ms=1_000,
        text="打开设置。",
        artifact_path="transcript.json",
    )

    with pytest.raises(ValidationError, match="unique"):
        ContentPack(
            task_id="20260711-a1b2c3d4",
            source_fingerprint="a1b2c3d4",
            evidence=[evidence, evidence],
        )


@pytest.mark.parametrize(
    "evidence_id",
    ["tr_001", "audio_0001", "tr_bad", "custom-id"],
)
def test_evidence_rejects_ids_outside_the_stable_format(evidence_id: str) -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        Evidence(
            id=evidence_id,
            kind="transcript",
            start_ms=0,
            end_ms=1_000,
            text="打开设置。",
            artifact_path="transcript.json",
        )


@pytest.mark.parametrize(
    ("evidence_id", "kind", "source_fields"),
    [
        ("tr_0001", "frame", {"start_ms": 0}),
        ("fr_0001", "ocr", {"frame_id": "fr_0001", "text": "设置"}),
        ("ocr_0001", "ai_supplement", {}),
        (
            "ai_0001",
            "transcript",
            {"start_ms": 0, "end_ms": 1_000, "text": "打开设置。"},
        ),
    ],
)
def test_evidence_rejects_id_prefix_that_does_not_match_kind(
    evidence_id: str,
    kind: str,
    source_fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="evidence id prefix must match kind"):
        Evidence(
            id=evidence_id,
            kind=kind,  # type: ignore[arg-type]
            artifact_path="evidence.json",
            **source_fields,
        )


@pytest.mark.parametrize(
    "data",
    [
        {
            "id": "tr_0001",
            "kind": "transcript",
            "start_ms": 0,
            "end_ms": 1_000,
            "text": "打开设置。",
            "artifact_path": "transcript.json",
        },
        {
            "id": "fr_10000",
            "kind": "frame",
            "start_ms": 1_000,
            "artifact_path": "frames/selected/fr_10000.png",
        },
        {
            "id": "ocr_0001",
            "kind": "ocr",
            "text": "设置",
            "artifact_path": "ocr.json",
            "frame_id": "fr_0001",
        },
        {
            "id": "ai_0001",
            "kind": "ai_supplement",
            "artifact_path": "ai.json",
        },
    ],
)
def test_evidence_accepts_canonical_id_prefix_and_kind_pairs(
    data: dict[str, object],
) -> None:
    evidence = Evidence(**data)

    assert evidence.id == data["id"]
    assert evidence.kind == data["kind"]


def test_content_pack_rejects_evidence_reference_that_is_not_present() -> None:
    frame = Evidence(
        id="fr_0001",
        kind="frame",
        start_ms=1_000,
        artifact_path="frames/selected/fr_0001.png",
        related_evidence_ids=["ocr_0001"],
    )

    with pytest.raises(ValidationError, match="unknown evidence id"):
        ContentPack(
            task_id="20260711-a1b2c3d4",
            source_fingerprint="a1b2c3d4",
            evidence=[frame],
        )


def test_content_pack_rejects_ocr_reference_to_non_frame_evidence() -> None:
    transcript = Evidence(
        id="tr_0001",
        kind="transcript",
        start_ms=0,
        end_ms=1_000,
        text="打开设置。",
        artifact_path="transcript.json",
    )
    ocr = Evidence(
        id="ocr_0001",
        kind="ocr",
        text="设置",
        artifact_path="frames/selected/fr_0001.png",
        frame_id="tr_0001",
    )

    with pytest.raises(ValidationError, match="frame evidence"):
        ContentPack(
            task_id="20260711-a1b2c3d4",
            source_fingerprint="a1b2c3d4",
            evidence=[transcript, ocr],
        )


def test_content_pack_rejects_frame_reference_to_non_ocr_evidence() -> None:
    transcript = Evidence(
        id="tr_0001",
        kind="transcript",
        start_ms=0,
        end_ms=1_000,
        text="打开设置。",
        artifact_path="transcript.json",
    )
    frame = Evidence(
        id="fr_0001",
        kind="frame",
        start_ms=1_000,
        artifact_path="frames/selected/fr_0001.png",
        related_evidence_ids=["tr_0001"],
    )

    with pytest.raises(ValidationError, match="ocr evidence"):
        ContentPack(
            task_id="20260711-a1b2c3d4",
            source_fingerprint="a1b2c3d4",
            evidence=[transcript, frame],
        )


def test_task_record_rejects_unknown_stage_names() -> None:
    required = {
        "task_id": "20260711-a1b2c3d4",
        "source_path": "C:/videos/lesson.mp4",
        "source_fingerprint": "a1b2c3d4",
        "title": "lesson",
    }

    with pytest.raises(ValidationError):
        TaskRecord(**required, stages={"download": "completed"})
    with pytest.raises(ValidationError):
        TaskRecord(**required, artifacts={"download": ["download.mp4"]})
    with pytest.raises(ValidationError, match="artifact paths must not be empty"):
        TaskRecord(**required, artifacts={"source": []})


def test_task_record_accepts_multiple_artifacts_for_one_stage() -> None:
    task = TaskRecord(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        artifacts={"transcript": ["transcript.json", "transcript.md"]},
    )

    assert task.artifacts["transcript"] == ["transcript.json", "transcript.md"]


def test_task_record_accepts_a_canonical_note_type_override() -> None:
    task = TaskRecord(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        note_type_override="resource_share",
    )

    assert task.note_type_override == "resource_share"


def test_task_record_note_type_override_defaults_to_none() -> None:
    task = TaskRecord(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
    )

    assert task.note_type_override is None


def test_task_record_defaults_a_legacy_task_without_profile_to_evidence() -> None:
    legacy_task = TaskRecord.model_validate(
        {
            "task_id": "20260711-a1b2c3d4",
            "source_path": "C:/videos/lesson.mp4",
            "source_fingerprint": "a1b2c3d4",
            "title": "lesson",
        }
    )

    assert legacy_task.profile == "evidence"
    assert legacy_task.source_type == "local_file"
    assert legacy_task.source_input == "C:/videos/lesson.mp4"
    assert legacy_task.media_path == "C:/videos/lesson.mp4"


def test_task_record_allows_url_identity_before_media_is_acquired() -> None:
    task = TaskRecord(
        task_id="20260711-a1b2c3d4",
        source_path="https://example.com/watch?v=1",
        source_input="https://example.com/watch?v=1",
        source_type="url",
        media_path=None,
        source_fingerprint="a1b2c3d4",
        title="url-a1b2c3d4",
    )

    assert task.source_type == "url"
    assert task.media_path is None
