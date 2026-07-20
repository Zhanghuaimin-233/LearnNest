"""Validation for persisted task artifacts and cross-file references."""

from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from learnnest.models import ContentPack, StageStatus, TaskRecord
from learnnest.note_models import GeneratedNoteV4
from learnnest.note_templates import load_template_snapshot
from learnnest.note_validation import (
    parse_generated_note,
    validate_generated_note,
    validate_v4_bundle_provenance,
    validate_v4_source_contract,
)
from learnnest.podcast_models import PodcastScript
from learnnest.podcast_validation import validate_podcast_script
from learnnest.publication import (
    read_audio_ownership_marker,
    select_published_note_path,
)
from learnnest.rendering import render_podcast_script, render_podcast_speech
from learnnest.task_store import load_task
from learnnest.tts_generation import probe_audio, validate_wav_bytes
from learnnest.util import safe_title

_NOTE_EVIDENCE_PATTERN = re.compile(
    r"(?<!!)(?<!\[)(?<!\\)\[([a-z][a-z0-9_-]*)\](?![\](])"
)
_NOTE_EVIDENCE_LIKE_MARKER_PATTERN = re.compile(r"<!--\s*evidence\b", re.IGNORECASE)
_NOTE_EVIDENCE_COMMENT_PATTERN = re.compile(
    r"<!-- evidence: (?P<body>(?:(?!<!--)[^\r\n])*?) -->"
)
_NOTE_EVIDENCE_ID_PATTERN = re.compile(r"(?:tr|fr|ocr|ai)_[0-9]{4,}")
_OBSIDIAN_EMBED_PATTERN = re.compile(r"!\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
_REQUIRED_NOTE_ARTIFACTS = ("note.json", "note.md")
_REQUIRED_PODCAST_ARTIFACTS = (
    "podcast_script.json",
    "speech.txt",
)
_REQUIRED_TTS_ARTIFACTS = ("audio.json", "audio.wav")


def validate_task(task_dir: str | Path) -> list[str]:
    """Return contract violations found in one task directory."""
    root = Path(task_dir).resolve()
    errors: list[str] = []
    try:
        task = load_task(root)
    except (OSError, ValidationError, ValueError) as error:
        return [f"invalid task.json: {error}"]

    artifact_paths: dict[str, list[Path]] = {}
    for stage, paths in task.artifacts.items():
        artifact_paths[stage] = []
        for artifact_path in paths:
            resolved = _resolve_artifact_path(root, artifact_path, errors)
            if resolved is not None:
                artifact_paths[stage].append(resolved)
                if not resolved.exists():
                    errors.append(f"artifact is missing: {artifact_path}")

    for stage, status in task.stages.items():
        if status is StageStatus.COMPLETED and stage not in task.artifacts:
            errors.append(f"completed stage has no artifact: {stage}")
    for stage in task.artifacts:
        if task.stages.get(stage) is not StageStatus.COMPLETED:
            errors.append(f"artifact stage is not completed: {stage}")

    if task.stages.get("note") is StageStatus.COMPLETED:
        declared_note_artifacts = {
            Path(path).name for path in task.artifacts.get("note", [])
        }
        for required in _REQUIRED_NOTE_ARTIFACTS:
            if required not in declared_note_artifacts:
                errors.append(
                    f"completed note stage is missing required artifact: {required}"
                )

    _validate_required_stage_artifacts(
        task,
        "podcast_script",
        _REQUIRED_PODCAST_ARTIFACTS,
        errors,
    )
    _validate_required_stage_artifacts(
        task,
        "tts",
        _REQUIRED_TTS_ARTIFACTS,
        errors,
    )

    content_pack = _load_content_pack(task, artifact_paths, errors)
    if content_pack is not None:
        content_pack_path = _artifact_named(
            artifact_paths.get("content_pack", []), "content_pack.json"
        )
        try:
            content_pack_sha256 = (
                hashlib.sha256(content_pack_path.read_bytes()).hexdigest()
                if content_pack_path is not None
                else None
            )
        except OSError:
            content_pack_sha256 = None
        _validate_evidence_artifacts(root, content_pack, errors)
        note_json_path = _artifact_named(artifact_paths.get("note", []), "note.json")
        note_markdown_path = _artifact_named(artifact_paths.get("note", []), "note.md")
        _validate_note_references(
            note_json_path,
            content_pack,
            errors,
            content_pack_sha256=content_pack_sha256,
        )
        _validate_note_markdown(
            root,
            task,
            note_markdown_path,
            content_pack,
            errors,
        )
        _validate_published_note(
            root,
            task,
            note_markdown_path,
            content_pack,
            errors,
        )
        _validate_podcast_artifacts(
            task,
            artifact_paths,
            content_pack,
            errors,
        )
        _validate_tts_artifacts(
            root,
            task,
            artifact_paths,
            errors,
        )
    return errors


def _validate_required_stage_artifacts(
    task: TaskRecord,
    stage: str,
    required: tuple[str, ...],
    errors: list[str],
) -> None:
    if task.stages.get(stage) is not StageStatus.COMPLETED:
        return
    declared = {Path(path).name for path in task.artifacts.get(stage, [])}
    for filename in required:
        if filename not in declared:
            errors.append(
                f"completed {stage} stage is missing required artifact: {filename}"
            )


def _resolve_artifact_path(
    root: Path, artifact_path: str, errors: list[str]
) -> Path | None:
    candidate = Path(artifact_path)
    if candidate.is_absolute():
        errors.append(f"artifact path must be relative: {artifact_path}")
        return None
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        errors.append(f"artifact path escapes task root: {artifact_path}")
        return None
    return resolved


def _load_content_pack(
    task: TaskRecord, artifact_paths: dict[str, list[Path]], errors: list[str]
) -> ContentPack | None:
    content_pack_path = _artifact_named(
        artifact_paths.get("content_pack", []), "content_pack.json"
    )
    if content_pack_path is None:
        return None
    try:
        content_pack = ContentPack.model_validate_json(
            content_pack_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as error:
        errors.append(f"invalid content pack: {error}")
        return None
    if content_pack.task_id != task.task_id:
        errors.append("content pack task_id does not match task.json")
    if content_pack.source_fingerprint != task.source_fingerprint:
        errors.append("content pack source_fingerprint does not match task.json")
    return content_pack


def _artifact_named(paths: list[Path], filename: str) -> Path | None:
    return next(
        (path for path in paths if path.name == filename and path.is_file()), None
    )


def _validate_evidence_artifacts(
    root: Path, content_pack: ContentPack, errors: list[str]
) -> None:
    for evidence in content_pack.evidence:
        resolved = _resolve_artifact_path(root, evidence.artifact_path, errors)
        if resolved is not None and not resolved.is_file():
            errors.append(f"artifact is missing: {evidence.artifact_path}")


def _validate_note_references(
    note_path: Path | None,
    content_pack: ContentPack,
    errors: list[str],
    *,
    content_pack_sha256: str | None,
) -> None:
    if note_path is None or not note_path.is_file():
        return
    try:
        raw_note = note_path.read_text(encoding="utf-8")
        note = json.loads(raw_note)
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"invalid note.json: {error}")
        return
    if isinstance(note, dict) and note.get("schema_version") in {"2.0", "3.0", "4.0"}:
        schema_version = note["schema_version"]
        try:
            generated_note = parse_generated_note(raw_note)
        except ValidationError as error:
            errors.append(f"invalid generated note {schema_version}: {error}")
            return
        if isinstance(generated_note, GeneratedNoteV4):
            snapshot_path = note_path.parent / "template.json"
            try:
                template = load_template_snapshot(snapshot_path)
            except ValueError:
                errors.append("active GeneratedNote 4.0 template snapshot is invalid")
                return
            errors.extend(
                validate_v4_source_contract(
                    generated_note,
                    content_pack,
                    template=template,
                )
            )
            errors.extend(
                validate_v4_bundle_provenance(
                    note_path.parent,
                    generated_note,
                    template,
                    content_pack_sha256=content_pack_sha256,
                )
            )
        else:
            errors.extend(validate_generated_note(generated_note, content_pack))
        return
    known_ids = {evidence.id for evidence in content_pack.evidence}
    for evidence_id in _evidence_ids(note):
        if evidence_id not in known_ids:
            errors.append(f"note references unknown evidence id: {evidence_id}")


def _validate_note_markdown(
    task_dir: Path,
    task: TaskRecord,
    note_path: Path | None,
    content_pack: ContentPack,
    errors: list[str],
) -> None:
    if note_path is None or not note_path.is_file():
        return
    try:
        markdown = note_path.read_text(encoding="utf-8")
    except UnicodeError:
        errors.append("invalid note.md: file is not valid UTF-8")
        return
    except OSError as error:
        errors.append(f"invalid note.md: {error}")
        return

    errors.extend(validate_note_markdown_text(task_dir, markdown, content_pack))
    errors.extend(_validate_derived_note_links(task_dir, task, markdown))


def _validate_derived_note_links(
    task_dir: Path,
    task: TaskRecord,
    markdown: str,
) -> list[str]:
    errors: list[str] = []
    output_root = task_dir.parents[1]
    asset_prefix = task_dir.relative_to(output_root).as_posix()
    podcast_paths = [
        PurePosixPath(path) for path in task.artifacts.get("podcast_script", [])
    ]
    script_path = next(
        (path for path in podcast_paths if path.name == "podcast_script.md"),
        next((path for path in podcast_paths if path.name == "speech.txt"), None),
    )
    if task.stages.get("podcast_script") is StageStatus.COMPLETED:
        if script_path is not None:
            label = "播客稿" if script_path.name == "podcast_script.md" else "口播稿"
            expected = f"[[{PurePosixPath(asset_prefix) / script_path}|{label}]]"
            if expected not in markdown:
                errors.append("note Markdown is missing active podcast link")
    if task.stages.get("tts") is StageStatus.COMPLETED:
        expected = (
            f"[[视频学习音频/{safe_title(task.title)}--{task.task_id[-8:]}.mp3|音频]]"
        )
        if expected not in markdown:
            errors.append("note Markdown is missing active audio link")
    return errors


def _validate_published_note(
    task_dir: Path,
    task: TaskRecord,
    active_note_path: Path | None,
    content_pack: ContentPack,
    errors: list[str],
) -> None:
    if task.stages.get("publish") is not StageStatus.COMPLETED:
        return
    if active_note_path is None or not active_note_path.is_file():
        return
    output_root = task_dir.parents[1]
    try:
        destination = select_published_note_path(task_dir, task, output_root)
    except RuntimeError as error:
        errors.append(f"invalid published Vault note: {error}")
        return
    if not destination.is_file():
        errors.append(f"published Vault note is missing: {destination.name}")
        return
    try:
        active_bytes = active_note_path.read_bytes()
        published_bytes = destination.read_bytes()
        published_markdown = published_bytes.decode("utf-8")
    except (OSError, UnicodeError) as error:
        errors.append(f"invalid published Vault note: {error}")
        return
    if published_bytes != active_bytes:
        errors.append("published Vault note does not match active note bundle")
        return
    errors.extend(
        f"published Vault note: {error}"
        for error in validate_note_markdown_text(
            task_dir,
            published_markdown,
            content_pack,
        )
    )
    errors.extend(
        f"published Vault note: {error}"
        for error in _validate_derived_note_links(
            task_dir,
            task,
            published_markdown,
        )
    )


def _validate_podcast_artifacts(
    task: TaskRecord,
    artifact_paths: dict[str, list[Path]],
    content_pack: ContentPack,
    errors: list[str],
) -> None:
    if task.stages.get("podcast_script") is not StageStatus.COMPLETED:
        return
    script_path = _artifact_named(
        artifact_paths.get("podcast_script", []),
        "podcast_script.json",
    )
    speech_path = _artifact_named(
        artifact_paths.get("podcast_script", []),
        "speech.txt",
    )
    markdown_path = _artifact_named(
        artifact_paths.get("podcast_script", []),
        "podcast_script.md",
    )
    note_path = _artifact_named(artifact_paths.get("note", []), "note.json")
    if script_path is None or speech_path is None or note_path is None:
        return
    try:
        script = PodcastScript.model_validate_json(script_path.read_bytes())
        note_sha = hashlib.sha256(note_path.read_bytes()).hexdigest()
        speech = speech_path.read_bytes()
    except (OSError, UnicodeError, ValidationError, ValueError) as error:
        errors.append(f"invalid podcast artifacts: {error}")
        return
    errors.extend(validate_podcast_script(script, content_pack, note_sha))
    if speech != render_podcast_speech(script).encode("utf-8"):
        errors.append("speech.txt does not match podcast_script.json")
    if markdown_path is not None:
        try:
            markdown = markdown_path.read_bytes()
        except OSError as error:
            errors.append(f"invalid podcast artifacts: {error}")
        else:
            if markdown != render_podcast_script(script).encode("utf-8"):
                errors.append("podcast_script.md does not match podcast_script.json")


def _validate_tts_artifacts(
    task_dir: Path,
    task: TaskRecord,
    artifact_paths: dict[str, list[Path]],
    errors: list[str],
) -> None:
    if task.stages.get("tts") is not StageStatus.COMPLETED:
        return
    audio_json = _artifact_named(artifact_paths.get("tts", []), "audio.json")
    wav_path = _artifact_named(artifact_paths.get("tts", []), "audio.wav")
    script_path = _artifact_named(
        artifact_paths.get("podcast_script", []),
        "podcast_script.json",
    )
    speech_path = _artifact_named(
        artifact_paths.get("podcast_script", []),
        "speech.txt",
    )
    if None in (audio_json, wav_path, script_path, speech_path):
        return
    assert audio_json is not None
    assert wav_path is not None
    assert script_path is not None
    assert speech_path is not None
    try:
        metadata = json.loads(audio_json.read_text(encoding="utf-8"))
        wav_bytes = wav_path.read_bytes()
        script_sha = hashlib.sha256(script_path.read_bytes()).hexdigest()
        speech_sha = hashlib.sha256(speech_path.read_bytes()).hexdigest()
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        errors.append(f"invalid TTS artifacts: {error}")
        return
    if metadata.get("task_id") != task.task_id:
        errors.append("audio.json task_id does not match task.json")
    if metadata.get("source_fingerprint") != task.source_fingerprint:
        errors.append("audio.json source_fingerprint does not match task.json")
    if metadata.get("podcast_script_sha256") != script_sha:
        errors.append("audio.json podcast_script_sha256 does not match active script")
    if metadata.get("speech_sha256") != speech_sha:
        errors.append("audio.json speech_sha256 does not match active speech")
    try:
        validate_wav_bytes(wav_bytes)
        wav_probe = probe_audio(wav_path)
        _validate_audio_probe(wav_probe, expected_codec=None)
    except (OSError, ValueError) as error:
        errors.append(f"invalid generated audio: {error}")

    published_path = metadata.get("published_path")
    if not isinstance(published_path, str) or not published_path:
        errors.append("audio.json published_path is missing")
        return
    published = (task_dir.parents[1] / published_path).resolve()
    if not published.is_relative_to(task_dir.parents[1].resolve()):
        errors.append("audio published_path escapes output root")
    elif not published.is_file():
        errors.append(f"published audio is missing: {published_path}")
    else:
        try:
            published_sha = hashlib.sha256(published.read_bytes()).hexdigest()
            marker = read_audio_ownership_marker(published)
            if marker is None:
                raise ValueError("audio ownership marker is missing")
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"invalid published audio ownership marker: {error}")
            return
        if metadata.get("mp3_sha256") != published_sha:
            errors.append("published audio does not match active audio bundle")
        if marker.get("task_id") != task.task_id:
            errors.append("published audio ownership marker has wrong task_id")
        if marker.get("mp3_sha256") != published_sha:
            errors.append("published audio ownership marker has wrong hash")
        if marker.get("status") != "completed":
            errors.append("published audio ownership marker is not completed")
        try:
            _validate_audio_probe(probe_audio(published), expected_codec="mp3")
        except (OSError, ValueError) as error:
            errors.append(f"invalid published audio: {error}")


