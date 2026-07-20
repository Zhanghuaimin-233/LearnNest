"""Immutable PodcastScript generation and task activation."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from learnnest.execution import (
    begin_persisted_attempt,
    complete_persisted_attempt,
    fail_persisted_attempt,
)
from learnnest.models import ContentPack, StageStatus, TaskRecord
from learnnest.path_budget import assert_stage_path_budget
from learnnest.note_models import AnyGeneratedNote, GeneratedNoteV4
from learnnest.note_templates import load_template_snapshot
from learnnest.note_validation import (
    parse_generated_note,
    validate_generated_note,
    validate_v4_bundle_provenance,
    validate_v4_source_contract,
)
from learnnest.podcast_models import PodcastScript
from learnnest.podcast_providers import PodcastProvider, PodcastProviderError
from learnnest.podcast_validation import (
    parse_podcast_script,
    safe_podcast_schema_errors,
    validate_podcast_script,
)
from learnnest.publication import reconcile_pending_note_publication
from learnnest.rendering import render_podcast_speech
from learnnest.task_store import load_task, write_task_atomic

_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PodcastGenerationError(RuntimeError):
    def __init__(self, bundle_path: Path, errors: tuple[str, ...]) -> None:
        self.bundle_path = bundle_path
        self.errors = errors
        super().__init__(f"podcast generation failed: {'; '.join(errors)}")


def generate_and_activate_podcast(
    task_dir: Path,
    provider: PodcastProvider,
    output_root: Path | None = None,
    *,
    retain_debug_artifacts: bool = False,
) -> TaskRecord:
    recovered = _recover_note_link_publication(task_dir, output_root)
    if recovered is not None:
        return recovered
    task = load_task(task_dir)
    assert_stage_path_budget(
        task_dir,
        output_root,
        stage="podcast_script",
        task_title=task.title,
        task_id=task.task_id,
    )
    begin_persisted_attempt(
        task_dir,
        reason="retry",
        from_stage="podcast_script",
        now=datetime.now(UTC),
    )
    try:
        bundle = generate_podcast_bundle(
            task_dir,
            provider,
            retain_debug_artifacts=retain_debug_artifacts,
        )
        activated = _activate_podcast_bundle(
            task_dir,
            bundle,
            provider=provider.name,
            model=provider.model,
        )
        _refresh_note_links(activated, task_dir, output_root)
    except Exception as error:
        fail_persisted_attempt(
            task_dir,
            error,
            failed_stage="podcast_script",
            now=datetime.now(UTC),
        )
        raise
    return complete_persisted_attempt(task_dir, now=datetime.now(UTC))


def build_and_activate_external_podcast(
    task_dir: Path,
    raw_path: Path,
    output_root: Path | None = None,
) -> TaskRecord:
    recovered = _recover_note_link_publication(task_dir, output_root)
    if recovered is not None:
        return recovered
    begin_persisted_attempt(
        task_dir,
        reason="retry",
        from_stage="podcast_script",
        now=datetime.now(UTC),
    )
    try:
        context = _load_context(task_dir)
        raw = raw_path.read_text(encoding="utf-8")
        bundle = _build_podcast_bundle(
            context,
            raw,
            provider="external-agent",
            model="external",
            run_id=None,
            attempt_count=1,
        )
        activated = _activate_podcast_bundle(
            task_dir,
            bundle,
            provider="external-agent",
            model="external",
        )
        _refresh_note_links(activated, task_dir, output_root)
    except Exception as error:
        fail_persisted_attempt(
            task_dir,
            error,
            failed_stage="podcast_script",
            now=datetime.now(UTC),
        )
        raise
    return complete_persisted_attempt(task_dir, now=datetime.now(UTC))


def generate_podcast_bundle(
    task_dir: Path,
    provider: PodcastProvider,
    *,
    run_id: str | None = None,
    retain_debug_artifacts: bool = False,
) -> Path:
    context = _load_context(task_dir)
    selected_run_id = run_id or _new_run_id(context.task)
    temporary, final = _prepare_bundle(context.task_dir, selected_run_id)
    feedback: tuple[str, ...] = ()
    attempts = 0
    for attempt in range(1, 3):
        attempts = attempt
        try:
            raw = provider.generate(context.provider_context_json, feedback)
        except Exception as error:
            errors = (
                str(error)
                if isinstance(error, PodcastProviderError)
                else f"provider failed: {type(error).__name__}",
            )
            return _finish_failed(
                temporary,
                final,
                context,
                selected_run_id,
                provider.name,
                provider.model,
                attempts,
                errors,
            )
        if retain_debug_artifacts:
            _write_text(temporary / f"attempt-{attempt}.raw.txt", raw)
        script, errors = _parse_and_validate(raw, context)
        if script is not None and not errors:
            return _finish_success(
                temporary,
                final,
                context,
                selected_run_id,
                provider.name,
                provider.model,
                attempts,
                script,
            )
        feedback = errors
    return _finish_failed(
        temporary,
        final,
        context,
        selected_run_id,
        provider.name,
        provider.model,
        attempts,
        feedback,
    )


class _PodcastContext:
    def __init__(
        self,
        task_dir: Path,
        task: TaskRecord,
        content_pack: ContentPack,
        note: AnyGeneratedNote,
        note_bytes: bytes,
    ) -> None:
        self.task_dir = task_dir
        self.task = task
        self.content_pack = content_pack
        self.note_sha256 = hashlib.sha256(note_bytes).hexdigest()
        self.provider_context_json = json.dumps(
            {
                "content_pack": content_pack.model_dump(mode="json"),
                "note": note.model_dump(mode="json"),
                "note_content_sha256": self.note_sha256,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _load_context(task_dir: Path) -> _PodcastContext:
    root = task_dir.resolve()
    task = load_task(root)
    pack_path = _active_artifact(root, task, "content_pack", "content_pack.json")
    note_path = _active_artifact(root, task, "note", "note.json")
    pack_bytes = pack_path.read_bytes()
    note_bytes = note_path.read_bytes()
    content_pack = ContentPack.model_validate_json(pack_bytes)
    note = parse_generated_note(note_bytes.decode("utf-8"))
    if content_pack.task_id != task.task_id:
        raise ValueError("active content pack task_id does not match task.json")
    if content_pack.source_fingerprint != task.source_fingerprint:
        raise ValueError(
            "active content pack source_fingerprint does not match task.json"
        )
    if isinstance(note, GeneratedNoteV4):
        try:
            template = load_template_snapshot(note_path.parent / "template.json")
        except (OSError, UnicodeError, ValidationError, ValueError):
            note_errors = ["V4 template snapshot is invalid"]
        else:
            note_errors = validate_v4_source_contract(
                note, content_pack, template=template
            )
            note_errors.extend(
                validate_v4_bundle_provenance(
                    note_path.parent,
                    note,
                    template,
                    content_pack_sha256=hashlib.sha256(pack_bytes).hexdigest(),
                )
            )
    else:
        note_errors = validate_generated_note(note, content_pack)
    if note_errors:
        raise ValueError(f"active note is invalid: {'; '.join(note_errors)}")
    return _PodcastContext(root, task, content_pack, note, note_bytes)


def _active_artifact(
    root: Path,
    task: TaskRecord,
    stage: str,
    filename: str,
) -> Path:
    path = next(
        (
            root / item
            for item in task.artifacts.get(stage, [])
            if Path(item).name == filename
        ),
        None,
    )
    if path is None:
        raise ValueError(f"task has no active {filename} artifact")
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"active {filename} artifact is missing or outside task")
    return resolved


def _parse_and_validate(
    raw: str,
    context: _PodcastContext,
) -> tuple[PodcastScript | None, tuple[str, ...]]:
    try:
        script = parse_podcast_script(raw)
    except ValidationError as error:
        return None, safe_podcast_schema_errors(error)
    return script, tuple(
        validate_podcast_script(
            script,
            context.content_pack,
            context.note_sha256,
        )
    )


def _build_podcast_bundle(
    context: _PodcastContext,
    raw: str,
    *,
    provider: str,
    model: str,
    run_id: str | None,
    attempt_count: int,
) -> Path:
    selected_run_id = run_id or _new_run_id(context.task)
    temporary, final = _prepare_bundle(context.task_dir, selected_run_id)
    script, errors = _parse_and_validate(raw, context)
    if script is None or errors:
        return _finish_failed(
            temporary,
            final,
            context,
            selected_run_id,
            provider,
            model,
            attempt_count,
            errors,
        )
    return _finish_success(
        temporary,
        final,
        context,
        selected_run_id,
        provider,
        model,
        attempt_count,
        script,
    )


def _prepare_bundle(task_dir: Path, run_id: str) -> tuple[Path, Path]:
    if not _RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must contain only safe filename characters")
    parent = task_dir / "generated_podcasts"
    temporary = parent / f".{run_id}"
    final = parent / run_id
    if temporary.exists() or final.exists():
        raise FileExistsError(f"podcast bundle already exists: {run_id}")
    parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    return temporary, final


def _finish_success(
    temporary: Path,
    final: Path,
    context: _PodcastContext,
    run_id: str,
    provider: str,
    model: str,
    attempt_count: int,
    script: PodcastScript,
) -> Path:
    _write_text(temporary / "podcast_script.json", script.model_dump_json(indent=2))
    _write_text(temporary / "speech.txt", render_podcast_speech(script))
    _write_metadata(
        temporary,
        run_id,
        provider,
        model,
        attempt_count,
        "completed",
        (),
        context.note_sha256,
    )
    os.replace(temporary, final)
    return final


def _finish_failed(
    temporary: Path,
    final: Path,
    context: _PodcastContext,
    run_id: str,
    provider: str,
    model: str,
    attempt_count: int,
    errors: tuple[str, ...],
) -> Path:
    _write_metadata(
        temporary,
        run_id,
        provider,
        model,
        attempt_count,
        "failed",
        errors,
        context.note_sha256,
    )
    os.replace(temporary, final)
    raise PodcastGenerationError(final, errors)


def _write_metadata(
    directory: Path,
    run_id: str,
    provider: str,
    model: str,
    attempt_count: int,
    status: str,
    errors: tuple[str, ...],
    note_sha256: str,
) -> None:
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "provider": provider,
        "model": model,
        "attempt_count": attempt_count,
        "status": status,
        "note_content_sha256": note_sha256,
        "errors": list(errors),
    }
    _write_text(
        directory / "generation.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _activate_podcast_bundle(
    task_dir: Path,
    bundle: Path,
    *,
    provider: str,
    model: str,
) -> TaskRecord:
    root = task_dir.resolve()
    resolved_bundle = bundle.resolve()
    if not resolved_bundle.is_relative_to(root):
        raise ValueError("podcast bundle must be inside task directory")
    required = ("podcast_script.json", "speech.txt")
    for filename in required:
        if not (resolved_bundle / filename).is_file():
            raise ValueError(f"podcast bundle is missing: {filename}")
    task = load_task(root)
    relative = resolved_bundle.relative_to(root)
    artifacts = {
        stage: paths
        for stage, paths in task.artifacts.items()
        if stage not in {"podcast_script", "tts"}
    }
    artifacts["podcast_script"] = [
        (relative / filename).as_posix() for filename in required
    ]
    providers = {
        stage: value for stage, value in task.providers.items() if stage != "tts"
    }
    models = {stage: value for stage, value in task.models.items() if stage != "tts"}
    updated = task.model_copy(
        update={
            "stages": {
                **task.stages,
                "podcast_script": StageStatus.COMPLETED,
                "tts": StageStatus.PENDING,
            },
            "artifacts": artifacts,
            "providers": {**providers, "podcast_script": provider},
            "models": {**models, "podcast_script": model},
            "error_summary": None,
        }
    )
    write_task_atomic(root, updated)
    return updated


def _refresh_note_links(
    task: TaskRecord,
    task_dir: Path,
    output_root: Path | None,
) -> TaskRecord:
    if output_root is None:
        return task
    from learnnest.note_generation import rerender_and_activate_note

    return rerender_and_activate_note(task_dir, output_root)


def _recover_note_link_publication(
    task_dir: Path,
    output_root: Path | None,
) -> TaskRecord | None:
    if output_root is None:
        return None
    task = load_task(task_dir)
    if task.stages.get("podcast_script") is not StageStatus.COMPLETED:
        return None
    if task.stages.get("publish") is StageStatus.RUNNING:
        return reconcile_pending_note_publication(task_dir, output_root)
    if task.stages.get("publish") is StageStatus.FAILED:
        from learnnest.note_generation import rerender_and_activate_note

        return rerender_and_activate_note(task_dir, output_root)
    return None


def _write_text(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        if not content.endswith("\n"):
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _new_run_id(task: TaskRecord) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{task.task_id[-8:]}"
