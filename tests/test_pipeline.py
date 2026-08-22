from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from learnnest.models import StageStatus
from learnnest.pipeline import (
    PipelineError,
    candidate_timestamps,
    process_video,
    rerun_task,
)


def _video(path: Path) -> Path:
    path.write_bytes(b"not a real video")
    return path


def _probe(*, duration_seconds: float = 20.0, audio: bool = True) -> dict[str, object]:
    streams: list[dict[str, str]] = [{"codec_type": "video"}]
    if audio:
        streams.append({"codec_type": "audio"})
    return {"format": {"duration": str(duration_seconds)}, "streams": streams}


def _fake_ffmpeg(*, video_path: Path, timestamp_ms: int, output_path: Path) -> None:
    del video_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color=(timestamp_ms % 255, 0, 0)).save(output_path)


def _fake_worker(args: list[str]) -> None:
    output = Path(args[2])
    output.parent.mkdir(parents=True, exist_ok=True)
    if args[0] == "asr":
        model = args[args.index("--model") + 1]
        payload: dict[str, object] = {
            "schema_version": "1.0",
            "provider": "faster-whisper",
            "model": model,
            "segments": [
                {
                    "id": "tr_0001",
                    "start_ms": 1_000,
                    "end_ms": 2_000,
                    "text": "打开设置",
                }
            ],
        }
    else:
        payload = {
            "schema_version": "1.0",
            "provider": "paddleocr",
            "items": [{"text": "设置", "confidence": 0.9}],
        }
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_media_tools_decode_utf8_output_for_chinese_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> object:
        calls.append(kwargs)
        if command[0] == "ffmpeg":
            output = Path(command[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (16, 16)).save(output)
            return pipeline.subprocess.CompletedProcess(
                command, 0, stdout="", stderr="输出"
            )
        return pipeline.subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(_probe()),
            stderr="",
        )

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    video = tmp_path / "中文视频.mp4"
    output = tmp_path / "视频学习素材" / "候选帧.png"

    pipeline.probe_video(video)
    pipeline.extract_frame(video_path=video, timestamp_ms=0, output_path=output)

    assert calls == [
        {
            "capture_output": True,
            "encoding": "utf-8",
            "errors": "replace",
            "check": False,
        },
        {
            "capture_output": True,
            "encoding": "utf-8",
            "errors": "replace",
            "check": False,
        },
    ]


def test_candidate_timestamps_are_sorted_unique_and_bounded() -> None:
    timestamps = candidate_timestamps(
        duration_ms=25_000,
        transcript_segments=[
            {"start_ms": 10_000, "text": "打开设置"},
            {"start_ms": 10_000, "text": "打开设置"},
        ],
    )

    assert timestamps == [0, 10_000, 20_000]


def test_frame_duration_uses_video_stream_instead_of_longer_container_duration() -> (
    None
):
    import learnnest.pipeline as pipeline

    probe = {
        "format": {"duration": "10.026667"},
        "streams": [
            {"codec_type": "video", "duration": "10.000000"},
            {"codec_type": "audio", "duration": "10.026667"},
        ],
    }

    duration_ms = pipeline._frame_duration_ms(probe)

    assert duration_ms == 10_000
    assert candidate_timestamps(
        duration_ms=duration_ms,
        transcript_segments=[],
    ) == [0]


def test_process_video_rejects_a_video_longer_than_thirty_minutes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=1_801)
    )

    with pytest.raises(PipelineError, match="30 minutes"):
        process_video(_video(tmp_path / "long.mp4"), tmp_path / "output", "evidence")

    report = next((tmp_path / "output" / "视频学习素材").glob("*/process_report.md"))
    assert "需要分块模式" in report.read_text(encoding="utf-8")


def test_process_video_rejects_a_video_without_an_audio_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe(audio=False))

    with pytest.raises(PipelineError, match="audio stream"):
        process_video(_video(tmp_path / "silent.mp4"), tmp_path / "output", "evidence")


def test_process_video_compacts_intermediate_sources_into_content_pack_and_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=170)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)

    task = process_video(
        _video(tmp_path / "lesson.mp4"), tmp_path / "output", "evidence"
    )
    task_dir = tmp_path / "output" / "视频学习素材" / f"lesson--{task.task_id[-8:]}"
    content_pack = json.loads(
        (task_dir / "content_pack.json").read_text(encoding="utf-8")
    )

    assert task.stages["note"] is StageStatus.SKIPPED
    assert task.stages["publish"] is StageStatus.SKIPPED
    assert task.stages["podcast_script"] is StageStatus.SKIPPED
    assert task.stages["tts"] is StageStatus.SKIPPED
    assert task.provider == "faster-whisper"
    assert task.model == "large-v3"
    assert task.providers == {"asr": "faster-whisper", "ocr": "paddleocr"}
    assert task.models == {"asr": "large-v3"}
    selected_count = len(list((task_dir / "frames" / "selected").glob("*.png")))
    assert selected_count <= 15
    assert {item["kind"] for item in content_pack["evidence"]} == {
        "transcript",
        "frame",
        "ocr",
    }
    assert (task_dir / "source.json").is_file()
    assert (task_dir / "content_pack.json").is_file()
    assert (task_dir / "trace.md").is_file()
    assert not (task_dir / "transcript.json").exists()
    assert not (task_dir / "transcript.md").exists()
    assert not (task_dir / "ocr").exists()
    assert not (task_dir / "ocr.json").exists()
    assert not (task_dir / "ocr.md").exists()
    assert not (task_dir / "evidence.json").exists()
    assert not (task_dir / "evidence.md").exists()
    assert not (task_dir / "frames" / "candidates").exists()
    assert task.active_attempt_id is None
    assert len(task.attempts) == 1
    assert task.attempts[0].reason == "initial"
    assert task.attempts[0].status == "completed"


