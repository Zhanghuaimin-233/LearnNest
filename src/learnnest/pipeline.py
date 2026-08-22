"""File-oriented orchestration for the local video evidence pipeline."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageChops, ImageStat
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from learnnest import providers  # noqa: F401 - retained as the worker test seam
from learnnest.downloader import YtDlpDownloader
from learnnest.execution import (
    begin_attempt,
    classify_failure,
    ensure_stage_attempts_available,
    finish_attempt,
    next_retry_time,
    plan_recovery,
    record_stage_attempt,
)
from learnnest.execution_models import AttemptReason, SourceIdentities
from learnnest.identities import source_identities, stream_sha256
from learnnest.locks import identity_lock, task_lock
from learnnest.material_adapters import (
    AsrAdapter,
    MaterialAdapters,
    OcrAdapter,
    material_adapters_from_bindings,
)
from learnnest.models import (
    ContentPack,
    Evidence,
    StageName,
    StageStatus,
    TaskProfile,
    TaskRecord,
    TranscriptSegment,
)
from learnnest.publication import atomic_replace_bytes, note_belongs_to_task
from learnnest.provider_profiles import freeze_role_bindings
from learnnest.rendering import (
    render_note,
    render_note_manifest,
    render_report,
    render_trace,
)
from learnnest.scheduler import ResourceScheduler
from learnnest.source_models import AcquiredSource, SourceItem
from learnnest.stages import STAGES, stage_artifacts
from learnnest.subtitles import SubtitleError, parse_subtitle_file
from learnnest.task_store import (
    create_task,
    find_task_by_id,
    find_task_by_identities,
    load_task,
    write_task_atomic,
)
from learnnest.util import (
    safe_title,
    source_fingerprint,
    task_directory_name,
    url_source_fingerprint,
)
from learnnest.validation import validate_task

Profile = TaskProfile
InitialAttemptReason = Literal["initial", "scheduled"]
_STAGES = STAGES
_OPERATION_KEYWORDS = ("打开", "点击", "选择", "设置", "输入", "保存", "拖动")
_MAX_DURATION_MS = 30 * 60 * 1_000
_MAX_SELECTED_FRAMES = 15


class PipelineError(RuntimeError):
    """A user-facing failure that leaves task state and a report behind."""


class _OcrItem(BaseModel):
    """The minimum OCR worker item contract consumed by the evidence stage."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class _AsrPayload(BaseModel):
    """The ASR worker file contract consumed by the transcript stage."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    language: str | None = None
    segments: list[TranscriptSegment]


class _OcrPayload(BaseModel):
    """The OCR worker file contract consumed by the OCR stage."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)
    items: list[_OcrItem]


