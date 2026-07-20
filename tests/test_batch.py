from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from learnnest.models import StageStatus, TaskRecord
from learnnest.sources import parse_source
from learnnest.task_store import write_task_atomic


def _video(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return path


def test_batch_continues_after_failure_and_skips_in_batch_duplicate(
    tmp_path: Path,
) -> None:
    from learnnest.batch import run_batch

    first = parse_source(str(_video(tmp_path / "a.mp4")))
    failing = parse_source("https://example.com/fail")
    third = parse_source(str(_video(tmp_path / "b.mp4")))
    calls: list[str] = []

    def processor(source, output_root, profile):
        calls.append(source.input)
        if source.input_type == "url":
            raise RuntimeError("adapter unavailable")
        return TaskRecord(
            task_id=f"20260711-{len(calls):08d}",
            source_path=source.input,
            source_fingerprint=f"fingerprint-{len(calls)}",
            title=Path(source.input).stem,
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )

    manifest = run_batch(
        [first, failing, third, first],
        tmp_path / "vault",
        "evidence",
        processor=processor,
        now=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )

    assert calls == [first.input, failing.input, third.input]
    assert [result.status for result in manifest.results] == [
        "completed",
        "failed",
        "completed",
        "skipped_duplicate",
    ]
    assert manifest.completed_count == 2
    assert manifest.failed_count == 1
    assert manifest.skipped_count == 1
    batch_dir = tmp_path / "vault" / "视频学习批次" / manifest.batch_id
    assert (batch_dir / "batch.json").is_file()
    report = (batch_dir / "batch_report.md").read_text(encoding="utf-8")
    assert "completed: 2" in report
    assert "failed: 1" in report
    assert "adapter unavailable" in report


def test_batch_forwards_runtime_downloader_to_process_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import learnnest.batch as batch_module

    marker = object()
    received: list[object | None] = []

    def fake_process_source(
        source,
        output_root,
        profile,
        *,
        downloader=None,
        **kwargs,
    ):
        del source, output_root, kwargs
        received.append(downloader)
        return TaskRecord(
            task_id="task-runtime-downloader",
            source_path="https://www.douyin.com/video/101",
            source_input="https://www.douyin.com/video/101",
            source_type="url",
            source_fingerprint="fingerprint-runtime-downloader",
            title="视频",
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )

    monkeypatch.setattr(batch_module, "process_source", fake_process_source)
    source = parse_source("https://www.douyin.com/video/101")

    batch_module.run_batch(
        [source],
        tmp_path / "vault",
        "evidence",
        processor=fake_process_source,
        downloader=marker,
        now=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
    )

    assert received == [marker]


def test_batch_manifest_exists_before_first_processor_and_updates_each_item(
    tmp_path: Path,
) -> None:
    from learnnest.batch import run_batch
    from learnnest.batch_store import load_batch

    first = parse_source(str(_video(tmp_path / "a.mp4")))
    second = parse_source(str(_video(tmp_path / "b.mp4")))
    output_root = tmp_path / "vault"
    observed: list[tuple[str, list[str]]] = []

    def processor(source, root, profile):
        batch_json = next((root / "视频学习批次").glob("*/batch.json"))
        manifest = load_batch(batch_json)
        observed.append((manifest.status, [item.status for item in manifest.results]))
        return TaskRecord(
            task_id=f"20260712-{len(observed):08d}",
            source_path=source.input,
            source_fingerprint=f"fingerprint-{len(observed)}",
            title=Path(source.input).stem,
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )

    manifest = run_batch(
        [first, second],
        output_root,
        processor=processor,
        now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
    )

    assert observed == [
        ("running", ["running", "pending"]),
        ("running", ["completed", "running"]),
    ]
    assert manifest.status == "completed"
    assert manifest.finished_at is not None


def test_batch_interruption_leaves_explicit_recoverable_manifest(
    tmp_path: Path,
) -> None:
    from learnnest.batch import run_batch
    from learnnest.batch_store import load_batch

    source = parse_source(str(_video(tmp_path / "lesson.mp4")))
    output_root = tmp_path / "vault"

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    try:
        run_batch(
            [source],
            output_root,
            processor=interrupt,
            now=datetime(2026, 7, 12, 12, 1, tzinfo=UTC),
        )
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("test processor must interrupt the batch")

    batch_json = next((output_root / "视频学习批次").glob("*/batch.json"))
    manifest = load_batch(batch_json)
    assert manifest.status == "interrupted"
    assert manifest.results[0].status == "running"
    assert manifest.finished_at is not None


def test_batch_skips_a_historically_completed_source(tmp_path: Path) -> None:
    from learnnest.batch import run_batch
    from learnnest.sources import source_item_fingerprint

    video = _video(tmp_path / "lesson.mp4")
    source = parse_source(str(video))
    fingerprint = source_item_fingerprint(source)
    task_dir = tmp_path / "vault" / "视频学习素材" / f"lesson--{fingerprint[:8]}"
    write_task_atomic(
        task_dir,
        TaskRecord(
            task_id=f"20260711-{fingerprint[:8]}",
            source_path=source.input,
            source_fingerprint=fingerprint,
            title="lesson",
            stages={"content_pack": StageStatus.COMPLETED},
        ),
    )

    manifest = run_batch(
        [source],
        tmp_path / "vault",
        "evidence",
        processor=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("historical duplicate must not run")
        ),
        now=datetime(2026, 7, 11, 12, 1, tzinfo=UTC),
    )

    assert manifest.results[0].status == "skipped_duplicate"
    assert manifest.results[0].task_id == f"20260711-{fingerprint[:8]}"