def test_process_video_executes_the_selected_material_adapters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.material_adapters import MaterialAdapters

    calls: list[str] = []

    class SelectedAsr:
        def transcribe(self, _source: Path, output: Path) -> None:
            calls.append("asr")
            output.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "provider": "selected-asr",
                        "model": "selected-model",
                        "segments": [
                            {
                                "id": "tr_0001",
                                "start_ms": 0,
                                "end_ms": 1_000,
                                "text": "真实选择",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    class SelectedOcr:
        def recognize(self, _image: Path, output: Path) -> None:
            calls.append("ocr")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "provider": "selected-ocr",
                        "items": [{"text": "画面", "confidence": 0.98}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)

    task = process_video(
        _video(tmp_path / "selected.mp4"),
        tmp_path / "output",
        material_adapters=MaterialAdapters(asr=SelectedAsr(), ocr=SelectedOcr()),
    )

    assert calls[0] == "asr"
    assert set(calls[1:]) == {"ocr"}
    assert task.providers == {"asr": "selected-asr", "ocr": "selected-ocr"}
    assert task.models == {"asr": "selected-model"}


def test_process_source_can_mark_a_new_attempt_as_scheduled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.sources import parse_source

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)

    source = parse_source(str(_video(tmp_path / "scheduled.mp4")))
    task = pipeline.process_source(
        source,
        tmp_path / "output",
        initial_attempt_reason="scheduled",
    )

    assert [attempt.reason for attempt in task.attempts] == ["scheduled"]


def test_full_profile_explicitly_skips_future_audio_stages_after_publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)

    task = process_video(_video(tmp_path / "full.mp4"), tmp_path / "output", "full")

    assert task.stages["note"] is StageStatus.COMPLETED
    assert task.stages["publish"] is StageStatus.COMPLETED
    assert task.stages["podcast_script"] is StageStatus.SKIPPED
    assert task.stages["tts"] is StageStatus.SKIPPED
    task_dir = tmp_path / "output" / "视频学习素材" / f"full--{task.task_id[-8:]}"
    assert not (task_dir / "published_note.md").exists()


def test_rerun_note_overwrites_the_same_tasks_published_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    task = process_video(_video(tmp_path / "lesson.mp4"), tmp_path / "output", "note")
    task_dir = tmp_path / "output" / "视频学习素材" / f"lesson--{task.task_id[-8:]}"

    rerun_task(task_dir, "note")

    published = list((tmp_path / "output" / "视频学习笔记").glob("lesson*.md"))
    assert [path.name for path in published] == ["lesson.md"]
    assert f"learnnest-task-id: {task.task_id}" in published[0].read_text(
        encoding="utf-8"
    )


def test_rerun_note_uses_the_actual_renamed_task_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    task = process_video(_video(tmp_path / "lesson.mp4"), tmp_path / "output", "note")
    task_dir = tmp_path / "output" / "视频学习素材" / f"lesson--{task.task_id[-8:]}"
    renamed = task_dir.with_name(f"custom-directory--{task.task_id[-8:]}")
    task_dir.rename(renamed)

    rerun_task(renamed, "note")

    note = (renamed / "note.md").read_text(encoding="utf-8")
    assert f"视频学习素材/{renamed.name}/frames/selected" in note
    assert f"视频学习素材/{task_dir.name}/frames/selected" not in note


def test_publish_keeps_an_existing_note_owned_by_another_task(tmp_path: Path) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.task_store import create_task

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    (task_dir / "note.md").write_text(
        "<!-- learnnest-task-id: 20260711-a1b2c3d4 -->\n# lesson\n",
        encoding="utf-8",
    )
    notes_dir = tmp_path / "视频学习笔记"
    notes_dir.mkdir()
    (notes_dir / "lesson.md").write_text("# 用户已有笔记\n", encoding="utf-8")

    pipeline._publish_note(task_dir, task, tmp_path)

    assert (notes_dir / "lesson.md").read_text(encoding="utf-8") == "# 用户已有笔记\n"
    assert (notes_dir / "lesson--a1b2c3d4.md").is_file()


