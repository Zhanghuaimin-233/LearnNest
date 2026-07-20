from __future__ import annotations

import hashlib
import io
import json
import wave
from collections.abc import Callable
from pathlib import Path

import pytest

from helpers.note_v3_fixtures import (
    concept_payload,
    practical_payload,
    resource_payload,
)
from learnnest.models import ContentPack, StageStatus, TaskRecord
from learnnest.note_models import GeneratedNote
from learnnest.note_validation import parse_generated_note
from learnnest.podcast_models import PodcastScript
from learnnest.rendering import (
    render_generated_note,
    render_podcast_script,
    render_podcast_speech,
)
from learnnest.task_store import load_task, write_task_atomic
from learnnest.validation import validate_task


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes(b"\x00\x00" * 1_600)
    return output.getvalue()


def _complete_task(tmp_path: Path) -> tuple[Path, Path]:
    output_root = tmp_path / "vault"
    task_dir = output_root / "视频学习素材" / "lesson--a1b2c3d4"
    note_dir = task_dir / "generated_notes" / "note-run"
    podcast_dir = task_dir / "generated_podcasts" / "podcast-run"
    audio_dir = task_dir / "generated_audio" / "audio-run"
    task_dir.mkdir(parents=True)

    content_pack = {
        "schema_version": "1.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "evidence": [
            {
                "id": "tr_0001",
                "kind": "transcript",
                "start_ms": 0,
                "end_ms": 1_000,
                "text": "打开设置。",
                "artifact_path": "transcript.json",
            }
        ],
    }
    _write_json(task_dir / "content_pack.json", content_pack)
    _write_json(task_dir / "transcript.json", {})

    note = GeneratedNote.model_validate(
        {
            "schema_version": "2.0",
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "title": "设置教程",
            "audience": {"text": "适合初学者。", "evidence_ids": ["tr_0001"]},
            "summary": {"text": "视频介绍了设置入口。", "evidence_ids": ["tr_0001"]},
            "key_points": [{"text": "先打开设置。", "evidence_ids": ["tr_0001"]}],
            "steps": [],
            "cautions": [],
            "ai_supplements": [],
        }
    )
    note_bytes = (note.model_dump_json(indent=2) + "\n").encode("utf-8")
    note_dir.mkdir(parents=True)
    (note_dir / "note.json").write_bytes(note_bytes)
    (note_dir / "note.md").write_text("# 设置教程\n", encoding="utf-8")

    script = PodcastScript.model_validate(
        {
            "schema_version": "1.0",
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "note_content_sha256": hashlib.sha256(note_bytes).hexdigest(),
            "title": "设置教程播客",
            "segments": [
                {
                    "order": 1,
                    "kind": "intro",
                    "text": "今天聊聊设置入口。",
                    "evidence_ids": ["tr_0001"],
                },
                {
                    "order": 2,
                    "kind": "body",
                    "text": "第一步是打开设置。",
                    "evidence_ids": ["tr_0001"],
                },
                {
                    "order": 3,
                    "kind": "outro",
                    "text": "这就是本期的重点。",
                    "evidence_ids": ["tr_0001"],
                },
            ],
            "ai_supplements": [],
        }
    )
    script_bytes = (script.model_dump_json(indent=2) + "\n").encode("utf-8")
    speech_bytes = render_podcast_speech(script).encode("utf-8")
    podcast_dir.mkdir(parents=True)
    (podcast_dir / "podcast_script.json").write_bytes(script_bytes)
    (podcast_dir / "podcast_script.md").write_bytes(
        render_podcast_script(script).encode("utf-8")
    )
    (podcast_dir / "speech.txt").write_bytes(speech_bytes)

    audio_dir.mkdir(parents=True)
    (audio_dir / "audio.wav").write_bytes(_wav_bytes())
    (audio_dir / "audio.mp3").write_bytes(b"valid-mp3-fixture")
    published = output_root / "视频学习音频" / "lesson--a1b2c3d4.mp3"
    published.parent.mkdir()
    published.write_bytes(b"valid-mp3-fixture")
    mp3_sha256 = hashlib.sha256(b"valid-mp3-fixture").hexdigest()
    _write_json(
        published.with_suffix(".learnnest.json"),
        {
            "schema_version": "1.0",
            "task_id": "20260711-a1b2c3d4",
            "mp3_sha256": mp3_sha256,
            "status": "completed",
        },
    )
    _write_json(
        audio_dir / "audio.json",
        {
            "schema_version": "1.0",
            "run_id": "audio-run",
            "provider": "fake",
            "model": "fake-tts",
            "voice": "test",
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "podcast_script_sha256": hashlib.sha256(script_bytes).hexdigest(),
            "speech_sha256": hashlib.sha256(speech_bytes).hexdigest(),
            "mp3_sha256": mp3_sha256,
            "published_path": "视频学习音频/lesson--a1b2c3d4.mp3",
            "status": "completed",
        },
    )

    task = TaskRecord(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        stages={
            "content_pack": StageStatus.COMPLETED,
            "note": StageStatus.COMPLETED,
            "podcast_script": StageStatus.COMPLETED,
            "tts": StageStatus.COMPLETED,
        },
        artifacts={
            "content_pack": ["content_pack.json"],
            "note": [
                "generated_notes/note-run/note.json",
                "generated_notes/note-run/note.md",
            ],
            "podcast_script": [
                "generated_podcasts/podcast-run/podcast_script.json",
                "generated_podcasts/podcast-run/podcast_script.md",
                "generated_podcasts/podcast-run/speech.txt",
            ],
            "tts": [
                "generated_audio/audio-run/audio.json",
                "generated_audio/audio-run/audio.wav",
                "generated_audio/audio-run/audio.mp3",
            ],
        },
    )
    (note_dir / "note.md").write_bytes(
        render_generated_note(
            task,
            ContentPack.model_validate(content_pack),
            note,
            asset_prefix="视频学习素材/lesson--a1b2c3d4",
        ).encode("utf-8")
    )
    write_task_atomic(task_dir, task)
    return task_dir, podcast_dir / "speech.txt"


