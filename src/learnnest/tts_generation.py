"""Validated WAV/MP3 generation, immutable bundles, and safe publication."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from learnnest.models import StageStatus, TaskRecord
from learnnest.path_budget import assert_stage_path_budget
from learnnest.execution import (
    begin_persisted_attempt,
    complete_persisted_attempt,
    fail_persisted_attempt,
)
from learnnest.podcast_generation import validate_active_podcast_bundle
from learnnest.publication import (
    atomic_replace_bytes,
    read_audio_ownership_marker,
)
from learnnest.task_store import load_task, write_task_atomic
from learnnest.tts_providers import TtsProvider, TtsProviderError
from learnnest.util import safe_title

DEFAULT_TTS_STYLE = "温和、清晰、自然，像陪伴式知识播客；语速适中，重点处略作停顿。"


class TtsGenerationError(RuntimeError):
    def __init__(self, bundle_path: Path, errors: tuple[str, ...]) -> None:
        self.bundle_path = bundle_path
        self.errors = errors
        super().__init__(f"TTS generation failed: {'; '.join(errors)}")


def generate_and_activate_tts(
    task_dir: Path,
    provider: TtsProvider,
    output_root: Path,
    *,
    style_instruction: str = DEFAULT_TTS_STYLE,
) -> TaskRecord:
    begin_persisted_attempt(
        task_dir,
        reason="retry",
        from_stage="tts",
        now=datetime.now(UTC),
    )
    try:
        _generate_and_activate_tts(
            task_dir,
            provider,
            output_root,
            style_instruction=style_instruction,
        )
    except Exception as error:
        fail_persisted_attempt(
            task_dir,
            error,
            failed_stage="tts",
            now=datetime.now(UTC),
        )
        raise
    return complete_persisted_attempt(task_dir, now=datetime.now(UTC))


def _generate_and_activate_tts(
    task_dir: Path,
    provider: TtsProvider,
    output_root: Path,
    *,
    style_instruction: str,
) -> TaskRecord:
    recovered = reconcile_completed_tts_publication(task_dir, output_root)
    if recovered is not None:
        return recovered
    context = _load_context(task_dir)
    assert_stage_path_budget(
        context.task_dir,
        output_root,
        stage="tts",
        task_title=context.task.title,
        task_id=context.task.task_id,
    )
    run_id = _new_run_id(context.task)
    temporary, final = _prepare_bundle(context.task_dir, run_id)
    try:
        wav = provider.synthesize(context.speech, style_instruction)
        validate_wav_bytes(wav)
        _write_bytes(temporary / "audio.wav", wav)
        _validate_audio_probe(probe_audio(temporary / "audio.wav"), expected_codec=None)
        convert_wav_to_mp3(temporary / "audio.wav", temporary / "audio.mp3")
        _validate_audio_probe(
            probe_audio(temporary / "audio.mp3"),
            expected_codec="mp3",
        )
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, (TtsProviderError, TtsGenerationError, ValueError))
            else f"TTS processing failed: {type(error).__name__}"
        )
        return _finish_failed(
            temporary,
            final,
            context,
            run_id,
            provider,
            (message,),
        )

    published_relative = (
        Path("视频学习音频")
        / f"{safe_title(context.task.title)}--{context.task.task_id[-8:]}.mp3"
    )
    metadata = {
        "schema_version": "1.0",
        "run_id": run_id,
        "provider": provider.name,
        "model": provider.model,
        "voice": provider.voice,
        "task_id": context.task.task_id,
        "source_fingerprint": context.task.source_fingerprint,
        "podcast_script_sha256": context.script_sha256,
        "speech_sha256": context.speech_sha256,
        "mp3_sha256": hashlib.sha256(
            (temporary / "audio.mp3").read_bytes()
        ).hexdigest(),
        "published_path": published_relative.as_posix(),
        "status": "completed",
    }
    _write_text(
        temporary / "audio.json",
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
    )
    os.replace(temporary, final)

    try:
        _publish_audio(
            context.task,
            final / "audio.mp3",
            output_root.resolve() / published_relative,
        )
    except Exception as error:
        message = f"audio publish failed: {type(error).__name__}"
        _mark_tts_failed(context.task_dir, message)
        raise TtsGenerationError(final, (message,)) from error
    activated = _activate_tts_bundle(
        context.task_dir,
        final,
        provider_name=provider.name,
        model=provider.model,
    )
    (final / "audio.mp3").unlink(missing_ok=True)
    return activated


class _TtsContext:
    def __init__(
        self,
        task_dir: Path,
        task: TaskRecord,
        script_bytes: bytes,
        speech_bytes: bytes,
    ) -> None:
        self.task_dir = task_dir
        self.task = task
        self.script_sha256 = hashlib.sha256(script_bytes).hexdigest()
        self.speech_sha256 = hashlib.sha256(speech_bytes).hexdigest()
        self.speech = speech_bytes.decode("utf-8")


def _load_context(task_dir: Path) -> _TtsContext:
    root = task_dir.resolve()
    validated = validate_active_podcast_bundle(root, load_task(root))
    return _TtsContext(
        root,
        validated.task,
        validated.script_bytes,
        validated.speech_bytes,
    )


def validate_wav_bytes(content: bytes) -> None:
    try:
        with wave.open(io.BytesIO(content), "rb") as stream:
            valid = (
                stream.getnchannels() > 0
                and stream.getframerate() > 0
                and stream.getsampwidth() > 0
                and stream.getnframes() > 0
            )
    except (EOFError, wave.Error) as error:
        raise ValueError("TTS did not return a valid WAV") from error
    if not valid:
        raise ValueError("TTS did not return a valid WAV")


def probe_audio(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("ffprobe could not validate generated audio")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("ffprobe returned invalid audio metadata") from error


def convert_wav_to_mp3(source: Path, destination: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(destination),
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or not destination.is_file():
        raise ValueError("ffmpeg could not convert WAV to MP3")


def _validate_audio_probe(
    probe: dict[str, Any],
    *,
    expected_codec: str | None,
) -> None:
    streams = [
        item for item in probe.get("streams", []) if item.get("codec_type") == "audio"
    ]
    try:
        duration = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("generated audio has no valid duration") from error
    if not streams or duration <= 0:
        raise ValueError("generated audio has no decodable audio stream")
    if expected_codec is not None and not any(
        item.get("codec_name") == expected_codec for item in streams
    ):
        raise ValueError(f"generated audio codec is not {expected_codec}")


def _prepare_bundle(task_dir: Path, run_id: str) -> tuple[Path, Path]:
    parent = task_dir / "generated_audio"
    temporary = parent / f".{run_id}"
    final = parent / run_id
    if temporary.exists() or final.exists():
        raise FileExistsError(f"audio bundle already exists: {run_id}")
    parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    return temporary, final


def _finish_failed(
    temporary: Path,
    final: Path,
    context: _TtsContext,
    run_id: str,
    provider: TtsProvider,
    errors: tuple[str, ...],
) -> TaskRecord:
    _write_text(
        temporary / "generation.json",
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "provider": provider.name,
                "model": provider.model,
                "voice": provider.voice,
                "status": "failed",
                "podcast_script_sha256": context.script_sha256,
                "speech_sha256": context.speech_sha256,
                "errors": list(errors),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    os.replace(temporary, final)
    _mark_tts_failed(context.task_dir, errors[0])
    raise TtsGenerationError(final, errors)


def _mark_tts_failed(task_dir: Path, message: str) -> None:
    task = load_task(task_dir)
    if task.stages.get("tts") is StageStatus.COMPLETED and task.artifacts.get("tts"):
        return
    artifacts = {
        stage: paths for stage, paths in task.artifacts.items() if stage != "tts"
    }
    failed = task.model_copy(
        update={
            "stages": {**task.stages, "tts": StageStatus.FAILED},
            "artifacts": artifacts,
            "error_summary": message,
        }
    )
    write_task_atomic(task_dir, failed)


def _publish_audio(task: TaskRecord, source: Path, destination: Path) -> None:
    marker = destination.with_suffix(".learnnest.json")
    source_bytes = source.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    try:
        owner = read_audio_ownership_marker(destination)
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    if owner is not None:
        if owner.get("task_id") != task.task_id:
            raise RuntimeError("audio destination belongs to another task")
    elif destination.exists():
        raise RuntimeError("audio destination ownership is unknown")

    pending = {
        "schema_version": "1.0",
        "task_id": task.task_id,
        "mp3_sha256": source_sha256,
        "status": "pending",
    }
    atomic_replace_bytes(
        marker,
        _json_bytes(pending),
    )
    if (
        not destination.is_file()
        or hashlib.sha256(destination.read_bytes()).hexdigest() != source_sha256
    ):
        atomic_replace_bytes(destination, source_bytes)
    atomic_replace_bytes(marker, _json_bytes({**pending, "status": "completed"}))


def _activate_tts_bundle(
    task_dir: Path,
    bundle: Path,
    *,
    provider_name: str,
    model: str,
) -> TaskRecord:
    root = task_dir.resolve()
    relative = bundle.resolve().relative_to(root)
    required = ("audio.json", "audio.wav", "audio.mp3")
    if any(not (bundle / filename).is_file() for filename in required):
        raise ValueError("audio bundle is incomplete")
    task = load_task(root)
    artifacts = {
        stage: paths for stage, paths in task.artifacts.items() if stage != "tts"
    }
    artifacts["tts"] = [
        (relative / filename).as_posix() for filename in ("audio.json", "audio.wav")
    ]
    updated = task.model_copy(
        update={
            "stages": {**task.stages, "tts": StageStatus.COMPLETED},
            "artifacts": artifacts,
            "providers": {**task.providers, "tts": provider_name},
            "models": {**task.models, "tts": model},
            "error_summary": None,
        }
    )
    write_task_atomic(root, updated)
    return updated


def reconcile_completed_tts_publication(
    task_dir: Path,
    output_root: Path,
) -> TaskRecord | None:
    """Recover a generated bundle after publication or final task write failed."""
    root = task_dir.resolve()
    task = load_task(root)
    if task.stages.get("tts") not in {
        StageStatus.PENDING,
        StageStatus.FAILED,
        StageStatus.RUNNING,
    }:
        return None
    context = _load_context(root)
    bundles_root = root / "generated_audio"
    if not bundles_root.is_dir():
        return None
    for bundle in sorted(
        (path for path in bundles_root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    ):
        metadata_path = bundle / "audio.json"
        wav_path = bundle / "audio.wav"
        mp3_path = bundle / "audio.mp3"
        if not all(path.is_file() for path in (metadata_path, wav_path, mp3_path)):
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            mp3_sha256 = hashlib.sha256(mp3_path.read_bytes()).hexdigest()
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            metadata.get("status") != "completed"
            or metadata.get("task_id") != task.task_id
            or metadata.get("source_fingerprint") != task.source_fingerprint
            or metadata.get("podcast_script_sha256") != context.script_sha256
            or metadata.get("speech_sha256") != context.speech_sha256
            or metadata.get("mp3_sha256") != mp3_sha256
        ):
            continue
        published_path = metadata.get("published_path")
        provider_name = metadata.get("provider")
        model = metadata.get("model")
        if not all(
            isinstance(value, str) and value
            for value in (published_path, provider_name, model)
        ):
            continue
        assert isinstance(published_path, str)
        destination = (output_root.resolve() / published_path).resolve()
        if not destination.is_relative_to(output_root.resolve()):
            continue
        _publish_audio(task, mp3_path, destination)
        assert isinstance(provider_name, str)
        assert isinstance(model, str)
        activated = _activate_tts_bundle(
            root,
            bundle,
            provider_name=provider_name,
            model=model,
        )
        mp3_path.unlink(missing_ok=True)
        return activated
    return None


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_text(path: Path, content: str) -> None:
    _write_bytes(path, content.encode("utf-8"))


def _write_bytes(path: Path, content: bytes) -> None:
    with path.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _new_run_id(task: TaskRecord) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{task.task_id[-8:]}"