def test_publish_keeps_a_non_utf8_user_note_and_uses_the_hashed_path(
    tmp_path: Path,
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.task_store import create_task

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    (task_dir / "note.md").write_text(
        "<!-- learnnest-task-id: 20260711-a1b2c3d4 -->\n# lesson\n",
        encoding="utf-8",
    )
    notes_dir = tmp_path / "视频学习笔记"
    notes_dir.mkdir()
    user_note = notes_dir / "lesson.md"
    user_note.write_bytes(b"\xff\xfe\x00")

    pipeline._publish_note(task_dir, task, tmp_path)

    assert user_note.read_bytes() == b"\xff\xfe\x00"
    assert (notes_dir / "lesson--a1b2c3d4.md").is_file()


def test_publish_replaces_a_note_owned_by_the_legacy_marker(tmp_path: Path) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.task_store import create_task

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    (task_dir / "note.md").write_text(
        "<!-- learnnest-task-id: 20260711-a1b2c3d4 -->\n# new lesson\n",
        encoding="utf-8",
    )
    notes_dir = tmp_path / "视频学习笔记"
    notes_dir.mkdir()
    published = notes_dir / "lesson.md"
    published.write_text(
        "<!-- learnpipe-task-id: 20260711-a1b2c3d4 -->\n# old lesson\n",
        encoding="utf-8",
    )

    pipeline._publish_note(task_dir, task, tmp_path)

    assert "# new lesson" in published.read_text(encoding="utf-8")
    assert not (notes_dir / "lesson--a1b2c3d4.md").exists()


def test_publish_blocks_when_legacy_duplicate_notes_belong_to_the_same_task(
    tmp_path: Path,
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.task_store import create_task

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    owned_note = "- 任务：`20260711-a1b2c3d4`\n# lesson\n"
    (task_dir / "note.md").write_text(owned_note, encoding="utf-8")
    notes_dir = tmp_path / "视频学习笔记"
    notes_dir.mkdir()
    (notes_dir / "lesson.md").write_text(owned_note, encoding="utf-8")
    (notes_dir / "lesson--a1b2c3d4.md").write_text(owned_note, encoding="utf-8")

    with pytest.raises(PipelineError, match="duplicate published notes"):
        pipeline._publish_note(task_dir, task, tmp_path)


def test_publish_replace_failure_preserves_an_existing_owned_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.task_store import create_task

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    (task_dir / "note.md").write_text(
        "<!-- learnnest-task-id: 20260711-a1b2c3d4 -->\n# new lesson\n",
        encoding="utf-8",
    )
    notes_dir = tmp_path / "视频学习笔记"
    notes_dir.mkdir()
    published = notes_dir / "lesson.md"
    published.write_text(
        "<!-- learnnest-task-id: 20260711-a1b2c3d4 -->\n# old lesson\n",
        encoding="utf-8",
    )
    old_bytes = published.read_bytes()
    monkeypatch.setattr(
        pipeline,
        "atomic_replace_bytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
        raising=False,
    )

    with pytest.raises(OSError, match="replace failed"):
        pipeline._publish_note(task_dir, task, tmp_path)

    assert published.read_bytes() == old_bytes


def test_process_video_fails_final_stage_when_task_validation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    monkeypatch.setattr(
        pipeline, "validate_task", lambda task_dir: ["missing evidence artifact"]
    )

    with pytest.raises(PipelineError, match="missing evidence artifact"):
        process_video(_video(tmp_path / "invalid.mp4"), tmp_path / "output", "evidence")

    task_json = next((tmp_path / "output" / "视频学习素材").glob("*/task.json"))
    failed = json.loads(task_json.read_text(encoding="utf-8"))
    assert failed["stages"]["content_pack"] == "failed"
    assert "missing evidence artifact" in (
        task_json.parent / "process_report.md"
    ).read_text(encoding="utf-8")


def test_rerun_task_restarts_at_the_requested_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    calls: list[str] = []

    def worker(args: list[str]) -> None:
        calls.append(args[0])
        _fake_worker(args)

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)
    task = process_video(
        _video(tmp_path / "rerun.mp4"), tmp_path / "output", "evidence"
    )
    task_dir = tmp_path / "output" / "视频学习素材" / f"rerun--{task.task_id[-8:]}"
    calls.clear()

    rerun = rerun_task(task_dir, "ocr")

    assert "asr" not in calls
    assert calls == ["ocr", "ocr", "ocr"]
    assert [attempt.reason for attempt in rerun.attempts] == ["initial", "retry"]
    assert [attempt.status for attempt in rerun.attempts] == [
        "completed",
        "completed",
    ]


def test_rerun_uses_acquired_media_path_instead_of_logical_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.task_store import load_task, write_task_atomic

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    task = process_video(
        _video(tmp_path / "source.mp4"), tmp_path / "output", "evidence"
    )
    task_dir = tmp_path / "output" / "视频学习素材" / f"source--{task.task_id[-8:]}"
    persisted = load_task(task_dir)
    logical_url = "https://example.com/watch?v=1"
    url_task = persisted.model_copy(
        update={
            "source_path": logical_url,
            "source_input": logical_url,
            "source_type": "url",
            "media_path": persisted.source_path,
        }
    )
    write_task_atomic(task_dir, url_task)

    rerun = rerun_task(task_dir, "ocr")

    assert rerun.stages["content_pack"] is StageStatus.COMPLETED


def test_process_url_prefers_platform_subtitle_without_asr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import AcquiredSource
    from learnnest.sources import parse_source

    worker_calls: list[str] = []

    class FakeDownloader:
        def acquire(self, source, task_dir, *, source_fingerprint):
            media = task_dir / "downloads" / "source.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            subtitle = task_dir / "downloads" / "source.zh.vtt"
            subtitle.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n平台字幕\n",
                encoding="utf-8",
            )
            return AcquiredSource(
                source_input=source.input,
                source_type="url",
                source_fingerprint=source_fingerprint,
                media_path="downloads/source.mp4",
                subtitle_path="downloads/source.zh.vtt",
                platform_id="video-1",
                platform_title="公开课程",
            )

    def worker(args: list[str]) -> None:
        worker_calls.append(args[0])
        _fake_worker(args)

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)

    task = pipeline.process_source(
        parse_source("https://example.com/watch?v=1"),
        tmp_path,
        "evidence",
        downloader=FakeDownloader(),
    )
    task_dir = next((tmp_path / "视频学习素材").glob(f"*--{task.task_id[-8:]}"))
    content_pack = json.loads(
        (task_dir / "content_pack.json").read_text(encoding="utf-8")
    )
    transcripts = [
        item for item in content_pack["evidence"] if item["kind"] == "transcript"
    ]

    assert "asr" not in worker_calls
    assert worker_calls
    assert set(worker_calls) == {"ocr"}
    assert transcripts[0]["text"] == "平台字幕"
    assert task.source_type == "url"
    assert task.media_path == "downloads/source.mp4"
    assert task.stages["content_pack"] is StageStatus.COMPLETED


def test_process_url_falls_back_to_asr_without_platform_subtitle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import AcquiredSource
    from learnnest.sources import parse_source

    worker_calls: list[str] = []

    class FakeDownloader:
        def acquire(self, source, task_dir, *, source_fingerprint):
            media = task_dir / "downloads" / "source.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            return AcquiredSource(
                source_input=source.input,
                source_type="url",
                source_fingerprint=source_fingerprint,
                media_path="downloads/source.mp4",
            )

    def worker(args: list[str]) -> None:
        worker_calls.append(args[0])
        _fake_worker(args)

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)

    pipeline.process_source(
        parse_source("https://example.com/watch?v=1"),
        tmp_path,
        "evidence",
        downloader=FakeDownloader(),
    )

    assert worker_calls[0] == "asr"