def _v3_payload_for_workspace(
    payload_factory: Callable[[], dict[str, object]],
) -> dict[str, object]:
    payload = payload_factory()
    payload["task_id"] = "20260711-a1b2c3d4"
    payload["source_fingerprint"] = "a1b2c3d4"

    def bind_evidence(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"classification_evidence_ids", "evidence_ids"}:
                    value[key] = ["tr_0001"]
                else:
                    bind_evidence(child)
        elif isinstance(value, list):
            for child in value:
                bind_evidence(child)

    bind_evidence(payload)
    if payload.get("note_type") == "resource_share":
        resources = payload["resources"]
        assert isinstance(resources, list)
        for resource in resources:
            assert isinstance(resource, dict)
            resource["locator"] = None
    return payload


def _complete_v3_task(
    tmp_path: Path,
    payload_factory: Callable[[], dict[str, object]],
) -> Path:
    task_dir, _ = _complete_task(tmp_path)
    note_payload = _v3_payload_for_workspace(payload_factory)
    note_bytes = (
        json.dumps(note_payload, ensure_ascii=False, separators=(",", ":")) + " \n"
    ).encode("utf-8")
    note_dir = task_dir / "generated_notes" / "note-run"
    (note_dir / "note.json").write_bytes(note_bytes)

    script_dir = task_dir / "generated_podcasts" / "podcast-run"
    script_path = script_dir / "podcast_script.json"
    script_payload = json.loads(script_path.read_text(encoding="utf-8"))
    script_payload["note_content_sha256"] = hashlib.sha256(note_bytes).hexdigest()
    script = PodcastScript.model_validate(script_payload)
    script_bytes = (script.model_dump_json(indent=2) + "\n").encode("utf-8")
    speech_bytes = render_podcast_speech(script).encode("utf-8")
    script_path.write_bytes(script_bytes)
    (script_dir / "podcast_script.md").write_bytes(
        render_podcast_script(script).encode("utf-8")
    )
    (script_dir / "speech.txt").write_bytes(speech_bytes)

    audio_json_path = task_dir / "generated_audio" / "audio-run" / "audio.json"
    audio_json = json.loads(audio_json_path.read_text(encoding="utf-8"))
    audio_json["podcast_script_sha256"] = hashlib.sha256(script_bytes).hexdigest()
    audio_json["speech_sha256"] = hashlib.sha256(speech_bytes).hexdigest()
    _write_json(audio_json_path, audio_json)

    task = load_task(task_dir)
    content_pack = ContentPack.model_validate_json(
        (task_dir / "content_pack.json").read_bytes()
    )
    note = parse_generated_note(note_bytes.decode("utf-8"))
    (note_dir / "note.md").write_bytes(
        render_generated_note(
            task,
            content_pack,
            note,
            asset_prefix="视频学习素材/lesson--a1b2c3d4",
        ).encode("utf-8")
    )
    return task_dir