def test_batch_json_is_versioned_and_contains_every_source(tmp_path: Path) -> None:
    from learnnest.batch import run_batch

    source = parse_source(str(_video(tmp_path / "lesson.mp4")))
    manifest = run_batch(
        [source],
        tmp_path / "vault",
        "evidence",
        processor=lambda item, root, profile: TaskRecord(
            task_id="20260711-a1b2c3d4",
            source_path=item.input,
            source_fingerprint="a1b2c3d4",
            title="lesson",
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        ),
        now=datetime(2026, 7, 11, 12, 2, tzinfo=UTC),
    )
    payload = json.loads(
        (
            tmp_path / "vault" / "视频学习批次" / manifest.batch_id / "batch.json"
        ).read_text(encoding="utf-8")
    )

    assert payload["schema_version"] == "2.0"
    assert payload["status"] == "completed"
    assert len(payload["results"]) == 1
    assert payload["results"][0]["input"] == source.input


def test_batch_persists_complete_source_item_metadata(tmp_path: Path) -> None:
    from learnnest.batch import run_batch

    source = parse_source(str(_video(tmp_path / "metadata.mp4"))).model_copy(
        update={
            "title": "自定义标题",
            "content_type": "course",
            "tags": ["python", "video"],
        }
    )
    manifest = run_batch(
        [source],
        tmp_path / "vault",
        processor=lambda item, root, profile: TaskRecord(
            task_id="20260712-metadata",
            source_path=item.input,
            source_fingerprint="metadata-fingerprint",
            title=item.title or "metadata",
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        ),
        now=datetime(2026, 7, 12, 12, 3, tzinfo=UTC),
    )

    assert manifest.results[0].source == source
    batch_json = tmp_path / "vault" / "视频学习批次" / manifest.batch_id / "batch.json"
    payload = json.loads(batch_json.read_text(encoding="utf-8"))
    assert payload["results"][0]["source"] == source.model_dump(mode="json")