def test_process_same_local_input_returns_canonical_without_writes_or_heavy_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    worker_calls: list[str] = []

    def worker(args: list[str]) -> None:
        worker_calls.append(args[0])
        _fake_worker(args)

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)
    source = _video(tmp_path / "same.mp4")
    first = process_video(source, tmp_path / "output")
    task_dir = next((tmp_path / "output" / "视频学习素材").iterdir())
    old_bytes = (task_dir / "task.json").read_bytes()
    worker_calls.clear()

    second = process_video(source, tmp_path / "output")

    assert second.task_id == first.task_id
    assert worker_calls == []
    assert (task_dir / "task.json").read_bytes() == old_bytes


def test_new_task_persists_note_type_but_duplicate_does_not_rewrite_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import SourceItem
    from learnnest.task_store import find_task_by_id, load_task

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    video = _video(tmp_path / "lesson.mp4")
    output = tmp_path / "vault"
    first = pipeline.process_source(
        SourceItem(
            input=str(video),
            input_type="local_file",
            note_type="concept_explanation",
        ),
        output,
    )

    duplicate = pipeline.process_source(
        SourceItem(
            input=str(video),
            input_type="local_file",
            note_type="practical_tutorial",
        ),
        output,
    )

    match = find_task_by_id(output, first.task_id)
    assert match is not None
    task_dir, _ = match
    assert first.task_id == duplicate.task_id
    assert load_task(task_dir).note_type_override == "concept_explanation"


def test_force_does_not_rewrite_the_persisted_note_type_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import SourceItem
    from learnnest.task_store import find_task_by_id, load_task

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    video = _video(tmp_path / "force-note-type.mp4")
    output = tmp_path / "vault"
    first = pipeline.process_source(
        SourceItem(
            input=str(video),
            input_type="local_file",
            note_type="resource_share",
        ),
        output,
    )

    forced = pipeline.process_source(
        SourceItem(
            input=str(video),
            input_type="local_file",
            note_type="practical_tutorial",
        ),
        output,
        force=True,
    )

    match = find_task_by_id(output, first.task_id)
    assert match is not None
    task_dir, _ = match
    assert forced.task_id == first.task_id
    assert load_task(task_dir).note_type_override == "resource_share"


def test_process_existing_failed_task_is_not_overwritten_and_gives_recovery_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(
        pipeline.providers,
        "run_worker",
        lambda args: (_ for _ in ()).throw(RuntimeError("temporary ASR failure")),
    )
    source = _video(tmp_path / "failed.mp4")
    with pytest.raises(PipelineError, match="temporary ASR failure"):
        process_video(source, tmp_path / "output")
    task_dir = next((tmp_path / "output" / "视频学习素材").iterdir())
    old_bytes = (task_dir / "task.json").read_bytes()

    with pytest.raises(PipelineError, match=r"recover|retry"):
        process_video(source, tmp_path / "output")

    assert (task_dir / "task.json").read_bytes() == old_bytes


def test_process_copy_with_same_content_returns_canonical_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    worker_calls: list[str] = []

    def worker(args: list[str]) -> None:
        worker_calls.append(args[0])
        _fake_worker(args)

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)
    first_path = _video(tmp_path / "first.mp4")
    second_path = tmp_path / "copy.mp4"
    second_path.write_bytes(first_path.read_bytes())
    first = process_video(first_path, tmp_path / "output")
    worker_calls.clear()

    second = process_video(second_path, tmp_path / "output")

    assert second.task_id == first.task_id
    assert worker_calls == []
    assert len(list((tmp_path / "output" / "视频学习素材").iterdir())) == 1


def test_process_prefers_completed_legacy_content_over_incomplete_duplicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hashlib
    import learnnest.pipeline as pipeline
    from learnnest.execution_models import SourceIdentities
    from learnnest.models import TaskRecord
    from learnnest.task_store import write_task_atomic

    output = tmp_path / "output"
    original = _video(tmp_path / "original.mp4")
    copy = tmp_path / "copy.mp4"
    copy.write_bytes(original.read_bytes())
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(original.read_bytes())
    legacy_dir = output / "视频学习素材" / "z-legacy--legacy01"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "task.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "task_id": "20260711-legacy01",
                "source_path": str(original),
                "source_fingerprint": "legacy-fingerprint",
                "title": "legacy",
                "stages": {"content_pack": "completed"},
            }
        ),
        encoding="utf-8",
    )
    content_hash = hashlib.sha256(copy.read_bytes()).hexdigest()
    write_task_atomic(
        output / "视频学习素材" / "a-incomplete--modern01",
        TaskRecord(
            task_id="20260712-modern01",
            source_path=str(copy),
            media_path=str(copy),
            source_fingerprint="modern-fingerprint",
            title="incomplete",
            identities=SourceIdentities(
                normalized_source=str(copy),
                content_sha256=content_hash,
            ),
            stages={"ocr": StageStatus.RUNNING},
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "probe_video",
        lambda path: pytest.fail("completed canonical must skip heavy stages"),
    )

    returned = process_video(candidate, output)

    assert returned.task_id == "20260711-legacy01"


def test_process_different_bytes_with_same_size_and_title_are_not_duplicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import SourceItem

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    first_path = tmp_path / "first.mp4"
    second_path = tmp_path / "second.mp4"
    first_path.write_bytes(b"abc123")
    second_path.write_bytes(b"xyz789")

    first = pipeline.process_source(
        SourceItem(input=str(first_path), input_type="local_file", title="same"),
        tmp_path / "output",
    )
    second = pipeline.process_source(
        SourceItem(input=str(second_path), input_type="local_file", title="same"),
        tmp_path / "output",
    )

    assert first.task_id != second.task_id
    assert len(list((tmp_path / "output" / "视频学习素材").iterdir())) == 2


def test_process_url_records_platform_and_content_identities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hashlib
    import learnnest.pipeline as pipeline
    from learnnest.source_models import AcquiredSource
    from learnnest.sources import parse_source

    class FakeDownloader:
        def acquire(self, source, task_dir, *, source_fingerprint):
            media = task_dir / "downloads" / "source.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"downloaded-video")
            return AcquiredSource(
                source_input=source.input,
                source_type="url",
                source_fingerprint=source_fingerprint,
                media_path="downloads/source.mp4",
                platform_id="video-1",
            )

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)

    task = pipeline.process_source(
        parse_source("https://example.com/watch?v=1"),
        tmp_path / "output",
        downloader=FakeDownloader(),
    )
    task_dir = next((tmp_path / "output" / "视频学习素材").iterdir())
    source_payload = json.loads((task_dir / "source.json").read_text(encoding="utf-8"))

    expected_hash = hashlib.sha256(b"downloaded-video").hexdigest()
    assert task.identities is not None
    assert task.identities.platform == "yt-dlp"
    assert task.identities.platform_id == "video-1"
    assert task.identities.content_sha256 == expected_hash
    assert source_payload["identities"] == task.identities.model_dump(mode="json")