def process_video(
    video_path: str | Path,
    output_root: str | Path,
    profile: Profile = "evidence",
    *,
    force: bool = False,
    allow_duplicate: bool = False,
    scheduler: Any | None = None,
    task_lock_timeout: float = 0.0,
    batch_id: str | None = None,
    on_attempt_started: Callable[[TaskRecord], None] | None = None,
    material_adapters: MaterialAdapters | None = None,
) -> TaskRecord:
    """Compatibility wrapper for one local video source."""
    source = Path(video_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise PipelineError(f"video path is not a file: {source}")
    return process_source(
        SourceItem(input=str(source), input_type="local_file"),
        output_root,
        profile,
        force=force,
        allow_duplicate=allow_duplicate,
        scheduler=scheduler,
        task_lock_timeout=task_lock_timeout,
        batch_id=batch_id,
        on_attempt_started=on_attempt_started,
        material_adapters=material_adapters,
    )


def _find_existing_task(
    output_root: Path,
    identities: SourceIdentities,
    source_fingerprint: str,
    *,
    force: bool,
    allow_duplicate: bool,
) -> tuple[Path, TaskRecord] | None:
    exact = find_task_by_identities(
        output_root,
        identities,
        source_fingerprint,
        include_derived=False,
    )
    if exact is not None or allow_duplicate:
        return exact
    completed = find_task_by_identities(
        output_root,
        identities,
        source_fingerprint,
        include_derived=True,
        require_completed=True,
    )
    if completed is not None:
        return completed
    return find_task_by_identities(
        output_root,
        identities,
        source_fingerprint,
        include_derived=True,
    )


def process_source(
    source_item: SourceItem,
    output_root: str | Path,
    profile: Profile = "evidence",
    *,
    downloader: Any | None = None,
    force: bool = False,
    allow_duplicate: bool = False,
    scheduler: Any | None = None,
    task_lock_timeout: float = 0.0,
    batch_id: str | None = None,
    on_attempt_started: Callable[[TaskRecord], None] | None = None,
    initial_attempt_reason: InitialAttemptReason = "initial",
    material_adapters: MaterialAdapters | None = None,
    _task_lock_held: bool = False,
    _admission_fingerprint: str | None = None,
    _admission_identities: SourceIdentities | None = None,
) -> TaskRecord:
    """Run one normalized local or URL source through the deterministic pipeline."""
    if profile not in {"evidence", "note", "full"}:
        raise ValueError(f"unknown profile: {profile}")
    if not _task_lock_held:
        root = Path(output_root).resolve()
        if source_item.input_type == "local_file":
            candidate = Path(source_item.input).expanduser().resolve(strict=True)
            if not candidate.is_file():
                raise PipelineError(f"video path is not a file: {candidate}")
            pre_fingerprint = source_fingerprint(candidate)
            pre_identities = source_identities(
                source_item,
                content_sha256=stream_sha256(candidate),
            )
        else:
            pre_fingerprint = url_source_fingerprint(source_item.input)
            pre_identities = source_identities(source_item)
        admission_key = (
            f"content:{pre_identities.content_sha256}"
            if not allow_duplicate and pre_identities.content_sha256 is not None
            else f"source:{pre_fingerprint}"
        )
        with identity_lock(root, admission_key, timeout=task_lock_timeout):
            existing = _find_existing_task(
                root,
                pre_identities,
                pre_fingerprint,
                force=force,
                allow_duplicate=allow_duplicate,
            )
            if existing is None:
                lock_task_id = f"{datetime.now():%Y%m%d}-{pre_fingerprint[:8]}"
            else:
                _, canonical = _resolve_canonical_task(root, *existing)
                lock_task_id = canonical.task_id
            with task_lock(root, lock_task_id, timeout=task_lock_timeout):
                return process_source(
                    source_item,
                    root,
                    profile,
                    downloader=downloader,
                    force=force,
                    allow_duplicate=allow_duplicate,
                    scheduler=scheduler,
                    task_lock_timeout=task_lock_timeout,
                    batch_id=batch_id,
                    on_attempt_started=on_attempt_started,
                    initial_attempt_reason=initial_attempt_reason,
                    material_adapters=material_adapters,
                    _task_lock_held=True,
                    _admission_fingerprint=pre_fingerprint,
                    _admission_identities=pre_identities,
                )
    if source_item.input_type == "local_file":
        source = Path(source_item.input).expanduser().resolve(strict=True)
        if not source.is_file():
            raise PipelineError(f"video path is not a file: {source}")
        fingerprint = _admission_fingerprint or source_fingerprint(source)
        title = safe_title(source_item.title or source.stem)
        media_path: str | None = str(source)
        identities = _admission_identities or source_identities(
            source_item, content_sha256=stream_sha256(source)
        )
    else:
        source = None
        fingerprint = _admission_fingerprint or url_source_fingerprint(
            source_item.input
        )
        title = safe_title(source_item.title or f"url-{fingerprint[:8]}")
        media_path = None
        identities = _admission_identities or source_identities(source_item)

    root = Path(output_root).resolve()
    selected_scheduler = scheduler or ResourceScheduler(root)
    existing = _find_existing_task(
        root,
        identities,
        fingerprint,
        force=force,
        allow_duplicate=allow_duplicate,
    )
    if existing is not None:
        canonical_dir, canonical = _resolve_canonical_task(root, *existing)
        if force:
            if source_item.input_type == "local_file":
                canonical = canonical.model_copy(
                    update={
                        "source_path": source_item.input,
                        "source_input": source_item.input,
                        "media_path": media_path,
                        "source_fingerprint": fingerprint,
                        "identities": identities,
                    }
                )
                write_task_atomic(canonical_dir, canonical)
            return rerun_task(
                canonical_dir,
                "source",
                reason="force",
                downloader=downloader,
                allow_duplicate=allow_duplicate,
                scheduler=selected_scheduler,
                batch_id=batch_id,
                on_attempt_started=on_attempt_started,
                _task_lock_held=True,
            )
        if _is_completed_task(canonical):
            return canonical
        raise PipelineError(
            f"task {canonical.task_id} already exists but is incomplete or failed; "
            f"use `learnnest recover {canonical.task_id}` or "
            f"`learnnest retry {canonical.task_id} --from source`"
        )

    task_id = f"{datetime.now():%Y%m%d}-{fingerprint[:8]}"
    task_dir = root / "视频学习素材" / task_directory_name(title, task_id)
    if (task_dir / "task.json").exists():
        raise PipelineError(
            f"task directory already exists for another task: {task_dir.name}"
        )
    task = create_task(
        task_id=task_id,
        source_path=source_item.input,
        source_input=source_item.input,
        source_type=source_item.input_type,
        media_path=media_path,
        source_fingerprint=fingerprint,
        title=title,
        profile=profile,
        note_type_override=source_item.note_type,
        provider_bindings=freeze_role_bindings(root),
    ).model_copy(
        update={
            "identities": identities,
            "stages": {stage: StageStatus.PENDING for stage in _STAGES},
        }
    )
    task = begin_attempt(
        task,
        reason=initial_attempt_reason,
        from_stage="source",
        now=datetime.now(UTC),
        batch_id=batch_id,
    )
    write_task_atomic(task_dir, task)
    if on_attempt_started is not None:
        on_attempt_started(task)

    try:
        if source is None:
            selected_downloader = downloader or YtDlpDownloader()
            source, duplicate = _run_stage(
                task_dir,
                task,
                "source",
                lambda: _acquire_url_source(
                    task_dir,
                    source_item,
                    fingerprint,
                    root,
                    selected_downloader,
                    allow_duplicate=allow_duplicate,
                    scheduler=selected_scheduler,
                ),
            )
            if duplicate is not None:
                canonical_dir, canonical = _resolve_canonical_task(root, *duplicate)
                del canonical_dir
                current = load_task(task_dir)
                current = _skip_stages(
                    task_dir,
                    current,
                    tuple(stage for stage in _STAGES if stage != "source"),
                )
                current = finish_attempt(
                    current,
                    status="skipped_duplicate",
                    now=datetime.now(UTC),
                )
                write_task_atomic(task_dir, current)
                _write_report(task_dir)
                return canonical
            task = load_task(task_dir)
            result = _run_from_stage(
                task_dir,
                task,
                source,
                output_root,
                profile,
                _STAGES.index("transcript"),
                selected_scheduler,
                material_adapters,
            )
        else:
            result = _run_from_stage(
                task_dir,
                task,
                source,
                output_root,
                profile,
                0,
                selected_scheduler,
                material_adapters,
            )
        return _finish_active_attempt(task_dir, result)
    except Exception as error:
        _record_failure(task_dir, error)
        if isinstance(error, PipelineError):
            raise
        raise PipelineError(str(error)) from error


def _resolve_acquired_media_path(task_dir: Path, media_path: str) -> Path:
    candidate = Path(media_path)
    if not candidate.is_absolute():
        candidate = task_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise PipelineError(f"task media_path is missing: {media_path}") from error
    if not resolved.is_file():
        raise PipelineError(f"task media_path is not a file: {media_path}")
    return resolved


def _acquire_url_source(
    task_dir: Path,
    source_item: SourceItem,
    fingerprint: str,
    output_root: Path,
    downloader: Any,
    *,
    allow_duplicate: bool,
    scheduler: Any,
) -> tuple[Path, tuple[Path, TaskRecord] | None]:
    """Acquire URL media, persist discovered identities, and detect duplicates."""
    with scheduler.acquire("network"):
        with scheduler.acquire("ffmpeg"):
            acquired = downloader.acquire(
                source_item,
                task_dir,
                source_fingerprint=fingerprint,
            )
    if acquired.source_fingerprint != fingerprint:
        raise PipelineError("acquired source fingerprint changed")
    media = _resolve_acquired_media_path(task_dir, acquired.media_path)
    discovered = source_identities(
        source_item,
        content_sha256=stream_sha256(media),
        platform="yt-dlp" if acquired.platform_id is not None else None,
        platform_id=acquired.platform_id,
    )
    current = load_task(task_dir).model_copy(
        update={
            "media_path": acquired.media_path,
            "identities": discovered,
        }
    )
    write_task_atomic(task_dir, current)
    _write_source(
        task_dir,
        current,
        media,
        acquired=acquired,
        scheduler=scheduler,
    )
    duplicate = find_task_by_identities(
        output_root,
        discovered,
        fingerprint,
        include_derived=not allow_duplicate,
        require_completed=True,
        exclude_task_id=current.task_id,
    )
    if duplicate is not None:
        _, canonical = _resolve_canonical_task(output_root, *duplicate)
        current = load_task(task_dir).model_copy(
            update={"duplicate_of_task_id": canonical.task_id}
        )
        write_task_atomic(task_dir, current)
    return media, duplicate


def _resolve_canonical_task(
    output_root: Path,
    task_dir: Path,
    task: TaskRecord,
) -> tuple[Path, TaskRecord]:
    if task.duplicate_of_task_id is None:
        return task_dir, task
    canonical = find_task_by_id(output_root, task.duplicate_of_task_id)
    return canonical if canonical is not None else (task_dir, task)


def _is_completed_task(task: TaskRecord) -> bool:
    if task.active_attempt_id is not None:
        return False
    if task.attempts:
        return task.attempts[-1].status == "completed"
    return task.stages.get("content_pack") is StageStatus.COMPLETED


def rerun_task(
    task_dir: str | Path,
    from_stage: StageName,
    *,
    reason: AttemptReason = "retry",
    downloader: Any | None = None,
    allow_duplicate: bool = False,
    scheduler: Any | None = None,
    task_lock_timeout: float = 0.0,
    batch_id: str | None = None,
    on_attempt_started: Callable[[TaskRecord], None] | None = None,
    max_stage_attempts: int = 4,
    _task_lock_held: bool = False,
) -> TaskRecord:
    """Re-run one persisted stage and every following applicable stage."""
    root = Path(task_dir).resolve()
    if not _task_lock_held:
        task = load_task(root)
        with task_lock(root.parents[1], task.task_id, timeout=task_lock_timeout):
            return rerun_task(
                root,
                from_stage,
                reason=reason,
                downloader=downloader,
                allow_duplicate=allow_duplicate,
                scheduler=scheduler,
                task_lock_timeout=task_lock_timeout,
                batch_id=batch_id,
                on_attempt_started=on_attempt_started,
                max_stage_attempts=max_stage_attempts,
                _task_lock_held=True,
            )
    try:
        start_index = _STAGES.index(from_stage)
    except ValueError as error:
        raise ValueError(f"unknown stage: {from_stage}") from error
    task = load_task(root)
    if task.active_attempt_id is not None:
        task = finish_attempt(
            task,
            status="interrupted",
            now=datetime.now(UTC),
        )
        write_task_atomic(root, task)
    profile: Profile = task.profile
    stages = {
        **task.stages,
        **{stage: StageStatus.PENDING for stage in _STAGES[start_index:]},
    }
    artifacts = {
        stage: paths
        for stage, paths in task.artifacts.items()
        if _STAGES.index(stage) < start_index
    }
    reset = task.model_copy(
        update={"stages": stages, "artifacts": artifacts, "error_summary": None}
    )
    ensure_stage_attempts_available(
        reset,
        _runnable_stages(reset, from_stage),
        max_attempts=max_stage_attempts,
    )
    reset = begin_attempt(
        reset,
        reason=reason,
        from_stage=from_stage,
        now=datetime.now(UTC),
        batch_id=batch_id,
    )
    write_task_atomic(root, reset)
    if on_attempt_started is not None:
        on_attempt_started(reset)
    _write_report(root)
    try:
        output_root = root.parents[1]
        selected_scheduler = scheduler or ResourceScheduler(output_root)
        if from_stage == "source" and reset.source_type == "url":
            source_item = SourceItem(
                input=reset.source_input or reset.source_path,
                input_type="url",
            )
            source, duplicate = _run_stage(
                root,
                reset,
                "source",
                lambda: _acquire_url_source(
                    root,
                    source_item,
                    reset.source_fingerprint,
                    output_root,
                    downloader or YtDlpDownloader(),
                    allow_duplicate=allow_duplicate,
                    scheduler=selected_scheduler,
                ),
            )
            if duplicate is not None:
                _, canonical = _resolve_canonical_task(output_root, *duplicate)
                current = load_task(root)
                current = _skip_stages(
                    root,
                    current,
                    tuple(stage for stage in _STAGES if stage != "source"),
                )
                current = finish_attempt(
                    current,
                    status="skipped_duplicate",
                    now=datetime.now(UTC),
                )
                write_task_atomic(root, current)
                _write_report(root)
                return canonical
            reset = load_task(root)
            result = _run_from_stage(
                root,
                reset,
                source,
                output_root,
                profile,
                _STAGES.index("transcript"),
                selected_scheduler,
            )
        else:
            result = _run_from_stage(
                root,
                reset,
                _resolve_task_media_path(root, reset),
                output_root,
                profile,
                start_index,
                selected_scheduler,
            )
        return _finish_active_attempt(root, result)
    except Exception as error:
        if not _failure_already_recorded(root, error):
            _record_failure(root, error, from_stage)
        if isinstance(error, PipelineError):
            raise
        raise PipelineError(str(error)) from error


def recover_task(
    task_dir: str | Path,
    *,
    downloader: Any | None = None,
    scheduler: Any | None = None,
    task_lock_timeout: float = 0.0,
    batch_id: str | None = None,
    on_attempt_started: Callable[[TaskRecord], None] | None = None,
    reason: AttemptReason = "resume",
    max_stage_attempts: int = 4,
) -> TaskRecord:
    """Plan and execute one free recovery inside a single task lock."""
    root = Path(task_dir).resolve()
    task_id = load_task(root).task_id
    output_root = root.parents[1]
    with task_lock(output_root, task_id, timeout=task_lock_timeout):
        current = load_task(root)
        plan = plan_recovery(root)
        if plan.from_stage is None:
            return current
        if plan.requires_paid:
            raise PipelineError(
                "batch recovery reached a paid stage; explicit command required"
            )
        return rerun_task(
            root,
            plan.from_stage,
            reason=reason,
            downloader=downloader,
            scheduler=scheduler,
            batch_id=batch_id,
            on_attempt_started=on_attempt_started,
            max_stage_attempts=max_stage_attempts,
            _task_lock_held=True,
        )


def _run_from_stage(
    task_dir: Path,
    task: TaskRecord,
    source: Path,
    output_root: str | Path,
    profile: Profile,
    start_index: int,
    scheduler: Any,
    material_adapters: MaterialAdapters | None = None,
) -> TaskRecord:
    selected_material_adapters = material_adapters or material_adapters_from_bindings(
        task.provider_bindings
    )
    if start_index <= _STAGES.index("source"):
        probe = _run_stage(
            task_dir,
            task,
            "source",
            lambda: _write_source(
                task_dir,
                task,
                source,
                scheduler=scheduler,
            ),
        )
    else:
        probe = _read_json(task_dir / "source.json")["probe"]
    task = load_task(task_dir)

    if start_index <= _STAGES.index("transcript"):
        transcript_payload = _run_stage(
            task_dir,
            task,
            "transcript",
            lambda: _transcribe_source(
                source, task_dir, scheduler, selected_material_adapters.asr
            ),
        )
    else:
        transcript_payload = _load_transcript_payload(task_dir)
    task = load_task(task_dir)
    task = _record_provider(task_dir, task, "asr", transcript_payload)

    if start_index <= _STAGES.index("frames"):
        frame_records = _run_stage(
            task_dir,
            task,
            "frames",
            lambda: _resource_call(
                scheduler,
                "ffmpeg",
                lambda: _extract_frames(
                    source,
                    task_dir,
                    transcript_payload,
                    _frame_duration_ms(probe),
                ),
            ),
        )
    else:
        frame_records = list(_read_json(task_dir / "frames.json").get("frames", []))
    task = load_task(task_dir)

    if start_index <= _STAGES.index("ocr"):
        ocr_payload = _run_stage(
            task_dir,
            task,
            "ocr",
            lambda: _resource_call(
                scheduler,
                "ocr",
                lambda: _recognize_frames(
                    task_dir, frame_records, selected_material_adapters.ocr
                ),
            ),
        )
    else:
        ocr_payload = _load_ocr_payload(task_dir)
    task = load_task(task_dir)
    task = _record_provider(task_dir, task, "ocr", ocr_payload)

    if start_index <= _STAGES.index("evidence"):
        content_pack = _run_stage(
            task_dir,
            task,
            "evidence",
            lambda: _write_evidence(
                task_dir, task, transcript_payload, frame_records, ocr_payload
            ),
        )
    else:
        content_pack = ContentPack.model_validate_json(
            (task_dir / "content_pack.json").read_text(encoding="utf-8")
        )
    task = load_task(task_dir)

    if start_index <= _STAGES.index("content_pack"):
        _run_stage(
            task_dir,
            task,
            "content_pack",
            lambda: _write_content_pack(task_dir, content_pack),
        )
    task = load_task(task_dir)
    if profile == "evidence":
        _skip_stages(task_dir, task, ("note", "publish", "podcast_script", "tts"))
        return _validate_success(task_dir, "content_pack")

    if start_index <= _STAGES.index("note"):
        _run_stage(
            task_dir, task, "note", lambda: _write_note(task_dir, task, content_pack)
        )
    task = load_task(task_dir)
    if start_index <= _STAGES.index("publish"):
        _run_stage(
            task_dir,
            task,
            "publish",
            lambda: _publish_note(task_dir, task, output_root),
        )
    task = load_task(task_dir)
    _skip_stages(task_dir, task, ("podcast_script", "tts"))
    return _validate_success(task_dir, "publish")


def probe_video(video_path: Path) -> dict[str, Any]:
    """Read stream metadata through ffprobe without loading media libraries."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,duration",
        "-of",
        "json",
        str(video_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise PipelineError(
            result.stderr.strip() or "ffprobe could not inspect the video"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PipelineError("ffprobe produced invalid JSON") from error


def extract_frame(*, video_path: Path, timestamp_ms: int, output_path: Path) -> None:
    """Extract exactly one candidate frame through ffmpeg."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{timestamp_ms / 1_000:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        str(output_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or "ffmpeg could not extract a frame")
    if not output_path.is_file():
        raise PipelineError(f"ffmpeg did not create frame: {output_path.name}")


def candidate_timestamps(
    *, duration_ms: int, transcript_segments: list[dict[str, Any]]
) -> list[int]:
    """Return stable candidate moments: first frame, ten-second steps and operations."""
    timestamps = set(range(0, max(duration_ms, 1), 10_000))
    for segment in transcript_segments:
        text = segment.get("text")
        start_ms = segment.get("start_ms")
        if (
            isinstance(text, str)
            and isinstance(start_ms, int)
            and any(keyword in text for keyword in _OPERATION_KEYWORDS)
        ):
            timestamps.add(max(0, min(start_ms, max(duration_ms - 1, 0))))
    return sorted(timestamp for timestamp in timestamps if timestamp < duration_ms)


def build_content_pack(
    task: TaskRecord,
    transcript_payload: dict[str, Any],
    frame_records: list[dict[str, Any]],
    ocr_payload: dict[str, Any],
) -> ContentPack:
    """Aggregate source-typed evidence while preserving frame/OCR links."""
    evidence: list[Evidence] = []
    for segment in transcript_payload.get("segments", []):
        evidence.append(
            Evidence(
                id=str(segment["id"]),
                kind="transcript",
                start_ms=int(segment["start_ms"]),
                end_ms=int(segment["end_ms"]),
                text=str(segment["text"]),
                artifact_path="content_pack.json",
            )
        )

    ocr_by_frame = {
        str(item["frame_id"]): item for item in ocr_payload.get("frames", [])
    }
    for frame in frame_records:
        frame_id = str(frame["id"])
        ocr_items = ocr_by_frame.get(frame_id, {}).get("items", [])
        ocr_ids = [
            f"ocr_{len([item for item in evidence if item.kind == 'ocr']) + index + 1:04d}"
            for index, _ in enumerate(ocr_items)
        ]
        evidence.append(
            Evidence(
                id=frame_id,
                kind="frame",
                start_ms=int(frame["timestamp_ms"]),
                artifact_path=str(frame["artifact_path"]),
                related_evidence_ids=ocr_ids,
            )
        )
        for ocr_id, item in zip(ocr_ids, ocr_items, strict=True):
            evidence.append(
                Evidence(
                    id=ocr_id,
                    kind="ocr",
                    text=str(item["text"]),
                    confidence=float(item["confidence"])
                    if item.get("confidence") is not None
                    else None,
                    artifact_path="content_pack.json",
                    frame_id=frame_id,
                )
            )
    return ContentPack(
        task_id=task.task_id,
        source_fingerprint=task.source_fingerprint,
        evidence=evidence,
    )


def _resource_call(scheduler: Any, resource: str, action: Any) -> Any:
    with scheduler.acquire(resource):
        return action()


def _write_source(
    task_dir: Path,
    task: TaskRecord,
    source: Path,
    *,
    acquired: AcquiredSource | None = None,
    scheduler: Any,
) -> dict[str, Any]:
    probe = _resource_call(scheduler, "ffmpeg", lambda: probe_video(source))
    _validate_probe(probe)
    payload = {
        "schema_version": "1.0",
        "task_id": task.task_id,
        "source_path": str(source),
        "source_input": task.source_input,
        "source_type": task.source_type,
        "media_path": task.media_path,
        "source_fingerprint": task.source_fingerprint,
        "platform_id": acquired.platform_id if acquired is not None else None,
        "platform_title": acquired.platform_title if acquired is not None else None,
        "subtitle_path": acquired.subtitle_path if acquired is not None else None,
        "identities": task.identities.model_dump(mode="json")
        if task.identities
        else None,
        "probe": probe,
    }
    _write_json(task_dir / "source.json", payload)
    return probe


def _resolve_task_media_path(task_dir: Path, task: TaskRecord) -> Path:
    if task.media_path is None:
        raise PipelineError("task has no acquired media_path")
    candidate = Path(task.media_path)
    if not candidate.is_absolute():
        candidate = task_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise PipelineError(f"task media_path is missing: {task.media_path}") from error
    if not resolved.is_file():
        raise PipelineError(f"task media_path is not a file: {task.media_path}")
    return resolved


def _transcribe(
    source: Path, task_dir: Path, scheduler: Any, adapter: AsrAdapter
) -> dict[str, Any]:
    output = task_dir / "transcript.json"
    _resource_call(
        scheduler,
        "asr",
        lambda: adapter.transcribe(source, output),
    )
    payload = _read_json(output)
    _validate_asr_payload(payload)
    _write_transcript_markdown(task_dir / "transcript.md", payload)
    return payload


def _transcribe_source(
    source: Path, task_dir: Path, scheduler: Any, adapter: AsrAdapter
) -> dict[str, Any]:
    source_payload = _read_json(task_dir / "source.json")
    subtitle_path = source_payload.get("subtitle_path")
    if isinstance(subtitle_path, str) and subtitle_path:
        candidate = (task_dir / subtitle_path).resolve()
        if not candidate.is_relative_to(task_dir.resolve()):
            raise PipelineError("platform subtitle path escapes task directory")
        if candidate.is_file():
            try:
                payload = parse_subtitle_file(candidate)
            except SubtitleError:
                pass
            else:
                _validate_asr_payload(payload)
                _write_json(task_dir / "transcript.json", payload)
                _write_transcript_markdown(task_dir / "transcript.md", payload)
                return payload
    return _transcribe(source, task_dir, scheduler, adapter)


def _extract_frames(
    source: Path, task_dir: Path, transcript_payload: dict[str, Any], duration_ms: int
) -> list[dict[str, Any]]:
    candidates_dir = task_dir / "frames" / "candidates"
    selected_dir = task_dir / "frames" / "selected"
    shutil.rmtree(candidates_dir, ignore_errors=True)
    shutil.rmtree(selected_dir, ignore_errors=True)
    candidates: list[tuple[int, Path]] = []
    for timestamp_ms in candidate_timestamps(
        duration_ms=duration_ms,
        transcript_segments=list(transcript_payload.get("segments", [])),
    ):
        candidate = candidates_dir / f"candidate_{timestamp_ms:010d}.png"
        extract_frame(
            video_path=source, timestamp_ms=timestamp_ms, output_path=candidate
        )
        candidates.append((timestamp_ms, candidate))

    selected = _select_distinct_frames(candidates)
    records: list[dict[str, Any]] = []
    for index, (timestamp_ms, candidate) in enumerate(selected, start=1):
        frame_id = f"fr_{index:04d}"
        selected_path = selected_dir / f"{frame_id}.png"
        selected_path.parent.mkdir(parents=True, exist_ok=True)
        candidate.replace(selected_path)
        records.append(
            {
                "id": frame_id,
                "timestamp_ms": timestamp_ms,
                "artifact_path": selected_path.relative_to(task_dir).as_posix(),
            }
        )
    _write_json(task_dir / "frames.json", {"schema_version": "1.0", "frames": records})
    shutil.rmtree(candidates_dir, ignore_errors=True)
    return records


def _recognize_frames(
    task_dir: Path,
    frame_records: list[dict[str, Any]],
    adapter: OcrAdapter,
) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    provider_names: set[str] = set()
    model_names: set[str] = set()
    for frame in frame_records:
        output = task_dir / "ocr" / f"{frame['id']}.json"
        adapter.recognize(task_dir / str(frame["artifact_path"]), output)
        worker_payload = _read_json(output)
        _validate_ocr_payload(worker_payload)
        provider_names.add(str(worker_payload["provider"]))
        if worker_payload.get("model"):
            model_names.add(str(worker_payload["model"]))
        frames.append({"frame_id": frame["id"], "items": worker_payload["items"]})
    if len(provider_names) != 1 or len(model_names) > 1:
        raise PipelineError("OCR frames were produced by inconsistent adapters")
    payload = {
        "schema_version": "1.0",
        "provider": next(iter(provider_names)),
        "frames": frames,
    }
    if model_names:
        payload["model"] = next(iter(model_names))
    _write_json(task_dir / "ocr.json", payload)
    _write_ocr_markdown(task_dir / "ocr.md", payload)
    return payload


def _write_evidence(
    task_dir: Path,
    task: TaskRecord,
    transcript_payload: dict[str, Any],
    frame_records: list[dict[str, Any]],
    ocr_payload: dict[str, Any],
) -> ContentPack:
    content_pack = build_content_pack(
        task, transcript_payload, frame_records, ocr_payload
    )
    return content_pack


def _write_content_pack(task_dir: Path, content_pack: ContentPack) -> None:
    _write_json(task_dir / "content_pack.json", content_pack.model_dump(mode="json"))
    (task_dir / "trace.md").write_text(render_trace(content_pack), encoding="utf-8")
    for filename in (
        "transcript.json",
        "transcript.md",
        "ocr.json",
        "ocr.md",
        "evidence.json",
        "evidence.md",
    ):
        (task_dir / filename).unlink(missing_ok=True)
    shutil.rmtree(task_dir / "ocr", ignore_errors=True)


def _load_transcript_payload(task_dir: Path) -> dict[str, Any]:
    path = task_dir / "transcript.json"
    if path.is_file():
        return _read_json(path)
    pack = ContentPack.model_validate_json(
        (task_dir / "content_pack.json").read_text(encoding="utf-8")
    )
    return {
        "schema_version": "1.0",
        "segments": [
            {
                "id": item.id,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "text": item.text,
                "confidence": item.confidence,
            }
            for item in pack.evidence
            if item.kind == "transcript"
        ],
    }


def _load_ocr_payload(task_dir: Path) -> dict[str, Any]:
    path = task_dir / "ocr.json"
    if path.is_file():
        return _read_json(path)
    pack = ContentPack.model_validate_json(
        (task_dir / "content_pack.json").read_text(encoding="utf-8")
    )
    frames: dict[str, list[dict[str, Any]]] = {}
    for item in pack.evidence:
        if item.kind != "ocr" or item.frame_id is None:
            continue
        frames.setdefault(item.frame_id, []).append(
            {
                "id": item.id,
                "text": item.text,
                "confidence": item.confidence,
            }
        )
    return {
        "schema_version": "1.0",
        "provider": "paddleocr",
        "frames": [
            {"frame_id": frame_id, "items": items} for frame_id, items in frames.items()
        ],
    }


def _write_note(task_dir: Path, task: TaskRecord, content_pack: ContentPack) -> None:
    asset_prefix = task_dir.relative_to(task_dir.parents[1]).as_posix()
    (task_dir / "note.md").write_text(
        render_note(task, content_pack, asset_prefix=asset_prefix),
        encoding="utf-8",
    )
    _write_json(task_dir / "note.json", render_note_manifest(task, content_pack))


def _publish_note(task_dir: Path, task: TaskRecord, output_root: str | Path) -> None:
    notes_dir = Path(output_root).resolve() / "视频学习笔记"
    base = notes_dir / f"{safe_title(task.title)}.md"
    hashed = notes_dir / f"{safe_title(task.title)}--{task.task_id[-8:]}.md"
    base_owned = base.exists() and note_belongs_to_task(base, task.task_id)
    hashed_owned = hashed.exists() and note_belongs_to_task(hashed, task.task_id)
    if base_owned and hashed_owned:
        raise PipelineError(
            f"duplicate published notes belong to task {task.task_id}; "
            "manual cleanup is required"
        )
    if base_owned:
        destination = base
    elif not base.exists() and hashed_owned:
        destination = hashed
    elif not base.exists():
        destination = base
    else:
        destination = hashed
        if destination.exists() and not hashed_owned:
            raise PipelineError(
                f"published note path belongs to another task: {destination.name}"
            )
    note_bytes = (task_dir / "note.md").read_bytes()
    atomic_replace_bytes(destination, note_bytes)
    try:
        (task_dir / "published_note.md").unlink(missing_ok=True)
    except OSError:
        # Publication already succeeded; an old compatibility snapshot must
        # never turn a completed Vault replacement into a failed stage.
        pass


def _run_stage(task_dir: Path, task: TaskRecord, stage: StageName, action: Any) -> Any:
    current = record_stage_attempt(task_dir, load_task(task_dir), stage)
    _set_stage(task_dir, current, stage, StageStatus.RUNNING)
    result = action()
    task = load_task(task_dir)
    artifacts = _stage_artifacts(stage)
    _complete_stage(task_dir, task, stage, artifacts)
    return result


def _runnable_stages(task: TaskRecord, from_stage: StageName) -> tuple[StageName, ...]:
    start = _STAGES.index(from_stage)
    end_stage = "content_pack" if task.profile == "evidence" else "publish"
    end = _STAGES.index(end_stage)
    if start > end:
        return ()
    return _STAGES[start : end + 1]


def _stage_artifacts(stage: StageName) -> list[str]:
    return list(stage_artifacts(stage))


def _set_stage(
    task_dir: Path, task: TaskRecord, stage: StageName, status: StageStatus
) -> None:
    stages = {**task.stages, stage: status}
    write_task_atomic(
        task_dir, task.model_copy(update={"stages": stages, "error_summary": None})
    )


def _complete_stage(
    task_dir: Path, task: TaskRecord, stage: StageName, artifacts: list[str]
) -> None:
    stages = {**task.stages, stage: StageStatus.COMPLETED}
    stage_artifacts = {**task.artifacts, stage: artifacts}
    write_task_atomic(
        task_dir,
        task.model_copy(update={"stages": stages, "artifacts": stage_artifacts}),
    )
    _write_report(task_dir)


def _skip_stages(
    task_dir: Path, task: TaskRecord, stages_to_skip: tuple[StageName, ...]
) -> TaskRecord:
    stages = {**task.stages, **{stage: StageStatus.SKIPPED for stage in stages_to_skip}}
    updated = task.model_copy(update={"stages": stages})
    write_task_atomic(task_dir, updated)
    _write_report(task_dir)
    return updated


def _record_failure(
    task_dir: Path, error: Exception, failed_stage: StageName | None = None
) -> None:
    try:
        task = load_task(task_dir)
    except (OSError, ValueError):
        return
    running = next(
        (
            stage
            for stage, status in task.stages.items()
            if status is StageStatus.RUNNING
        ),
        None,
    )
    stages = {**task.stages}
    if running is not None:
        stages[running] = StageStatus.FAILED
    elif failed_stage is not None:
        stages[failed_stage] = StageStatus.FAILED
    failed_stage_name = running or failed_stage
    failure = classify_failure(error)
    updated = task.model_copy(
        update={"stages": stages, "error_summary": failure.safe_summary}
    )
    if updated.active_attempt_id is not None:
        failed_at = datetime.now(UTC)
        updated = finish_attempt(
            updated,
            status="failed",
            now=failed_at,
            failed_stage=failed_stage_name,
            failure=failure,
            next_retry_at=next_retry_time(
                updated,
                failure,
                now=failed_at,
                stage=failed_stage_name,
            ),
        )
    write_task_atomic(task_dir, updated)
    _write_report(task_dir)


def _failure_already_recorded(task_dir: Path, error: Exception) -> bool:
    try:
        task = load_task(task_dir)
    except (OSError, ValueError):
        return False
    return task.error_summary == classify_failure(error).safe_summary and any(
        status is StageStatus.FAILED for status in task.stages.values()
    )


def _finish_active_attempt(task_dir: Path, task: TaskRecord) -> TaskRecord:
    if task.active_attempt_id is None:
        return task
    completed = finish_attempt(
        task,
        status="completed",
        now=datetime.now(UTC),
    )
    write_task_atomic(task_dir, completed)
    _write_report(task_dir)
    return completed


def _validate_success(task_dir: Path, final_stage: StageName) -> TaskRecord:
    errors = validate_task(task_dir)
    if errors:
        error = PipelineError(f"task validation failed: {'; '.join(errors)}")
        _record_failure(task_dir, error, final_stage)
        raise error
    return load_task(task_dir)


def _record_provider(
    task_dir: Path,
    task: TaskRecord,
    stage: Literal["asr", "ocr"],
    payload: dict[str, Any],
) -> TaskRecord:
    provider = payload.get("provider")
    model = payload.get("model")
    provider_names = {**task.providers}
    models = {**task.models}
    updates: dict[str, object] = {"providers": provider_names, "models": models}
    if isinstance(provider, str) and provider:
        provider_names[stage] = provider
        if stage == "asr":
            updates["provider"] = provider
    if isinstance(model, str) and model:
        models[stage] = model
        if stage == "asr":
            updates["model"] = model
    updated = task.model_copy(update=updates)
    write_task_atomic(task_dir, updated)
    return updated


def _write_report(task_dir: Path) -> None:
    (task_dir / "process_report.md").write_text(
        render_report(load_task(task_dir)), encoding="utf-8"
    )


def _validate_probe(probe: dict[str, Any]) -> None:
    duration_ms = _duration_ms(probe)
    if duration_ms > _MAX_DURATION_MS:
        raise PipelineError("video duration exceeds 30 minutes; 需要分块模式")
    if not any(
        stream.get("codec_type") == "audio" for stream in probe.get("streams", [])
    ):
        raise PipelineError("video audio stream is required")


def _validate_asr_payload(payload: dict[str, Any]) -> None:
    try:
        _AsrPayload.model_validate(payload)
    except ValidationError as error:
        raise PipelineError(
            f"ASR worker output violates its contract: {error}"
        ) from error


def _validate_ocr_payload(payload: dict[str, Any]) -> None:
    try:
        _OcrPayload.model_validate(payload)
    except ValidationError as error:
        raise PipelineError(
            f"OCR worker output violates its contract: {error}"
        ) from error


def _duration_ms(probe: dict[str, Any]) -> int:
    try:
        return round(float(probe["format"]["duration"]) * 1_000)
    except (KeyError, TypeError, ValueError) as error:
        raise PipelineError("ffprobe did not provide a valid duration") from error


def _frame_duration_ms(probe: dict[str, Any]) -> int:
    """Use the video stream boundary when the container/audio runs longer."""
    format_duration = _duration_ms(probe)
    video_durations: list[int] = []
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        try:
            duration = round(float(stream["duration"]) * 1_000)
        except (KeyError, TypeError, ValueError):
            continue
        if duration > 0:
            video_durations.append(duration)
    return (
        min(format_duration, *video_durations) if video_durations else format_duration
    )


def _select_distinct_frames(
    candidates: list[tuple[int, Path]],
) -> list[tuple[int, Path]]:
    selected: list[tuple[int, Path]] = []
    selected_thumbnails: list[Image.Image] = []
    for candidate in candidates:
        thumbnail = _gray_thumbnail(candidate[1])
        if all(
            _thumbnail_difference(selected_thumbnail, thumbnail) >= 8
            for selected_thumbnail in selected_thumbnails
        ):
            selected.append(candidate)
            selected_thumbnails.append(thumbnail)
        if len(selected) == _MAX_SELECTED_FRAMES:
            break
    return selected


def _gray_thumbnail(path: Path) -> Image.Image:
    with Image.open(path) as image:
        thumbnail = image.convert("L").resize((32, 18))
        return thumbnail.copy()


def _thumbnail_difference(left: Image.Image, right: Image.Image) -> float:
    return float(ImageStat.Stat(ImageChops.difference(left, right)).mean[0])


def _write_transcript_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        f"- {segment['start_ms']} ms: {segment['text']}"
        for segment in payload.get("segments", [])
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_ocr_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        f"- {frame['frame_id']}: {item['text']}"
        for frame in payload.get("frames", [])
        for item in frame.get("items", [])
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_evidence_markdown(path: Path, content_pack: ContentPack) -> None:
    lines = [f"- [{item.id}] {item.kind}" for item in content_pack.evidence]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineError(f"invalid worker output: {path.name}") from error
    if not isinstance(payload, dict):
        raise PipelineError(f"worker output must be an object: {path.name}")
    return payload