@pytest.mark.parametrize("interrupt_position", [1, 2, 3])
def test_resume_batch_continues_from_first_unfinished_item_without_repeating_completed(
    tmp_path: Path, interrupt_position: int
) -> None:
    from learnnest.batch import resume_batch, run_batch
    from learnnest.batch_store import load_batch

    sources = [
        parse_source(str(_video(tmp_path / f"{index}.mp4"))) for index in range(1, 4)
    ]
    output_root = tmp_path / "vault"
    initial_calls: list[str] = []

    def interrupting_processor(source, root, profile):
        initial_calls.append(source.input)
        if len(initial_calls) == interrupt_position:
            raise KeyboardInterrupt
        return TaskRecord(
            task_id=f"20260712-initial{len(initial_calls)}",
            source_path=source.input,
            source_fingerprint=f"initial-{len(initial_calls)}",
            title=Path(source.input).stem,
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )

    with pytest.raises(KeyboardInterrupt):
        run_batch(
            sources,
            output_root,
            processor=interrupting_processor,
            now=datetime(2026, 7, 12, 12, 4, tzinfo=UTC),
        )
    batch_dir = next((output_root / "视频学习批次").iterdir())
    interrupted = load_batch(batch_dir)
    resumed_calls: list[str] = []

    def resumed_processor(source, root, profile):
        resumed_calls.append(source.input)
        return TaskRecord(
            task_id=f"20260712-resumed{len(resumed_calls)}",
            source_path=source.input,
            source_fingerprint=f"resumed-{len(resumed_calls)}",
            title=Path(source.input).stem,
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )

    completed = resume_batch(
        batch_dir,
        processor=resumed_processor,
        now=datetime(2026, 7, 12, 12, 5, tzinfo=UTC),
    )

    assert interrupted.status == "interrupted"
    assert resumed_calls == [
        source.input for source in sources[interrupt_position - 1 :]
    ]
    assert completed.status == "completed"
    assert [item.status for item in completed.results] == [
        "completed",
        "completed",
        "completed",
    ]


def test_batch_ids_do_not_collide_for_same_sources_in_same_second(
    tmp_path: Path,
) -> None:
    from learnnest.batch import run_batch

    source = parse_source(str(_video(tmp_path / "same.mp4")))
    now = datetime(2026, 7, 12, 12, 6, tzinfo=UTC)

    def processor(item, root, profile):
        return TaskRecord(
            task_id="20260712-same",
            source_path=item.input,
            source_fingerprint="same",
            title="same",
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )

    first = run_batch([source], tmp_path / "vault", processor=processor, now=now)
    second = run_batch([source], tmp_path / "vault", processor=processor, now=now)

    assert first.batch_id != second.batch_id
    assert len(list((tmp_path / "vault" / "视频学习批次").iterdir())) == 2


def test_default_batch_links_task_and_attempt_before_heavy_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.pipeline as pipeline
    from learnnest.batch import run_batch
    from learnnest.batch_store import load_batch

    source = parse_source(str(_video(tmp_path / "linked.mp4")))
    output_root = tmp_path / "vault"

    def interrupting_probe(path):
        del path
        batch_json = next((output_root / "视频学习批次").glob("*/batch.json"))
        running = load_batch(batch_json)
        assert running.results[0].status == "running"
        assert running.results[0].task_id is not None
        assert running.results[0].attempt_id is not None
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "probe_video", interrupting_probe)

    with pytest.raises(KeyboardInterrupt):
        run_batch(
            [source],
            output_root,
            now=datetime(2026, 7, 12, 12, 7, tzinfo=UTC),
        )

    interrupted = load_batch(next((output_root / "视频学习批次").iterdir()))
    assert interrupted.results[0].task_id is not None
    assert interrupted.results[0].attempt_id is not None