def _validate_audio_probe(
    probe: dict[str, object],
    *,
    expected_codec: str | None,
) -> None:
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise ValueError("audio probe has no streams")
    audio_streams = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    format_payload = probe.get("format")
    try:
        duration = float(format_payload["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("audio probe has no duration") from error
    if not audio_streams or duration <= 0:
        raise ValueError("audio probe has no decodable stream")
    if expected_codec is not None and not any(
        item.get("codec_name") == expected_codec for item in audio_streams
    ):
        raise ValueError(f"audio codec is not {expected_codec}")


def validate_note_markdown_text(
    task_dir: Path,
    markdown: str,
    content_pack: ContentPack,
) -> list[str]:
    """Validate rendered note text without requiring it to be activated."""
    errors: list[str] = []

    known_ids = {item.id for item in content_pack.evidence}
    for evidence_id in _NOTE_EVIDENCE_PATTERN.findall(markdown):
        if evidence_id not in known_ids:
            errors.append(
                f"note Markdown references unknown evidence id: {evidence_id}"
            )
    for marker in _NOTE_EVIDENCE_LIKE_MARKER_PATTERN.finditer(markdown):
        comment = _NOTE_EVIDENCE_COMMENT_PATTERN.match(markdown, marker.start())
        if comment is None:
            errors.append("note Markdown contains invalid evidence comment or marker")
            continue
        comment_body = comment.group("body")
        for raw_evidence_id in comment_body.split(","):
            evidence_id = raw_evidence_id.strip()
            if _NOTE_EVIDENCE_ID_PATTERN.fullmatch(evidence_id) is None:
                evidence_id = evidence_id or "<empty>"
                errors.append(
                    f"note Markdown contains invalid evidence id: {evidence_id}"
                )
            elif evidence_id not in known_ids:
                errors.append(
                    f"note Markdown references unknown evidence id: {evidence_id}"
                )

    output_root = task_dir.parents[1]
    expected_targets = _expected_frame_targets(task_dir, content_pack)
    for target in _OBSIDIAN_EMBED_PATTERN.findall(markdown):
        normalized = str(PurePosixPath(target))
        if normalized not in expected_targets:
            errors.append(f"embedded image is not frame evidence: {target}")
            continue
        resolved = (output_root / Path(*PurePosixPath(normalized).parts)).resolve()
        if not resolved.is_relative_to(output_root.resolve()):
            errors.append(f"embedded image escapes output root: {target}")
        elif not resolved.is_file():
            errors.append(f"embedded frame is missing: {target}")
    return errors


def _expected_frame_targets(task_dir: Path, content_pack: ContentPack) -> set[str]:
    output_root = task_dir.parents[1]
    task_relative = task_dir.relative_to(output_root)
    return {
        str(PurePosixPath(task_relative.as_posix()) / item.artifact_path)
        for item in content_pack.evidence
        if item.kind == "frame"
    }


def _evidence_ids(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "evidence_ids" and isinstance(child, list):
                yield from (item for item in child if isinstance(item, str))
            else:
                yield from _evidence_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from _evidence_ids(child)
