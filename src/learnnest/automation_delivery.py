"""Route-preserving podcast and audio artifacts for model-reviewed Markdown."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from learnnest.models import ContentPack, StageStatus
from learnnest.podcast_models import PodcastScript
from learnnest.podcast_providers import PodcastProvider
from learnnest.podcast_validation import parse_podcast_script, validate_podcast_script
from learnnest.rendering import render_podcast_speech
from learnnest.task_store import find_task_by_id
from learnnest.tts_generation import convert_wav_to_mp3, probe_audio, validate_wav_bytes
from learnnest.tts_providers import TtsProvider
from learnnest.util import safe_title


@dataclass(frozen=True)
class ReviewedMarkdownSource:
    task_id: str
    task_dir: Path
    content_pack: ContentPack
    content_pack_sha256: str
    markdown: str
    markdown_sha256: str
    plan_id: str
    dossier_sha256: str


@dataclass(frozen=True)
class PodcastArtifact:
    directory: Path
    script: PodcastScript
    speech: str
    script_sha256: str
    speech_sha256: str


def load_reviewed_markdown_source(
    output_root: str | Path,
    *,
    task_id: str,
    reviewed_path: str | Path,
    plan_id: str,
    dossier_sha256: str,
) -> ReviewedMarkdownSource:
    root = Path(output_root).resolve()
    found = find_task_by_id(root, task_id)
    if found is None:
        raise ValueError(f"task not found: {task_id}")
    task_dir, task = found
    if task.stages.get("content_pack") is not StageStatus.COMPLETED:
        raise ValueError("task has no completed content pack")
    pack_path = next(
        (
            task_dir / relative
            for relative in task.artifacts.get("content_pack", [])
            if Path(relative).name == "content_pack.json"
        ),
        None,
    )
    if pack_path is None or not pack_path.is_file():
        raise ValueError("task content pack is missing")
    pack_bytes = pack_path.read_bytes()
    pack = ContentPack.model_validate_json(pack_bytes)
    if (
        pack.task_id != task.task_id
        or pack.source_fingerprint != task.source_fingerprint
    ):
        raise ValueError("content pack does not match task identity")
    note_path = Path(reviewed_path).resolve()
    if not note_path.is_relative_to(task_dir.resolve()) or not note_path.is_file():
        raise ValueError("reviewed Markdown is missing or outside its task")
    markdown = note_path.read_text(encoding="utf-8").strip()
    if not markdown:
        raise ValueError("reviewed Markdown is empty")
    return ReviewedMarkdownSource(
        task_id=task.task_id,
        task_dir=task_dir,
        content_pack=pack,
        content_pack_sha256=_sha256(pack_bytes),
        markdown=markdown + "\n",
        markdown_sha256=_sha256((markdown + "\n").encode("utf-8")),
        plan_id=plan_id,
        dossier_sha256=dossier_sha256,
    )


def generate_model_reviewed_podcast(
    source: ReviewedMarkdownSource,
    provider: PodcastProvider,
    *,
    delivery_dir: Path,
) -> PodcastArtifact:
    """Make exactly one structured podcast call from a route-labeled Markdown source."""
    directory = delivery_dir / "podcast"
    if directory.exists():
        return load_podcast_artifact(source, delivery_dir)
    temporary = directory.with_name(".podcast")
    temporary.mkdir(parents=True, exist_ok=False)
    context = json.dumps(
        {
            "route": "assisted_draft",
            "review_status": "model_reviewed",
            "assisted_plan_id": source.plan_id,
            "dossier_sha256": source.dossier_sha256,
            "content_pack": source.content_pack.model_dump(mode="json"),
            "reviewed_markdown": source.markdown,
            "note_content_sha256": source.markdown_sha256,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        raw = provider.generate(context, ())
        _write_text(temporary / "response.raw.txt", raw)
        script = parse_podcast_script(raw)
        errors = validate_podcast_script(
            script, source.content_pack, source.markdown_sha256
        )
        if errors:
            raise ValueError("; ".join(errors))
        speech = render_podcast_speech(script)
        _write_text(temporary / "podcast_script.json", script.model_dump_json(indent=2))
        _write_text(temporary / "speech.txt", speech)
        _write_json(
            temporary / "metadata.json",
            _route_metadata(source, provider=provider, status="completed"),
        )
        os.replace(temporary, directory)
    except Exception:
        _write_json(
            temporary / "metadata.json",
            _route_metadata(source, provider=provider, status="failed"),
        )
        failed = delivery_dir / "podcast-failed"
        if failed.exists():
            failed = delivery_dir / "podcast-failed-latest"
        os.replace(temporary, failed)
        raise
    return PodcastArtifact(
        directory=directory,
        script=script,
        speech=speech,
        script_sha256=_sha256((directory / "podcast_script.json").read_bytes()),
        speech_sha256=_sha256((directory / "speech.txt").read_bytes()),
    )


def load_podcast_artifact(
    source: ReviewedMarkdownSource, delivery_dir: Path
) -> PodcastArtifact:
    directory = delivery_dir / "podcast"
    metadata_path = directory / "metadata.json"
    if not metadata_path.is_file():
        raise ValueError("model-reviewed podcast artifact is missing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if (
        metadata.get("route") != "assisted_draft"
        or metadata.get("review_status") != "model_reviewed"
        or metadata.get("note_content_sha256") != source.markdown_sha256
        or metadata.get("status") != "completed"
    ):
        raise ValueError("model-reviewed podcast metadata does not match its source")
    script_bytes = (directory / "podcast_script.json").read_bytes()
    speech_bytes = (directory / "speech.txt").read_bytes()
    script = PodcastScript.model_validate_json(script_bytes)
    errors = validate_podcast_script(
        script, source.content_pack, source.markdown_sha256
    )
    if errors or speech_bytes != render_podcast_speech(script).encode("utf-8"):
        raise ValueError("model-reviewed podcast artifact is invalid")
    return PodcastArtifact(
        directory=directory,
        script=script,
        speech=speech_bytes.decode("utf-8"),
        script_sha256=_sha256(script_bytes),
        speech_sha256=_sha256(speech_bytes),
    )


def generate_model_reviewed_tts(
    source: ReviewedMarkdownSource,
    podcast: PodcastArtifact,
    provider: TtsProvider,
    *,
    output_root: str | Path,
    delivery_dir: Path,
    style_instruction: str,
) -> Path:
    """Make exactly one TTS call and publish a route-labeled MP3."""
    directory = delivery_dir / "tts"
    if directory.exists():
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        published = Path(output_root).resolve() / str(metadata["published_path"])
        audio = directory / "audio.mp3"
        if metadata.get("status") == "completed" and audio.is_file():
            _publish_audio(source, audio, published, podcast)
            return published
        raise ValueError("model-reviewed TTS artifact is incomplete")
    temporary = directory.with_name(".tts")
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        wav = provider.synthesize(podcast.speech, style_instruction)
        (temporary / "audio.wav").write_bytes(wav)
        validate_wav_bytes(wav)
        probe_audio(temporary / "audio.wav")
        convert_wav_to_mp3(temporary / "audio.wav", temporary / "audio.mp3")
        probe_audio(temporary / "audio.mp3")
        relative = (
            Path("视频学习音频")
            / "assisted-draft"
            / f"{safe_title(source.content_pack.task_id)}--{source.task_id[-8:]}.mp3"
        )
        _write_json(
            temporary / "metadata.json",
            {
                **_route_metadata(source, provider=provider, status="completed"),
                "podcast_script_sha256": podcast.script_sha256,
                "speech_sha256": podcast.speech_sha256,
                "published_path": relative.as_posix(),
            },
        )
        os.replace(temporary, directory)
    except Exception:
        _write_json(
            temporary / "metadata.json",
            _route_metadata(source, provider=provider, status="failed"),
        )
        failed = delivery_dir / "tts-failed"
        if failed.exists():
            failed = delivery_dir / "tts-failed-latest"
        os.replace(temporary, failed)
        raise
    destination = Path(output_root).resolve() / relative
    _publish_audio(source, directory / "audio.mp3", destination, podcast)
    return destination


def _publish_audio(
    source: ReviewedMarkdownSource,
    audio_path: Path,
    destination: Path,
    podcast: PodcastArtifact,
) -> None:
    content = audio_path.read_bytes()
    marker = destination.with_suffix(".learnnest.json")
    marker.parent.mkdir(parents=True, exist_ok=True)
    pending = {
        "schema_version": "1.0",
        "route": "assisted_draft",
        "review_status": "model_reviewed",
        "task_id": source.task_id,
        "assisted_plan_id": source.plan_id,
        "note_content_sha256": source.markdown_sha256,
        "podcast_script_sha256": podcast.script_sha256,
        "mp3_sha256": _sha256(content),
        "status": "pending",
    }
    _write_json(marker, pending)
    _write_bytes(destination, content)
    _write_json(marker, {**pending, "status": "completed"})


def _route_metadata(
    source: ReviewedMarkdownSource, *, provider: object, status: str
) -> dict[str, str]:
    return {
        "schema_version": "1.0",
        "route": "assisted_draft",
        "review_status": "model_reviewed",
        "task_id": source.task_id,
        "source_fingerprint": source.content_pack.source_fingerprint,
        "assisted_plan_id": source.plan_id,
        "dossier_sha256": source.dossier_sha256,
        "content_pack_sha256": source.content_pack_sha256,
        "note_content_sha256": source.markdown_sha256,
        "provider": str(getattr(provider, "name", "unknown")),
        "model": str(getattr(provider, "model", "unknown")),
        "status": status,
    }


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_text(path: Path, value: str) -> None:
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def _write_json(path: Path, value: object) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=path.parent) as stream:
        stream.write(value)
        temporary = Path(stream.name)
    temporary.replace(path)