def test_process_url_download_duplicate_keeps_download_and_skips_heavy_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import AcquiredSource
    from learnnest.sources import parse_source

    worker_calls: list[str] = []

    def worker(args: list[str]) -> None:
        worker_calls.append(args[0])
        _fake_worker(args)

    class FakeDownloader:
        def acquire(self, source, task_dir, *, source_fingerprint):
            media = task_dir / "downloads" / "source.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"same-content")
            return AcquiredSource(
                source_input=source.input,
                source_type="url",
                source_fingerprint=source_fingerprint,
                media_path="downloads/source.mp4",
                platform_id="remote-2",
            )

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)
    local = _video(tmp_path / "local.mp4")
    local.write_bytes(b"same-content")
    canonical = process_video(local, tmp_path / "output")
    worker_calls.clear()

    returned = pipeline.process_source(
        parse_source("https://example.com/other"),
        tmp_path / "output",
        downloader=FakeDownloader(),
    )

    duplicate_dir = next(
        path
        for path in (tmp_path / "output" / "视频学习素材").iterdir()
        if path.name.startswith("url-")
    )
    duplicate = json.loads((duplicate_dir / "task.json").read_text(encoding="utf-8"))
    assert returned.task_id == canonical.task_id
    assert duplicate["duplicate_of_task_id"] == canonical.task_id
    assert duplicate["attempts"][-1]["status"] == "skipped_duplicate"
    assert duplicate["stages"]["source"] == "completed"
    assert duplicate["stages"]["transcript"] == "skipped"
    assert (duplicate_dir / "downloads" / "source.mp4").read_bytes() == b"same-content"
    assert worker_calls == []


def test_process_force_reruns_the_same_task_with_a_force_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import SourceItem

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    source = _video(tmp_path / "force.mp4")
    item = SourceItem(input=str(source), input_type="local_file")
    first = pipeline.process_source(item, tmp_path / "output")

    forced = pipeline.process_source(item, tmp_path / "output", force=True)

    assert forced.task_id == first.task_id
    assert [attempt.reason for attempt in forced.attempts] == ["initial", "force"]
    assert len(list((tmp_path / "output" / "视频学习素材").iterdir())) == 1


def test_allow_duplicate_only_bypasses_content_identity_for_different_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import SourceItem

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    first_path = _video(tmp_path / "first.mp4")
    second_path = tmp_path / "second.mp4"
    second_path.write_bytes(first_path.read_bytes())
    first_item = SourceItem(input=str(first_path), input_type="local_file")
    second_item = SourceItem(input=str(second_path), input_type="local_file")
    first = pipeline.process_source(first_item, tmp_path / "output")

    duplicate = pipeline.process_source(
        second_item,
        tmp_path / "output",
        allow_duplicate=True,
    )
    duplicate_dir = next(
        path
        for path in (tmp_path / "output" / "视频学习素材").iterdir()
        if path.name.startswith("second--")
    )
    old_bytes = (duplicate_dir / "task.json").read_bytes()
    same_path = pipeline.process_source(
        second_item,
        tmp_path / "output",
        allow_duplicate=True,
    )

    assert duplicate.task_id != first.task_id
    assert same_path.task_id == duplicate.task_id
    assert (duplicate_dir / "task.json").read_bytes() == old_bytes


def test_same_url_returns_canonical_without_downloading_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import AcquiredSource
    from learnnest.sources import parse_source

    downloads = 0

    class FakeDownloader:
        def acquire(self, source, task_dir, *, source_fingerprint):
            nonlocal downloads
            downloads += 1
            media = task_dir / "downloads" / "source.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"url-video")
            return AcquiredSource(
                source_input=source.input,
                source_type="url",
                source_fingerprint=source_fingerprint,
                media_path="downloads/source.mp4",
            )

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    item = parse_source("https://example.com/watch?v=1")
    first = pipeline.process_source(
        item, tmp_path / "output", downloader=FakeDownloader()
    )
    task_dir = next((tmp_path / "output" / "视频学习素材").iterdir())
    old_bytes = (task_dir / "task.json").read_bytes()

    second = pipeline.process_source(
        item, tmp_path / "output", downloader=FakeDownloader()
    )

    assert second.task_id == first.task_id
    assert downloads == 1
    assert (task_dir / "task.json").read_bytes() == old_bytes


def test_allow_duplicate_bypasses_platform_and_content_for_different_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import AcquiredSource
    from learnnest.sources import parse_source

    class FakeDownloader:
        def acquire(self, source, task_dir, *, source_fingerprint):
            media = task_dir / "downloads" / "source.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"same-platform-content")
            return AcquiredSource(
                source_input=source.input,
                source_type="url",
                source_fingerprint=source_fingerprint,
                media_path="downloads/source.mp4",
                platform_id="shared-platform-id",
            )

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    first = pipeline.process_source(
        parse_source("https://example.com/first"),
        tmp_path / "output",
        downloader=FakeDownloader(),
    )

    second = pipeline.process_source(
        parse_source("https://mirror.example.com/second"),
        tmp_path / "output",
        downloader=FakeDownloader(),
        allow_duplicate=True,
    )

    tasks = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "output" / "视频学习素材").glob("*/task.json")
    ]
    assert first.task_id != second.task_id
    assert len(tasks) == 2
    assert all(task["duplicate_of_task_id"] is None for task in tasks)


