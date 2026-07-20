from __future__ import annotations

import json
import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from learnnest.execution import begin_attempt, finish_attempt
from learnnest.execution_models import FailureInfo
from learnnest.models import TaskRecord
from learnnest.task_store import write_task_atomic


def _write_task(vault: Path, task_id: str = "20260712-a1b2c3d4") -> Path:
    task_dir = vault / "视频学习素材" / f"lesson--{task_id[-8:]}"
    task = TaskRecord(
        task_id=task_id,
        source_path="C:/videos/lesson.mp4",
        source_fingerprint=task_id[-8:],
        title="lesson",
    )
    started = begin_attempt(
        task,
        reason="initial",
        from_stage="source",
        now=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
    )
    completed = finish_attempt(
        started,
        status="completed",
        now=datetime(2026, 7, 12, 10, 1, tzinfo=UTC),
    )
    write_task_atomic(task_dir, completed)
    return task_dir


def _write_legacy_batch(vault: Path) -> Path:
    batch_dir = vault / "视频学习批次" / "20260712T100000Z-a1b2c3d4"
    batch_dir.mkdir(parents=True)
    payload = {
        "schema_version": "1.0",
        "batch_id": batch_dir.name,
        "profile": "evidence",
        "results": [
            {
                "input": "C:/videos/lesson.mp4",
                "input_type": "local_file",
                "source_fingerprint": "a1b2c3d4",
                "status": "completed",
                "task_id": "20260712-a1b2c3d4",
                "error": None,
            }
        ],
    }
    path = batch_dir / "batch.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _write_empty_batch(
    vault: Path,
    *,
    batch_id: str,
    kind: str,
    schedule_id: str | None,
) -> Path:
    from learnnest.batch_models import BatchManifest
    from learnnest.batch_store import write_batch_atomic

    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    manifest = BatchManifest(
        batch_id=batch_id,
        kind=kind,
        status="completed",
        created_at=now,
        updated_at=now,
        finished_at=now,
        profile="evidence",
        schedule_id=schedule_id,
        results=[],
    )
    return write_batch_atomic(vault / "视频学习批次" / batch_id, manifest)


def test_rebuild_index_projects_tasks_attempts_and_legacy_batches(
    tmp_path: Path,
) -> None:
    from learnnest.index import index_status, query_history, rebuild_index

    vault = tmp_path / "vault"
    _write_task(vault)
    _write_legacy_batch(vault)

    result = rebuild_index(vault)
    status = index_status(vault)
    history = query_history(vault, task_id="20260712-a1b2c3d4")

    assert result.task_count == 1
    assert result.attempt_count == 1
    assert result.batch_count == 1
    assert result.database_path == vault / ".learnnest" / "index.sqlite3"
    assert status.exists is True
    assert status.stale is False
    assert history[0]["task_id"] == "20260712-a1b2c3d4"
    assert history[0]["attempt_status"] == "completed"


def test_history_fact_query_does_not_require_the_sqlite_projection(
    tmp_path: Path,
) -> None:
    from learnnest.index import query_history_facts

    vault = tmp_path / "vault"
    _write_task(vault)

    rows = query_history_facts(vault, task_id="20260712-a1b2c3d4")

    assert rows[0]["task_id"] == "20260712-a1b2c3d4"
    assert rows[0]["attempt_status"] == "completed"


