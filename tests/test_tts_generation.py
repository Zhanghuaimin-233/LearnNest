from __future__ import annotations

import hashlib
import io
import json
import wave
from collections.abc import Callable
from pathlib import Path

import pytest

from helpers.note_v3_fixtures import concept_payload
from learnnest.models import StageStatus, TaskRecord
from learnnest.podcast_models import PodcastScript
from learnnest.rendering import render_podcast_speech
from learnnest.task_store import load_task, write_task_atomic


class FakeTtsProvider:
    name = "fake-tts"
    model = "fake-wav"
    voice = "测试音色"

    def __init__(self, wav_bytes: bytes | None = None, error: Exception | None = None):
        self.wav_bytes = wav_bytes
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def synthesize(self, speech: str, style: str) -> bytes:
        self.calls.append((speech, style))
        if self.error is not None:
            raise self.error
        assert self.wav_bytes is not None
        return self.wav_bytes


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24_000)
        stream.writeframes(b"\x00\x00" * 2_400)
    return output.getvalue()


def workspace(tmp_path: Path) -> Path:
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    podcast = task_dir / "generated_podcasts" / "podcast-run"
    podcast.mkdir(parents=True)
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
            },
            {
                "id": "tr_0002",
                "kind": "transcript",
                "start_ms": 1_000,
                "end_ms": 2_000,
                "text": "保存配置。",
                "artifact_path": "transcript.json",
            },
        ],
    }
    (task_dir / "content_pack.json").write_text(
        json.dumps(content_pack, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (task_dir / "transcript.json").write_text("{}\n", encoding="utf-8")
    note_dir = task_dir / "generated_notes" / "note-run"
    note_dir.mkdir(parents=True)
    note = {
        "schema_version": "2.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "title": "模型设置",
        "audience": {"text": "初学者", "evidence_ids": ["tr_0001"]},
        "summary": {"text": "打开并保存设置", "evidence_ids": ["tr_0001"]},
        "key_points": [{"text": "保存配置", "evidence_ids": ["tr_0002"]}],
        "steps": [],
        "cautions": [],
        "ai_supplements": [],
    }
    note_bytes = (json.dumps(note, ensure_ascii=False, indent=2) + "\n").encode()
    (note_dir / "note.json").write_bytes(note_bytes)
    (note_dir / "note.md").write_text("# 模型设置\n", encoding="utf-8")
    script = {
        "schema_version": "1.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "note_content_sha256": hashlib.sha256(note_bytes).hexdigest(),
        "title": "模型设置复习",
        "segments": [
            {
                "order": 1,
                "kind": "intro",
                "text": "今天开始复习。",
                "evidence_ids": ["tr_0001"],
            },
            {
                "order": 2,
                "kind": "body",
                "text": "打开设置并确认。",
                "evidence_ids": ["tr_0001"],
            },
            {
                "order": 3,
                "kind": "outro",
                "text": "最后记得保存。",
                "evidence_ids": ["tr_0002"],
            },
        ],
        "ai_supplements": [],
    }
    (podcast / "podcast_script.json").write_text(
        json.dumps(script, ensure_ascii=False), encoding="utf-8"
    )
    (podcast / "podcast_script.md").write_text("# 播客\n", encoding="utf-8")
    (podcast / "speech.txt").write_bytes(
        render_podcast_speech(PodcastScript.model_validate(script)).encode("utf-8")
    )
    write_task_atomic(
        task_dir,
        TaskRecord(
            task_id="20260711-a1b2c3d4",
            source_path="C:/videos/lesson.mp4",
            source_fingerprint="a1b2c3d4",
            title="lesson",
            profile="full",
            stages={
                "content_pack": "completed",
                "note": "completed",
                "publish": "completed",
                "podcast_script": "completed",
                "tts": "pending",
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
            },
            providers={
                "note": "fake-note",
                "podcast_script": "fake-podcast",
            },
            models={"note": "queued", "podcast_script": "queued"},
        ),
    )
    return task_dir


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
    return payload


def _write_v3_note_and_bound_podcast(task_dir: Path) -> bytes:
    note_payload = _v3_payload_for_workspace(concept_payload)
    note_bytes = (
        json.dumps(note_payload, ensure_ascii=False, separators=(",", ":")) + " \n"
    ).encode("utf-8")
    note_path = task_dir / "generated_notes" / "note-run" / "note.json"
    note_path.write_bytes(note_bytes)

    script_path = (
        task_dir / "generated_podcasts" / "podcast-run" / "podcast_script.json"
    )
    script_payload = json.loads(script_path.read_text(encoding="utf-8"))
    script_payload["note_content_sha256"] = hashlib.sha256(note_bytes).hexdigest()
    script_path.write_text(
        json.dumps(script_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return note_bytes


def _write_v4_note_and_bound_podcast(task_dir: Path) -> bytes:
    from learnnest.note_templates import (
        builtin_template,
        template_snapshot_json,
        template_snapshot_sha256,
    )

    template = builtin_template("concept-explanation")
    note_payload = {
        "schema_version": "4.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "template_id": template.template_id,
        "template_sha256": template_snapshot_sha256(template),
        "title": {"text": "模板笔记", "evidence_ids": ["tr_0001"]},
        "blocks": [
            {
                "block_id": "core",
                "semantic_block": "core_facts",
                "items": [
                    {
                        "content": {
                            "text": "打开设置。",
                            "evidence_ids": ["tr_0001"],
                        }
                    }
                ],
            },
            {
                "block_id": "concepts",
                "semantic_block": "concept_cards",
                "items": [
                    {
                        "title": {
                            "text": "设置",
                            "evidence_ids": ["tr_0001"],
                        },
                        "content": {
                            "text": "保存配置。",
                            "evidence_ids": ["tr_0002"],
                        },
                    }
                ],
            },
            {
                "block_id": "review",
                "semantic_block": "review_questions",
                "items": [],
            },
        ],
        "ai_supplements": [],
    }
    note_dir = task_dir / "generated_notes" / "note-run"
    note_bytes = json.dumps(note_payload, ensure_ascii=False).encode("utf-8")
    (note_dir / "note.json").write_bytes(note_bytes)
    (note_dir / "template.json").write_text(
        template_snapshot_json(template), encoding="utf-8"
    )
    script_path = (
        task_dir / "generated_podcasts" / "podcast-run" / "podcast_script.json"
    )
    script_payload = json.loads(script_path.read_text(encoding="utf-8"))
    script_payload["note_content_sha256"] = hashlib.sha256(note_bytes).hexdigest()
    script_path.write_text(
        json.dumps(script_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return note_bytes


def test_tts_preflight_accepts_v3_note_without_changing_bound_sha(
    tmp_path: Path,
) -> None:
    import learnnest.tts_generation as tts_module

    task_dir = workspace(tmp_path)
    note_bytes = _write_v3_note_and_bound_podcast(task_dir)
    script_path = (
        task_dir / "generated_podcasts" / "podcast-run" / "podcast_script.json"
    )

    context = tts_module._load_context(task_dir)
    active_script = PodcastScript.model_validate_json(script_path.read_bytes())

    assert context.speech
    assert active_script.note_content_sha256 == hashlib.sha256(note_bytes).hexdigest()


def test_tts_preflight_rejects_tampered_v3_active_note_bytes(
    tmp_path: Path,
) -> None:
    import learnnest.tts_generation as tts_module

    task_dir = workspace(tmp_path)
    note_bytes = _write_v3_note_and_bound_podcast(task_dir)
    note_path = task_dir / "generated_notes" / "note-run" / "note.json"
    note_path.write_bytes(note_bytes + b"\n")

    with pytest.raises(ValueError, match="active podcast is invalid"):
        tts_module._load_context(task_dir)


def test_tts_preflight_rejects_v4_note_with_tampered_template_snapshot(
    tmp_path: Path,
) -> None:
    import learnnest.tts_generation as tts_module

    task_dir = workspace(tmp_path)
    _write_v4_note_and_bound_podcast(task_dir)
    snapshot_path = task_dir / "generated_notes" / "note-run" / "template.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["sections"][0]["heading"] = "被篡改的标题"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="active note is invalid"):
        tts_module._load_context(task_dir)


def fake_audio_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    import learnnest.tts_generation as generation

    def convert(source: Path, destination: Path) -> None:
        destination.write_bytes(b"ID3fake-mp3")

    def probe(path: Path) -> dict[str, object]:
        codec = "mp3" if path.suffix == ".mp3" else "pcm_s16le"
        return {
            "format": {"duration": "0.1"},
            "streams": [{"codec_type": "audio", "codec_name": codec}],
        }

    monkeypatch.setattr(generation, "convert_wav_to_mp3", convert)
    monkeypatch.setattr(generation, "probe_audio", probe)


def test_generate_tts_activates_bundle_and_publishes_mp3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from learnnest.tts_generation import generate_and_activate_tts

    task_dir = workspace(tmp_path)
    fake_audio_tools(monkeypatch)
    provider = FakeTtsProvider(wav_bytes())

    task = generate_and_activate_tts(task_dir, provider, tmp_path)

    assert len(provider.calls) == 1
    assert "今天开始复习" in provider.calls[0][0]
    assert task.stages["note"] is StageStatus.COMPLETED
    assert task.stages["publish"] is StageStatus.COMPLETED
    assert task.stages["podcast_script"] is StageStatus.COMPLETED
    assert task.stages["tts"] is StageStatus.COMPLETED
    assert task.providers["tts"] == "fake-tts"
    assert task.active_attempt_id is None
    assert task.attempts[-1].from_stage == "tts"
    assert task.attempts[-1].status == "completed"
    bundle = task_dir / Path(task.artifacts["tts"][0]).parent
    assert (bundle / "audio.wav").is_file()
    assert not (bundle / "audio.mp3").exists()
    published = tmp_path / "视频学习音频" / "lesson--a1b2c3d4.mp3"
    assert published.read_bytes() == b"ID3fake-mp3"
    marker = json.loads(
        published.with_suffix(".learnnest.json").read_text(encoding="utf-8")
    )
    assert marker["status"] == "completed"
    assert marker["task_id"] == task.task_id
    assert marker["mp3_sha256"] == hashlib.sha256(b"ID3fake-mp3").hexdigest()
    metadata = json.loads((bundle / "audio.json").read_text(encoding="utf-8"))
    assert metadata["mp3_sha256"] == marker["mp3_sha256"]
    note_markdown = (tmp_path / "视频学习笔记" / "lesson.md").read_text(
        encoding="utf-8"
    )
    assert "podcast_script.md|播客稿" in note_markdown
    assert "[[视频学习音频/lesson--a1b2c3d4.mp3|音频]]" in note_markdown
    assert load_task(task_dir) == task


def test_tts_provider_failure_marks_only_tts_failed(
    tmp_path: Path,
) -> None:
    from learnnest.tts_generation import TtsGenerationError, generate_and_activate_tts
    from learnnest.tts_providers import TtsProviderError

    task_dir = workspace(tmp_path)
    old_podcast = (
        task_dir / "generated_podcasts" / "podcast-run" / "podcast_script.json"
    ).read_bytes()
    provider = FakeTtsProvider(error=TtsProviderError("HTTP 503"))

    with pytest.raises(TtsGenerationError, match="HTTP 503"):
        generate_and_activate_tts(task_dir, provider, tmp_path)

    task = load_task(task_dir)
    assert task.stages["note"] is StageStatus.COMPLETED
    assert task.stages["publish"] is StageStatus.COMPLETED
    assert task.stages["podcast_script"] is StageStatus.COMPLETED
    assert task.stages["tts"] is StageStatus.FAILED
    assert task.attempts[-1].status == "failed"
    assert task.attempts[-1].failed_stage == "tts"
    assert (
        task_dir / "generated_podcasts" / "podcast-run" / "podcast_script.json"
    ).read_bytes() == old_podcast
    assert not (tmp_path / "视频学习音频").exists()


def test_invalid_wav_never_reaches_conversion_or_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from learnnest.tts_generation import TtsGenerationError, generate_and_activate_tts

    task_dir = workspace(tmp_path)
    monkeypatch.setattr(
        "learnnest.tts_generation.convert_wav_to_mp3",
        lambda *args: pytest.fail("invalid WAV must not be converted"),
    )

    with pytest.raises(TtsGenerationError, match="valid WAV"):
        generate_and_activate_tts(
            task_dir,
            FakeTtsProvider(b"not-wav"),
            tmp_path,
        )

    assert not (tmp_path / "视频学习音频").exists()


def test_tts_rejects_semantically_invalid_script_before_provider_call(
    tmp_path: Path,
) -> None:
    from learnnest.tts_generation import generate_and_activate_tts

    task_dir = workspace(tmp_path)
    script_path = (
        task_dir / "generated_podcasts" / "podcast-run" / "podcast_script.json"
    )
    payload = json.loads(script_path.read_text(encoding="utf-8"))
    payload["segments"][1]["evidence_ids"] = ["tr_9999"]
    script_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    provider = FakeTtsProvider(wav_bytes())

    with pytest.raises(ValueError, match="active podcast is invalid"):
        generate_and_activate_tts(task_dir, provider, tmp_path)

    assert provider.calls == []


def test_tts_publish_recovers_without_a_second_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import learnnest.tts_generation as generation

    task_dir = workspace(tmp_path)
    fake_audio_tools(monkeypatch)
    original_replace = generation.atomic_replace_bytes
    marker_writes = 0

    def fail_final_marker(path: Path, content: bytes) -> None:
        nonlocal marker_writes
        if path.suffixes[-2:] == [".learnnest", ".json"]:
            marker_writes += 1
            if marker_writes == 2:
                raise OSError("simulated final marker failure")
        original_replace(path, content)

    monkeypatch.setattr(generation, "atomic_replace_bytes", fail_final_marker)
    first_provider = FakeTtsProvider(wav_bytes())

    with pytest.raises(generation.TtsGenerationError, match="audio publish failed"):
        generation.generate_and_activate_tts(task_dir, first_provider, tmp_path)

    published = tmp_path / "视频学习音频" / "lesson--a1b2c3d4.mp3"
    pending_marker = json.loads(
        published.with_suffix(".learnnest.json").read_text(encoding="utf-8")
    )
    assert pending_marker["status"] == "pending"
    assert published.read_bytes() == b"ID3fake-mp3"

    monkeypatch.setattr(generation, "atomic_replace_bytes", original_replace)
    recovery_provider = FakeTtsProvider(error=AssertionError("must not call provider"))

    recovered = generation.generate_and_activate_tts(
        task_dir,
        recovery_provider,
        tmp_path,
    )

    assert recovery_provider.calls == []
    assert recovered.stages["tts"] is StageStatus.COMPLETED
    completed_marker = json.loads(
        published.with_suffix(".learnnest.json").read_text(encoding="utf-8")
    )
    assert completed_marker["status"] == "completed"


def test_tts_publish_recovers_from_a_legacy_marker_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import learnnest.tts_generation as generation

    task_dir = workspace(tmp_path)
    fake_audio_tools(monkeypatch)
    original_replace = generation.atomic_replace_bytes
    marker_writes = 0

    def fail_final_marker(path: Path, content: bytes) -> None:
        nonlocal marker_writes
        if path.suffixes[-2:] == [".learnnest", ".json"]:
            marker_writes += 1
            if marker_writes == 2:
                raise OSError("simulated final marker failure")
        original_replace(path, content)

    monkeypatch.setattr(generation, "atomic_replace_bytes", fail_final_marker)
    with pytest.raises(generation.TtsGenerationError, match="audio publish failed"):
        generation.generate_and_activate_tts(
            task_dir,
            FakeTtsProvider(wav_bytes()),
            tmp_path,
        )

    published = tmp_path / "视频学习音频" / "lesson--a1b2c3d4.mp3"
    published.with_suffix(".learnnest.json").rename(
        published.with_suffix(".learnpipe.json")
    )
    monkeypatch.setattr(generation, "atomic_replace_bytes", original_replace)
    recovery_provider = FakeTtsProvider(error=AssertionError("must not call provider"))

    recovered = generation.generate_and_activate_tts(
        task_dir,
        recovery_provider,
        tmp_path,
    )

    assert recovery_provider.calls == []
    assert recovered.stages["tts"] is StageStatus.COMPLETED
    assert (
        json.loads(
            published.with_suffix(".learnnest.json").read_text(encoding="utf-8")
        )["status"]
        == "completed"
    )
    assert published.with_suffix(".learnpipe.json").is_file()


def test_tts_recovers_after_final_task_write_failure_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import learnnest.tts_generation as generation

    task_dir = workspace(tmp_path)
    fake_audio_tools(monkeypatch)
    original_write = generation.write_task_atomic
    monkeypatch.setattr(
        generation,
        "write_task_atomic",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("simulated task write failure")
        ),
    )

    with pytest.raises(OSError, match="simulated task write failure"):
        generation.generate_and_activate_tts(
            task_dir,
            FakeTtsProvider(wav_bytes()),
            tmp_path,
        )

    assert load_task(task_dir).stages["tts"] is StageStatus.PENDING
    monkeypatch.setattr(generation, "write_task_atomic", original_write)
    recovery_provider = FakeTtsProvider(error=AssertionError("must not call provider"))

    recovered = generation.generate_and_activate_tts(
        task_dir,
        recovery_provider,
        tmp_path,
    )

    assert recovery_provider.calls == []
    assert recovered.stages["tts"] is StageStatus.COMPLETED


def test_failed_tts_rerun_preserves_previous_active_audio(tmp_path: Path) -> None:
    from learnnest.tts_generation import TtsGenerationError, generate_and_activate_tts
    from learnnest.tts_providers import TtsProviderError

    task_dir = workspace(tmp_path)
    task = load_task(task_dir).model_copy(
        update={
            "stages": {**load_task(task_dir).stages, "tts": StageStatus.COMPLETED},
            "artifacts": {
                **load_task(task_dir).artifacts,
                "tts": [
                    "generated_audio/old/audio.json",
                    "generated_audio/old/audio.wav",
                    "generated_audio/old/audio.mp3",
                ],
            },
        }
    )
    write_task_atomic(task_dir, task)

    with pytest.raises(TtsGenerationError, match="HTTP 503"):
        generate_and_activate_tts(
            task_dir,
            FakeTtsProvider(error=TtsProviderError("HTTP 503")),
            tmp_path,
        )

    unchanged = load_task(task_dir)
    assert unchanged.stages["tts"] is StageStatus.COMPLETED
    assert unchanged.artifacts["tts"] == task.artifacts["tts"]