def test_same_platform_id_is_canonical_even_when_downloaded_bytes_differ(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import AcquiredSource
    from learnnest.sources import parse_source

    class FakeDownloader:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def acquire(self, source, task_dir, *, source_fingerprint):
            media = task_dir / "downloads" / "source.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(self.content)
            return AcquiredSource(
                source_input=source.input,
                source_type="url",
                source_fingerprint=source_fingerprint,
                media_path="downloads/source.mp4",
                platform_id="same-video-id",
            )

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    first = pipeline.process_source(
        parse_source("https://example.com/watch/first"),
        tmp_path / "output",
        downloader=FakeDownloader(b"first-content"),
    )

    second = pipeline.process_source(
        parse_source("https://mirror.example.com/watch/second"),
        tmp_path / "output",
        downloader=FakeDownloader(b"other-content"),
    )

    duplicate = next(
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "output" / "视频学习素材").glob("*/task.json")
        if json.loads(path.read_text(encoding="utf-8"))["duplicate_of_task_id"]
    )
    assert second.task_id == first.task_id
    assert duplicate["duplicate_of_task_id"] == first.task_id


def test_force_retries_a_failed_url_acquisition_on_the_same_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.downloader import DownloaderError
    from learnnest.source_models import AcquiredSource
    from learnnest.sources import parse_source

    calls = 0

    class FakeDownloader:
        def acquire(self, source, task_dir, *, source_fingerprint):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DownloaderError("temporary download failure")
            media = task_dir / "downloads" / "source.mp4"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"recovered-url")
            return AcquiredSource(
                source_input=source.input,
                source_type="url",
                source_fingerprint=source_fingerprint,
                media_path="downloads/source.mp4",
            )

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    item = parse_source("https://example.com/retry")
    with pytest.raises(PipelineError, match="temporary download failure"):
        pipeline.process_source(
            item,
            tmp_path / "output",
            downloader=FakeDownloader(),
        )
    failed_task = next(
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "output" / "视频学习素材").glob("*/task.json")
    )

    recovered = pipeline.process_source(
        item,
        tmp_path / "output",
        downloader=FakeDownloader(),
        force=True,
    )

    assert recovered.task_id == failed_task["task_id"]
    assert [attempt.reason for attempt in recovered.attempts] == ["initial", "force"]
    assert [attempt.status for attempt in recovered.attempts] == ["failed", "completed"]
    assert calls == 2
    assert len(list((tmp_path / "output" / "视频学习素材").iterdir())) == 1


def test_url_duplicate_continues_when_only_matching_task_is_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.source_models import AcquiredSource
    from learnnest.sources import parse_source

    local = _video(tmp_path / "failed-local.mp4")
    local.write_bytes(b"shared-content")
    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)

    def fail_asr(args: list[str]) -> None:
        if args[0] == "asr":
            raise RuntimeError("forced transcript failure")
        _fake_worker(args)

    monkeypatch.setattr(pipeline.providers, "run_worker", fail_asr)
    with pytest.raises(PipelineError, match="forced transcript failure"):
        process_video(local, tmp_path / "output")
    failed_task = next(
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "output" / "视频学习素材").glob("*/task.json")
    )

    class FakeDownloader:
        def acquire(self, source, task_dir, *, source_fingerprint):
            media = task_dir / "downloads" / "source.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"shared-content")
            return AcquiredSource(
                source_input=source.input,
                source_type="url",
                source_fingerprint=source_fingerprint,
                media_path="downloads/source.mp4",
            )

    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    completed = pipeline.process_source(
        parse_source("https://example.com/usable-copy"),
        tmp_path / "output",
        downloader=FakeDownloader(),
    )
    completed_dir = next(
        path
        for path in (tmp_path / "output" / "视频学习素材").iterdir()
        if path.name.startswith("url-")
    )
    completed_payload = json.loads(
        (completed_dir / "task.json").read_text(encoding="utf-8")
    )

    assert completed.task_id != failed_task["task_id"]
    assert completed_payload["duplicate_of_task_id"] is None
    assert completed_payload["attempts"][-1]["status"] == "completed"


def test_local_force_refreshes_fingerprint_and_content_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hashlib

    import learnnest.pipeline as pipeline
    from learnnest.sources import parse_source
    from learnnest.util import source_fingerprint

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    source = _video(tmp_path / "changing.mp4")
    item = parse_source(str(source))
    first = pipeline.process_source(item, tmp_path / "output")
    source.write_bytes(b"new-media-content-with-a-different-size")

    forced = pipeline.process_source(item, tmp_path / "output", force=True)
    task_dir = next((tmp_path / "output" / "视频学习素材").iterdir())
    source_payload = json.loads((task_dir / "source.json").read_text(encoding="utf-8"))
    content_pack = json.loads(
        (task_dir / "content_pack.json").read_text(encoding="utf-8")
    )
    expected_content = hashlib.sha256(source.read_bytes()).hexdigest()
    expected_fingerprint = source_fingerprint(source)

    assert forced.task_id == first.task_id
    assert forced.source_fingerprint == expected_fingerprint
    assert forced.identities is not None
    assert forced.identities.content_sha256 == expected_content
    assert source_payload["source_fingerprint"] == expected_fingerprint
    assert source_payload["identities"] == forced.identities.model_dump(mode="json")
    assert content_pack["source_fingerprint"] == expected_fingerprint
    assert [attempt.reason for attempt in forced.attempts] == ["initial", "force"]