def test_rebuild_projects_schedule_fact_and_detects_schedule_change(
    tmp_path: Path,
) -> None:
    import sqlite3

    from learnnest.index import index_status, rebuild_index
    from learnnest.schedule_models import ScheduleRecord
    from learnnest.schedule_store import write_schedule_atomic

    vault = tmp_path / "vault"
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    schedule = ScheduleRecord(
        schedule_id="folder-indexed",
        status="enabled",
        source={"kind": "folder", "path": "C:/videos", "recursive": False},
        trigger={"kind": "manual"},
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(vault, schedule)

    result = rebuild_index(vault)

    assert result.schedule_count == 1
    assert index_status(vault).schedule_count == 1
    with sqlite3.connect(result.database_path) as connection:
        assert connection.execute("SELECT schedule_id FROM schedules").fetchone() == (
            "folder-indexed",
        )

    write_schedule_atomic(
        vault,
        schedule.model_copy(update={"status": "disabled"}),
    )
    assert index_status(vault).stale is True


def test_rebuild_projects_scan_batch_owned_by_existing_schedule(tmp_path: Path) -> None:
    import sqlite3

    from learnnest.index import rebuild_index
    from learnnest.schedule_models import ScheduleRecord
    from learnnest.schedule_store import write_schedule_atomic

    vault = tmp_path / "vault"
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    write_schedule_atomic(
        vault,
        ScheduleRecord(
            schedule_id="folder-owned",
            status="enabled",
            source={"kind": "folder", "path": "C:/videos", "recursive": False},
            trigger={"kind": "manual"},
            created_at=now,
            updated_at=now,
        ),
    )
    _write_empty_batch(
        vault,
        batch_id="20260712T100000Z-owned01",
        kind="scan",
        schedule_id="folder-owned",
    )

    result = rebuild_index(vault)

    with sqlite3.connect(result.database_path) as connection:
        assert connection.execute(
            "SELECT kind, schedule_id FROM batches"
        ).fetchone() == ("scan", "folder-owned")


@pytest.mark.parametrize(
    ("kind", "schedule_id"),
    [("scan", None), ("scheduled", "missing-schedule")],
)
def test_rebuild_rejects_unowned_automated_batch_and_preserves_previous_database(
    tmp_path: Path,
    kind: str,
    schedule_id: str | None,
) -> None:
    from learnnest.index import IndexRebuildError, rebuild_index

    vault = tmp_path / "vault"
    database = rebuild_index(vault).database_path
    original = database.read_bytes()
    _write_empty_batch(
        vault,
        batch_id=f"20260712T100000Z-{kind}01",
        kind=kind,
        schedule_id=schedule_id,
    )

    with pytest.raises(IndexRebuildError, match="constraint failed"):
        rebuild_index(vault)

    assert database.read_bytes() == original


def test_broken_schedule_rebuild_preserves_previous_database(tmp_path: Path) -> None:
    from learnnest.index import IndexRebuildError, rebuild_index
    from learnnest.schedule_models import ScheduleRecord
    from learnnest.schedule_store import write_schedule_atomic

    vault = tmp_path / "vault"
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    write_schedule_atomic(
        vault,
        ScheduleRecord(
            schedule_id="folder-stable",
            status="enabled",
            source={"kind": "folder", "path": "C:/videos", "recursive": False},
            trigger={"kind": "manual"},
            created_at=now,
            updated_at=now,
        ),
    )
    database = rebuild_index(vault).database_path
    original = database.read_bytes()
    broken = vault / "视频学习批次" / "schedules" / "broken.json"
    broken.write_text("not-json", encoding="utf-8")

    with pytest.raises(IndexRebuildError, match="broken.json"):
        rebuild_index(vault)

    assert database.read_bytes() == original


def test_failed_rebuild_preserves_previous_database(tmp_path: Path) -> None:
    from learnnest.index import IndexRebuildError, rebuild_index

    vault = tmp_path / "vault"
    _write_task(vault)
    database = rebuild_index(vault).database_path
    original = database.read_bytes()
    broken = vault / "视频学习素材" / "broken" / "task.json"
    broken.parent.mkdir()
    broken.write_text("not-json", encoding="utf-8")

    with pytest.raises(IndexRebuildError, match="broken.*task.json"):
        rebuild_index(vault)

    assert database.read_bytes() == original


def test_index_status_detects_changed_fact_file(tmp_path: Path) -> None:
    from learnnest.index import index_status, rebuild_index
    from learnnest.task_store import load_task

    vault = tmp_path / "vault"
    task_dir = _write_task(vault)
    rebuild_index(vault)
    task = load_task(task_dir).model_copy(update={"title": "changed"})
    write_task_atomic(task_dir, task)

    status = index_status(vault)

    assert status.stale is True
    assert "视频学习素材" in status.reason


def test_history_and_queue_queries_refuse_a_stale_projection(tmp_path: Path) -> None:
    from learnnest.index import (
        IndexRebuildError,
        query_failure_queue,
        query_history,
        rebuild_index,
    )
    from learnnest.task_store import load_task

    vault = tmp_path / "vault"
    task_dir = _write_task(vault)
    rebuild_index(vault)
    changed = load_task(task_dir).model_copy(update={"title": "changed"})
    write_task_atomic(task_dir, changed)

    with pytest.raises(IndexRebuildError, match="stale.*index rebuild"):
        query_history(vault)
    with pytest.raises(IndexRebuildError, match="stale.*index rebuild"):
        query_failure_queue(vault)


def test_index_status_reports_corruption_and_rebuild_restores_projection(
    tmp_path: Path,
) -> None:
    from learnnest.index import index_status, rebuild_index

    vault = tmp_path / "vault"
    _write_task(vault)
    database = rebuild_index(vault).database_path
    database.write_bytes(b"not a sqlite database")

    corrupted = index_status(vault)

    assert corrupted.exists is True
    assert corrupted.stale is True
    assert "unreadable" in corrupted.reason

    rebuild_index(vault)
    restored = index_status(vault)
    assert restored.stale is False
    assert restored.task_count == 1


def test_replace_failure_preserves_facts_and_a_later_rebuild_catches_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import learnnest.index as index_module
    from learnnest.index import IndexRebuildError, index_status, rebuild_index

    vault = tmp_path / "vault"
    _write_task(vault, "20260712-first001")
    database = rebuild_index(vault).database_path
    original = database.read_bytes()
    second = _write_task(vault, "20260712-second02")
    real_replace = index_module.os.replace

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("simulated replace failure")

    monkeypatch.setattr(index_module.os, "replace", fail_replace)
    with pytest.raises(IndexRebuildError, match="simulated replace failure"):
        rebuild_index(vault)

    assert second.joinpath("task.json").is_file()
    assert database.read_bytes() == original

    monkeypatch.setattr(index_module.os, "replace", real_replace)
    rebuilt = rebuild_index(vault)
    assert rebuilt.task_count == 2
    assert index_status(vault).stale is False


def test_rebuild_uses_vault_relative_paths_after_move(tmp_path: Path) -> None:
    from learnnest.index import query_history, rebuild_index

    source = tmp_path / "source-vault"
    _write_task(source)
    rebuild_index(source)
    moved = tmp_path / "moved-vault"
    shutil.copytree(source, moved)

    rebuild_index(moved)
    history = query_history(moved, task_id="20260712-a1b2c3d4")

    assert history[0]["task_path"].startswith("视频学习素材/")
    assert str(source) not in history[0]["task_path"]


def test_failure_queue_is_derived_from_retryable_attempts(tmp_path: Path) -> None:
    from learnnest.index import query_failure_queue, rebuild_index

    vault = tmp_path / "vault"
    task_dir = vault / "视频学习素材" / "lesson--a1b2c3d4"
    task = TaskRecord(
        task_id="20260712-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
    )
    started = begin_attempt(
        task,
        reason="retry",
        from_stage="transcript",
        now=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
    )
    failed = finish_attempt(
        started,
        status="failed",
        now=datetime(2026, 7, 12, 10, 1, tzinfo=UTC),
        failed_stage="transcript",
        failure=FailureInfo(
            code="http_503",
            category="provider",
            disposition="retryable",
            safe_summary="HTTP 503 unavailable",
        ),
        next_retry_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    write_task_atomic(task_dir, failed)
    rebuild_index(vault)

    queue = query_failure_queue(vault, now=datetime.now(UTC))

    assert len(queue) == 1
    assert queue[0]["task_id"] == task.task_id
    assert queue[0]["failure_code"] == "http_503"


@pytest.mark.parametrize("paid_stage", ["note", "podcast_script", "tts"])
def test_failure_queue_excludes_retryable_paid_stage_failures(
    tmp_path: Path, paid_stage: str
) -> None:
    from learnnest.index import query_failure_queue, rebuild_index

    vault = tmp_path / "vault"
    task_dir = vault / "视频学习素材" / f"lesson-{paid_stage}--a1b2c3d4"
    task = TaskRecord(
        task_id=f"20260712-{paid_stage}",
        source_path=f"C:/videos/{paid_stage}.mp4",
        source_fingerprint=f"fingerprint-{paid_stage}",
        title=paid_stage,
    )
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
    started = begin_attempt(task, reason="retry", from_stage=paid_stage, now=now)
    failed = finish_attempt(
        started,
        status="failed",
        now=now,
        failed_stage=paid_stage,
        failure=FailureInfo(
            code="http_503",
            category="provider",
            disposition="retryable",
            safe_summary="HTTP 503 unavailable",
        ),
        next_retry_at=now,
    )
    write_task_atomic(task_dir, failed)
    rebuild_index(vault)

    assert query_failure_queue(vault, now=now) == []


def test_failure_queue_only_contains_latest_attempt_and_stops_after_three_failures(
    tmp_path: Path,
) -> None:
    from learnnest.index import query_failure_queue, rebuild_index

    vault = tmp_path / "vault"
    failure = FailureInfo(
        code="http_503",
        category="provider",
        disposition="retryable",
        safe_summary="HTTP 503 unavailable",
    )
    now = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)

    recovered = TaskRecord(
        task_id="20260712-recover1",
        source_path="C:/videos/recovered.mp4",
        source_fingerprint="recovered",
        title="recovered",
    )
    first = begin_attempt(recovered, reason="initial", from_stage="source", now=now)
    first = finish_attempt(
        first,
        status="failed",
        now=now,
        failed_stage="source",
        failure=failure,
        next_retry_at=now,
    )
    second = begin_attempt(first, reason="retry", from_stage="source", now=now)
    recovered = finish_attempt(second, status="completed", now=now)
    write_task_atomic(vault / "视频学习素材" / "recovered--recover1", recovered)

    exhausted = TaskRecord(
        task_id="20260712-exhaust1",
        source_path="C:/videos/exhausted.mp4",
        source_fingerprint="exhausted",
        title="exhausted",
    )
    for ordinal in range(4):
        running = begin_attempt(
            exhausted,
            reason="initial" if ordinal == 0 else "retry",
            from_stage="source",
            now=now,
        )
        exhausted = finish_attempt(
            running,
            status="failed",
            now=now,
            failed_stage="source",
            failure=failure,
            next_retry_at=now if ordinal < 3 else None,
        )
    write_task_atomic(vault / "视频学习素材" / "exhausted--exhaust1", exhausted)

    rebuild_index(vault)

    assert query_failure_queue(vault, now=now) == []


def test_duplicate_task_id_makes_rebuild_fail_without_replacing_index(
    tmp_path: Path,
) -> None:
    from learnnest.index import IndexRebuildError, rebuild_index

    vault = tmp_path / "vault"
    first = _write_task(vault)
    database = rebuild_index(vault).database_path
    original = database.read_bytes()
    duplicate = vault / "视频学习素材" / "duplicate"
    duplicate.mkdir()
    shutil.copy2(first / "task.json", duplicate / "task.json")

    with pytest.raises(IndexRebuildError, match="duplicate task_id"):
        rebuild_index(vault)

    assert database.read_bytes() == original


def test_rebuild_refuses_a_second_process_owner(tmp_path: Path) -> None:
    from learnnest.index import rebuild_index
    from learnnest.locks import LockUnavailable, rebuild_lock

    vault = tmp_path / "vault"
    _write_task(vault)

    with rebuild_lock(vault, timeout=0):
        with pytest.raises(LockUnavailable, match="rebuild"):
            rebuild_index(vault)


def test_task_fact_projection_parses_and_hashes_the_same_single_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.index as index_module

    path = tmp_path / "task.json"
    first = json.dumps(
        {
            "schema_version": "2.0",
            "task_id": "20260712-single01",
            "source_path": "C:/videos/lesson.mp4",
            "source_fingerprint": "single-read",
            "title": "first snapshot",
        }
    ).encode()
    second = first.replace(b"first snapshot", b"other snapshot")
    reads = 0

    def changing_read_bytes(self: Path) -> bytes:
        nonlocal reads
        assert self == path
        reads += 1
        return first if reads == 1 else second

    monkeypatch.setattr(Path, "read_bytes", changing_read_bytes)

    task, digest = index_module._read_task_fact(path)

    assert task.title == "first snapshot"
    assert digest == hashlib.sha256(first).hexdigest()
    assert reads == 1