def test_resume_batch_routes_linked_running_task_through_resume_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.batch as batch_module
    from learnnest.batch import resume_batch
    from learnnest.batch_models import BatchItemResult, BatchManifest
    from learnnest.batch_store import write_batch_atomic
    from learnnest.execution import begin_attempt, finish_attempt
    from learnnest.task_store import write_task_atomic

    source = parse_source(str(_video(tmp_path / "resume-linked.mp4")))
    root = tmp_path / "vault"
    task_dir = root / "视频学习素材" / "resume-linked--linked01"
    running_task = begin_attempt(
        TaskRecord(
            task_id="20260712-linked01",
            source_path=source.input,
            source_fingerprint="linked-fingerprint",
            title="resume linked",
            stages={
                "source": StageStatus.RUNNING,
                "content_pack": StageStatus.COMPLETED,
            },
        ),
        reason="initial",
        from_stage="source",
        now=datetime(2026, 7, 12, 11, 59, tzinfo=UTC),
        batch_id="batch-linked",
    )
    write_task_atomic(task_dir, running_task)
    batch_dir = root / "视频学习批次" / "batch-linked"
    manifest = BatchManifest(
        batch_id="batch-linked",
        status="interrupted",
        created_at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
        profile="evidence",
        results=[
            BatchItemResult(
                position=1,
                source=source,
                input=source.input,
                input_type=source.input_type,
                source_fingerprint="linked-fingerprint",
                status="running",
                task_id=running_task.task_id,
                attempt_id=running_task.active_attempt_id,
            )
        ],
    )
    write_batch_atomic(batch_dir, manifest)
    calls: list[str] = []

    def fake_recover(task_path, **kwargs):
        calls.append(str(task_path))
        interrupted = finish_attempt(
            running_task,
            status="interrupted",
            now=datetime(2026, 7, 12, 12, 1, tzinfo=UTC),
        )
        resumed = begin_attempt(
            interrupted,
            reason="resume",
            from_stage="source",
            now=datetime(2026, 7, 12, 12, 1, tzinfo=UTC),
            batch_id="batch-linked",
        )
        return finish_attempt(
            resumed,
            status="completed",
            now=datetime(2026, 7, 12, 12, 2, tzinfo=UTC),
        )

    monkeypatch.setattr(batch_module, "recover_task", fake_recover)

    completed = resume_batch(
        batch_dir,
        now=datetime(2026, 7, 12, 12, 3, tzinfo=UTC),
    )

    assert calls == [str(task_dir)]
    assert completed.status == "completed"
    assert completed.results[0].task_id == running_task.task_id
    assert completed.results[0].attempt_id is not None
    assert completed.results[0].attempt_id != running_task.active_attempt_id


def test_two_resumers_cannot_advance_the_same_batch(tmp_path: Path) -> None:
    from learnnest.batch import resume_batch, run_batch
    from learnnest.locks import LockUnavailable, batch_lock

    source = parse_source(str(_video(tmp_path / "locked-batch.mp4")))
    root = tmp_path / "vault"

    with pytest.raises(KeyboardInterrupt):
        run_batch(
            [source],
            root,
            processor=lambda *args: (_ for _ in ()).throw(KeyboardInterrupt),
            now=datetime(2026, 7, 12, 12, 8, tzinfo=UTC),
        )
    batch_dir = next((root / "视频学习批次").iterdir())
    batch_id = batch_dir.name

    with batch_lock(root, batch_id, timeout=0):
        with pytest.raises(LockUnavailable, match=batch_id):
            resume_batch(batch_dir, lock_timeout=0)


def test_resume_keeps_linked_item_running_when_task_owner_is_busy(
    tmp_path: Path,
) -> None:
    from learnnest.batch import resume_batch
    from learnnest.batch_models import BatchItemResult, BatchManifest
    from learnnest.batch_store import load_batch, write_batch_atomic
    from learnnest.execution import begin_attempt
    from learnnest.locks import LockUnavailable, task_lock

    source = parse_source(str(_video(tmp_path / "busy-linked.mp4")))
    root = tmp_path / "vault"
    task_id = "20260712-busy0001"
    task_dir = root / "视频学习素材" / "busy-linked--busy0001"
    task = begin_attempt(
        TaskRecord(
            task_id=task_id,
            source_path=source.input,
            source_fingerprint="busy-fingerprint",
            title="busy linked",
            stages={"source": StageStatus.RUNNING},
        ),
        reason="initial",
        from_stage="source",
        now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
    )
    write_task_atomic(task_dir, task)
    batch_dir = root / "视频学习批次" / "batch-busy-linked"
    write_batch_atomic(
        batch_dir,
        BatchManifest(
            batch_id="batch-busy-linked",
            status="interrupted",
            created_at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            updated_at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            finished_at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            profile="evidence",
            results=[
                BatchItemResult(
                    position=1,
                    source=source,
                    input=source.input,
                    input_type=source.input_type,
                    source_fingerprint="busy-fingerprint",
                    status="running",
                    task_id=task_id,
                    attempt_id=task.active_attempt_id,
                )
            ],
        ),
    )

    with task_lock(root, task_id, timeout=0):
        with pytest.raises(LockUnavailable, match=task_id):
            resume_batch(
                batch_dir,
                now=datetime(2026, 7, 12, 12, 3, tzinfo=UTC),
            )

    persisted = load_batch(batch_dir)
    assert persisted.status == "interrupted"
    assert persisted.results[0].status == "running"
    assert persisted.results[0].failure is None