def test_local_pipeline_acquires_slots_around_each_heavy_resource(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from contextlib import contextmanager

    import learnnest.pipeline as pipeline
    from learnnest.sources import parse_source

    events: list[str] = []

    class RecordingScheduler:
        @contextmanager
        def acquire(self, resource: str):
            events.append(f"enter:{resource}")
            yield 0
            events.append(f"exit:{resource}")

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    source = _video(tmp_path / "resources.mp4")

    pipeline.process_source(
        parse_source(str(source)),
        tmp_path / "output",
        scheduler=RecordingScheduler(),
    )

    assert events == [
        "enter:ffmpeg",
        "exit:ffmpeg",
        "enter:asr",
        "exit:asr",
        "enter:ffmpeg",
        "exit:ffmpeg",
        "enter:ocr",
        "exit:ocr",
    ]


def test_url_acquisition_holds_network_then_ffmpeg_slots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from contextlib import contextmanager

    import learnnest.pipeline as pipeline
    from learnnest.source_models import AcquiredSource
    from learnnest.sources import parse_source

    events: list[str] = []

    class RecordingScheduler:
        @contextmanager
        def acquire(self, resource: str):
            events.append(f"enter:{resource}")
            yield 0
            events.append(f"exit:{resource}")

    class FakeDownloader:
        def acquire(self, source, task_dir, *, source_fingerprint):
            events.append("download")
            media = task_dir / "downloads" / "source.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"url-resource-test")
            return AcquiredSource(
                source_input=source.input,
                source_type="url",
                source_fingerprint=source_fingerprint,
                media_path="downloads/source.mp4",
            )

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)

    pipeline.process_source(
        parse_source("https://example.com/resource-test"),
        tmp_path / "output",
        downloader=FakeDownloader(),
        scheduler=RecordingScheduler(),
    )

    assert events[:5] == [
        "enter:network",
        "enter:ffmpeg",
        "download",
        "exit:ffmpeg",
        "exit:network",
    ]


def test_rerun_marks_stale_active_attempt_interrupted_before_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    import learnnest.pipeline as pipeline
    from learnnest.execution import begin_attempt
    from learnnest.task_store import load_task, write_task_atomic

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    source = _video(tmp_path / "stale.mp4")
    completed = process_video(source, tmp_path / "output")
    task_dir = next((tmp_path / "output" / "视频学习素材").iterdir())
    stale = begin_attempt(
        completed,
        reason="resume",
        from_stage="transcript",
        now=datetime(2026, 7, 12, 1, 0, tzinfo=UTC),
    )
    write_task_atomic(task_dir, stale)

    resumed = pipeline.rerun_task(task_dir, "transcript", reason="resume")

    assert resumed == load_task(task_dir)
    assert [attempt.status for attempt in resumed.attempts] == [
        "completed",
        "interrupted",
        "completed",
    ]
    assert [attempt.reason for attempt in resumed.attempts] == [
        "initial",
        "resume",
        "resume",
    ]


def test_process_source_reports_persisted_batch_attempt_before_heavy_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.sources import parse_source
    from learnnest.task_store import load_task

    observed: list[tuple[str, str]] = []

    def on_started(task):
        task_dir = next((tmp_path / "output" / "视频学习素材").iterdir())
        persisted = load_task(task_dir)
        assert persisted.active_attempt_id == task.active_attempt_id
        observed.append((task.task_id, task.active_attempt_id))

    monkeypatch.setattr(pipeline, "probe_video", lambda path: _probe())
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    source = _video(tmp_path / "batch-linked.mp4")

    completed = pipeline.process_source(
        parse_source(str(source)),
        tmp_path / "output",
        batch_id="batch-1",
        on_attempt_started=on_started,
    )

    assert observed == [(completed.task_id, completed.attempts[0].attempt_id)]
    assert completed.attempts[0].batch_id == "batch-1"


def test_process_url_download_failure_persists_failed_source_report(
    tmp_path: Path,
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.downloader import DownloaderError
    from learnnest.sources import parse_source

    class FakeDownloader:
        def acquire(self, source, task_dir, *, source_fingerprint):
            raise DownloaderError("可手动下载后按本地文件处理")

    with pytest.raises(PipelineError, match="手动下载"):
        pipeline.process_source(
            parse_source("https://example.com/watch?v=1"),
            tmp_path,
            "evidence",
            downloader=FakeDownloader(),
        )

    task_dir = next((tmp_path / "视频学习素材").iterdir())
    failed = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    assert failed["stages"]["source"] == "failed"
    assert "手动下载" in (task_dir / "process_report.md").read_text(encoding="utf-8")


def test_rerun_task_keeps_an_evidence_profile_evidence_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    task = process_video(_video(tmp_path / "note.mp4"), tmp_path / "output", "evidence")
    task_dir = tmp_path / "output" / "视频学习素材" / f"note--{task.task_id[-8:]}"

    rerun = rerun_task(task_dir, "note")

    assert rerun.profile == "evidence"
    assert rerun.stages["note"] is StageStatus.SKIPPED
    assert rerun.stages["publish"] is StageStatus.SKIPPED
    assert not (tmp_path / "output" / "视频学习笔记" / "note.md").exists()


def test_rerun_ocr_preserves_a_note_profile_after_the_initial_ocr_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    fail_ocr = True

    def worker(args: list[str]) -> None:
        nonlocal fail_ocr
        if args[0] == "ocr" and fail_ocr:
            raise RuntimeError("temporary OCR failure")
        _fake_worker(args)

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)

    with pytest.raises(PipelineError, match="temporary OCR failure"):
        process_video(_video(tmp_path / "resume-note.mp4"), tmp_path / "output", "note")

    task_dir = next((tmp_path / "output" / "视频学习素材").glob("resume-note--*"))
    failed = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    assert failed["profile"] == "note"
    fail_ocr = False

    rerun = rerun_task(task_dir, "ocr")

    assert rerun.profile == "note"
    assert rerun.stages["note"] is StageStatus.COMPLETED
    assert rerun.stages["publish"] is StageStatus.COMPLETED
    assert (tmp_path / "output" / "视频学习笔记" / "resume-note.md").is_file()