def _valid_probe(path: Path) -> dict[str, object]:
    codec = "mp3" if path.suffix == ".mp3" else "pcm_s16le"
    return {
        "streams": [{"codec_type": "audio", "codec_name": codec}],
        "format": {"duration": "0.1"},
    }


def test_validate_task_accepts_consistent_podcast_and_tts_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    task_dir, _ = _complete_task(tmp_path)
    monkeypatch.setattr("learnnest.validation.probe_audio", _valid_probe)

    assert validate_task(task_dir) == []


def test_validate_task_accepts_a_legacy_audio_ownership_marker(
    tmp_path: Path, monkeypatch
) -> None:
    task_dir, _ = _complete_task(tmp_path)
    published = task_dir.parents[1] / "视频学习音频" / "lesson--a1b2c3d4.mp3"
    published.with_suffix(".learnnest.json").rename(
        published.with_suffix(".learnpipe.json")
    )
    monkeypatch.setattr("learnnest.validation.probe_audio", _valid_probe)

    assert validate_task(task_dir) == []


def test_validate_task_rejects_conflicting_audio_ownership_markers(
    tmp_path: Path, monkeypatch
) -> None:
    task_dir, _ = _complete_task(tmp_path)
    published = task_dir.parents[1] / "视频学习音频" / "lesson--a1b2c3d4.mp3"
    legacy = json.loads(
        published.with_suffix(".learnnest.json").read_text(encoding="utf-8")
    )
    legacy["status"] = "pending"
    _write_json(published.with_suffix(".learnpipe.json"), legacy)
    monkeypatch.setattr("learnnest.validation.probe_audio", _valid_probe)

    assert "audio destination ownership markers conflict" in validate_task(task_dir)[0]


@pytest.mark.parametrize(
    "payload_factory",
    [concept_payload, resource_payload, practical_payload],
    ids=["concept", "resource", "practical"],
)
def test_validate_task_accepts_each_v3_note_with_podcast_and_tts_artifacts(
    payload_factory: Callable[[], dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_dir = _complete_v3_task(tmp_path, payload_factory)
    monkeypatch.setattr("learnnest.validation.probe_audio", _valid_probe)

    assert validate_task(task_dir) == []


def test_validate_task_rejects_podcast_speech_tampering(
    tmp_path: Path, monkeypatch
) -> None:
    task_dir, speech_path = _complete_task(tmp_path)
    speech_path.write_text("被篡改的播客口播。\n", encoding="utf-8")
    monkeypatch.setattr("learnnest.validation.probe_audio", _valid_probe)

    errors = validate_task(task_dir)

    assert "speech.txt does not match podcast_script.json" in errors
    assert "audio.json speech_sha256 does not match active speech" in errors


def test_validate_task_rejects_podcast_markdown_tampering(
    tmp_path: Path, monkeypatch
) -> None:
    task_dir, _ = _complete_task(tmp_path)
    script_markdown = (
        task_dir / "generated_podcasts" / "podcast-run" / "podcast_script.md"
    )
    script_markdown.write_text("# 被篡改\n", encoding="utf-8")
    monkeypatch.setattr("learnnest.validation.probe_audio", _valid_probe)

    assert "podcast_script.md does not match podcast_script.json" in validate_task(
        task_dir
    )


def test_validate_task_rejects_published_audio_tampering(
    tmp_path: Path, monkeypatch
) -> None:
    task_dir, _ = _complete_task(tmp_path)
    published = task_dir.parents[1] / "视频学习音频" / "lesson--a1b2c3d4.mp3"
    published.write_bytes(b"tampered")
    monkeypatch.setattr("learnnest.validation.probe_audio", _valid_probe)

    errors = validate_task(task_dir)

    assert "published audio does not match active audio bundle" in errors


def test_validate_task_rejects_missing_derived_note_links(
    tmp_path: Path, monkeypatch
) -> None:
    task_dir, _ = _complete_task(tmp_path)
    note_path = task_dir / "generated_notes" / "note-run" / "note.md"
    markdown = note_path.read_text(encoding="utf-8")
    note_path.write_text(
        markdown.replace(
            "- [[视频学习音频/lesson--a1b2c3d4.mp3|音频]]\n",
            "",
        ),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr("learnnest.validation.probe_audio", _valid_probe)

    assert "note Markdown is missing active audio link" in validate_task(task_dir)