def test_resume_recovers_callback_gap_by_source_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.batch as batch_module
    from learnnest.batch import resume_batch
    from learnnest.batch_models import BatchItemResult, BatchManifest
    from learnnest.batch_store import write_batch_atomic
    from learnnest.execution import begin_attempt, finish_attempt

    source = parse_source(str(_video(tmp_path / "callback-gap.mp4")))
    root = tmp_path / "vault"
    fingerprint = "callback-gap-fingerprint"
    task_dir = root / "视频学习素材" / "callback-gap--gap00001"
    running = begin_attempt(
        TaskRecord(
            task_id="20260712-gap00001",
            source_path=source.input,
            source_fingerprint=fingerprint,
            title="callback gap",
            stages={"source": StageStatus.RUNNING},
        ),
        reason="initial",
        from_stage="source",
        now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
    )
    write_task_atomic(task_dir, running)
    batch_dir = root / "视频学习批次" / "batch-callback-gap"
    write_batch_atomic(
        batch_dir,
        BatchManifest(
            batch_id="batch-callback-gap",
            status="interrupted",
            created_at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            updated_at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            finished_at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            profile="evidence",
            results=[
                BatchItemResult(
                    position=1,
                    source=source,
                    input=source.input,
                    input_type=source.input_type,
                    source_fingerprint=fingerprint,
                    status="running",
                )
            ],
        ),
    )
    calls: list[str] = []

    def fake_recover(task_path, **kwargs):
        calls.append(str(task_path))
        interrupted = finish_attempt(
            running,
            status="interrupted",
            now=datetime(2026, 7, 12, 12, 1, tzinfo=UTC),
        )
        resumed = begin_attempt(
            interrupted,
            reason="resume",
            from_stage="source",
            now=datetime(2026, 7, 12, 12, 1, tzinfo=UTC),
            batch_id="batch-callback-gap",
        )
        return finish_attempt(
            resumed,
            status="completed",
            now=datetime(2026, 7, 12, 12, 2, tzinfo=UTC),
        )

    monkeypatch.setattr(batch_module, "recover_task", fake_recover, raising=False)

    completed = resume_batch(
        batch_dir,
        now=datetime(2026, 7, 12, 12, 3, tzinfo=UTC),
    )

    assert calls == [str(task_dir)]
    assert completed.status == "completed"
    assert completed.results[0].task_id == running.task_id