def test_rerun_task_marks_requested_stage_failed_when_upstream_artifact_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    task = process_video(
        _video(tmp_path / "broken-rerun.mp4"), tmp_path / "output", "evidence"
    )
    task_dir = (
        tmp_path / "output" / "视频学习素材" / f"broken-rerun--{task.task_id[-8:]}"
    )
    (task_dir / "frames.json").unlink()

    with pytest.raises(PipelineError, match="frames.json"):
        rerun_task(task_dir, "ocr")

    failed = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    assert failed["stages"]["ocr"] == "failed"
    assert failed["active_attempt_id"] is None
    assert failed["attempts"][-1]["status"] == "failed"
    assert failed["attempts"][-1]["failed_stage"] == "ocr"


def test_rerun_validation_failure_keeps_the_final_stage_as_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", _fake_worker)
    task = process_video(
        _video(tmp_path / "validate-rerun.mp4"), tmp_path / "output", "evidence"
    )
    task_dir = (
        tmp_path / "output" / "视频学习素材" / f"validate-rerun--{task.task_id[-8:]}"
    )
    monkeypatch.setattr(
        pipeline, "validate_task", lambda task_dir: ["invalid content pack"]
    )

    with pytest.raises(PipelineError, match="invalid content pack"):
        rerun_task(task_dir, "ocr")

    failed = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    assert failed["stages"]["ocr"] == "completed"
    assert failed["stages"]["content_pack"] == "failed"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "segments": [
                {"id": "tr_0001", "start_ms": 1_000, "end_ms": 0, "text": "坏时间"}
            ]
        },
    ],
)
def test_process_rejects_invalid_asr_worker_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, object]
) -> None:
    import learnnest.pipeline as pipeline

    def worker(args: list[str]) -> None:
        output = Path(args[2])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)

    with pytest.raises(PipelineError, match="ASR worker output"):
        process_video(_video(tmp_path / "bad-asr.mp4"), tmp_path / "output", "evidence")

    task_json = next((tmp_path / "output" / "视频学习素材").glob("*/task.json"))
    failed = json.loads(task_json.read_text(encoding="utf-8"))
    assert failed["stages"]["transcript"] == "failed"
    assert "ASR worker output" in (task_json.parent / "process_report.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("payload", [{}, {"items": [{"confidence": 0.9}]}])
def test_process_rejects_invalid_ocr_worker_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, object]
) -> None:
    import learnnest.pipeline as pipeline

    def worker(args: list[str]) -> None:
        output = Path(args[2])
        output.parent.mkdir(parents=True, exist_ok=True)
        if args[0] == "asr":
            output.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "provider": "faster-whisper",
                        "model": "tiny",
                        "segments": [
                            {
                                "id": "tr_0001",
                                "start_ms": 0,
                                "end_ms": 1_000,
                                "text": "设置",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
        else:
            output.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)

    with pytest.raises(PipelineError, match="OCR worker output"):
        process_video(_video(tmp_path / "bad-ocr.mp4"), tmp_path / "output", "evidence")

    task_json = next((tmp_path / "output" / "视频学习素材").glob("*/task.json"))
    failed = json.loads(task_json.read_text(encoding="utf-8"))
    assert failed["stages"]["ocr"] == "failed"
    assert "OCR worker output" in (task_json.parent / "process_report.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("missing", ["schema_version", "provider", "model"])
def test_process_rejects_asr_worker_payload_missing_required_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
) -> None:
    import learnnest.pipeline as pipeline

    payload: dict[str, object] = {
        "schema_version": "1.0",
        "provider": "faster-whisper",
        "model": "tiny",
        "segments": [{"id": "tr_0001", "start_ms": 0, "end_ms": 1_000, "text": "设置"}],
    }
    del payload[missing]

    def worker(args: list[str]) -> None:
        output = Path(args[2])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)

    with pytest.raises(PipelineError, match="ASR worker output"):
        process_video(
            _video(tmp_path / f"missing-asr-{missing}.mp4"),
            tmp_path / "output",
            "evidence",
        )

    task_json = next((tmp_path / "output" / "视频学习素材").glob("*/task.json"))
    failed = json.loads(task_json.read_text(encoding="utf-8"))
    assert failed["stages"]["transcript"] == "failed"
    assert failed["provider"] is None
    assert failed["model"] is None


@pytest.mark.parametrize("missing", ["schema_version", "provider"])
def test_process_rejects_ocr_worker_payload_missing_required_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
) -> None:
    import learnnest.pipeline as pipeline

    ocr_payload: dict[str, object] = {
        "schema_version": "1.0",
        "provider": "paddleocr",
        "items": [{"text": "设置", "confidence": 0.9}],
    }
    del ocr_payload[missing]

    def worker(args: list[str]) -> None:
        output = Path(args[2])
        output.parent.mkdir(parents=True, exist_ok=True)
        if args[0] == "asr":
            _fake_worker(args)
        else:
            output.write_text(json.dumps(ocr_payload), encoding="utf-8")

    monkeypatch.setattr(
        pipeline, "probe_video", lambda path: _probe(duration_seconds=20)
    )
    monkeypatch.setattr(pipeline, "extract_frame", _fake_ffmpeg)
    monkeypatch.setattr(pipeline.providers, "run_worker", worker)

    with pytest.raises(PipelineError, match="OCR worker output"):
        process_video(
            _video(tmp_path / f"missing-ocr-{missing}.mp4"),
            tmp_path / "output",
            "evidence",
        )

    task_json = next((tmp_path / "output" / "视频学习素材").glob("*/task.json"))
    failed = json.loads(task_json.read_text(encoding="utf-8"))
    assert failed["stages"]["ocr"] == "failed"
    assert failed["providers"] == {"asr": "faster-whisper"}


def test_frame_deduplication_compares_against_every_selected_thumbnail(
    tmp_path: Path,
) -> None:
    import learnnest.pipeline as pipeline

    paths = [tmp_path / f"candidate-{index}.png" for index in range(3)]
    for path, value in zip(paths, [0, 100, 0], strict=True):
        Image.new("L", (16, 16), color=value).save(path)

    selected = pipeline._select_distinct_frames(list(enumerate(paths)))

    assert [timestamp for timestamp, _ in selected] == [0, 1]