def test_url_duplicate_keeps_actual_batch_task_and_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.batch as batch_module
    from learnnest.batch import run_batch
    from learnnest.execution import begin_attempt, finish_attempt
    from learnnest.sources import source_item_fingerprint

    source = parse_source("https://example.com/video/duplicate")
    root = tmp_path / "vault"
    fingerprint = source_item_fingerprint(source)
    canonical = TaskRecord(
        task_id="20260712-canonical",
        source_path="https://example.com/original",
        source_type="url",
        source_fingerprint="canonical-fingerprint",
        title="canonical",
        stages={"content_pack": StageStatus.COMPLETED},
    )
    write_task_atomic(root / "视频学习素材" / "canonical--canonical", canonical)

    def fake_process(item, output_root, profile, **kwargs):
        running = begin_attempt(
            TaskRecord(
                task_id="20260712-duplicate",
                source_path=item.input,
                source_type="url",
                source_fingerprint=fingerprint,
                title="duplicate",
                profile=profile,
                stages={"source": StageStatus.COMPLETED},
                duplicate_of_task_id=canonical.task_id,
            ),
            reason="initial",
            from_stage="source",
            now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
            batch_id=kwargs["batch_id"],
        )
        duplicate_dir = output_root / "视频学习素材" / "duplicate--duplicate"
        write_task_atomic(duplicate_dir, running)
        kwargs["on_attempt_started"](running)
        finished = finish_attempt(
            running,
            status="skipped_duplicate",
            now=datetime(2026, 7, 12, 12, 1, tzinfo=UTC),
        )
        write_task_atomic(duplicate_dir, finished)
        return canonical

    monkeypatch.setattr(batch_module, "process_source", fake_process)
    manifest = run_batch(
        [source],
        root,
        processor=fake_process,
        now=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
    )

    item = manifest.results[0]
    assert item.status == "skipped_duplicate"
    assert item.task_id == "20260712-duplicate"
    assert item.attempt_id is not None


def test_scan_batch_persists_scan_identity_and_cursor_snapshot(tmp_path: Path) -> None:
    from learnnest.batch import run_batch

    manifest = run_batch(
        [],
        tmp_path / "vault",
        kind="scan",
        schedule_id="manual-folder-a1b2c3d4",
        cursor_before='{"entries":{}}',
        cursor_after='{"entries":{"lesson.mp4":"fingerprint"}}',
        now=datetime(2026, 7, 12, 12, 4, tzinfo=UTC),
    )

    assert manifest.status == "completed"
    assert manifest.kind == "scan"
    assert manifest.schedule_id == "manual-folder-a1b2c3d4"
    assert manifest.cursor_before == '{"entries":{}}'
    assert manifest.cursor_after == '{"entries":{"lesson.mp4":"fingerprint"}}'


@pytest.mark.parametrize(
    ("kind", "expected_reason"),
    [("manual", "initial"), ("scan", "initial"), ("scheduled", "scheduled")],
)
def test_batch_sets_new_task_attempt_reason_from_its_kind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
    expected_reason: str,
) -> None:
    import learnnest.batch as batch_module
    from learnnest.batch import run_batch

    source = parse_source(str(_video(tmp_path / f"{kind}.mp4")))
    observed: list[str] = []

    def fake_process(item, output_root, profile, **kwargs):
        del output_root
        observed.append(kwargs["initial_attempt_reason"])
        task = TaskRecord(
            task_id=f"20260712-{kind}",
            source_path=item.input,
            source_fingerprint=f"fingerprint-{kind}",
            title=kind,
            profile=profile,
            stages={"content_pack": StageStatus.COMPLETED},
        )
        kwargs["on_attempt_started"](task)
        return task

    monkeypatch.setattr(batch_module, "process_source", fake_process)

    manifest = run_batch(
        [source],
        tmp_path / "vault",
        processor=fake_process,
        kind=kind,
        now=datetime(2026, 7, 12, 12, 4, tzinfo=UTC),
    )

    assert manifest.status == "completed"
    assert observed == [expected_reason]


def test_batch_started_callback_observes_persisted_manifest_before_items(
    tmp_path: Path,
) -> None:
    from learnnest.batch import run_batch
    from learnnest.batch_store import load_batch

    observed: list[str] = []

    def started(batch_dir, manifest):
        persisted = load_batch(batch_dir)
        assert persisted.batch_id == manifest.batch_id
        assert persisted.results == manifest.results
        observed.append(manifest.batch_id)

    manifest = run_batch(
        [],
        tmp_path / "vault",
        on_batch_started=started,
        now=datetime(2026, 7, 12, 12, 5, tzinfo=UTC),
    )

    assert observed == [manifest.batch_id]
