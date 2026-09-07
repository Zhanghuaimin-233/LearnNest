from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import learnnest.task_store as task_store
from learnnest.execution_models import TaskAttempt
from learnnest.identities import normalize_local_source
from learnnest.task_store import (
    complete_task_goal,
    create_task,
    load_task,
    write_task_atomic,
)


def test_task_store_creates_persists_and_loads_a_task_atomically(
    tmp_path: Path,
) -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
    )

    destination = write_task_atomic(tmp_path, task)

    assert destination == tmp_path / "task.json"
    assert load_task(destination) == task
    assert list(tmp_path.glob("*.tmp")) == []


def test_task_store_keeps_logical_url_separate_from_acquired_media() -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="https://example.com/watch?v=1",
        source_input="https://example.com/watch?v=1",
        source_type="url",
        media_path="downloads/source.mp4",
        source_fingerprint="a1b2c3d4",
        title="url-a1b2c3d4",
    )

    assert task.source_input == "https://example.com/watch?v=1"
    assert task.source_type == "url"
    assert task.media_path == "downloads/source.mp4"


def test_task_store_persists_a_note_type_override(tmp_path: Path) -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        note_type_override="practical_tutorial",
    )

    write_task_atomic(tmp_path, task)

    assert load_task(tmp_path).note_type_override == "practical_tutorial"


def test_task_store_replaces_an_existing_task_json(tmp_path: Path) -> None:
    old_task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="old title",
    )
    new_task = old_task.model_copy(update={"title": "new title"})
    write_task_atomic(tmp_path, old_task)

    write_task_atomic(tmp_path, new_task)

    assert load_task(tmp_path) == new_task
    assert list(tmp_path.glob("*.tmp")) == []


def test_task_store_cleans_up_temporary_file_when_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="old title",
    )
    new_task = old_task.model_copy(update={"title": "new title"})
    write_task_atomic(tmp_path, old_task)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replacement failed")

    monkeypatch.setattr(task_store.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        write_task_atomic(tmp_path, new_task)

    assert load_task(tmp_path) == old_task
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("winerror", [5, 32])
def test_task_store_retries_transient_windows_replacement_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="retried",
    )
    calls = 0
    real_replace = task_store.os.replace

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(winerror, "sharing violation", str(destination))
        real_replace(source, destination)

    monkeypatch.setattr(task_store.os, "replace", flaky_replace)
    monkeypatch.setattr(task_store.time, "sleep", lambda _delay: None)

    assert write_task_atomic(tmp_path, task) == tmp_path / "task.json"
    assert calls == 3
    assert load_task(tmp_path) == task
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("winerror", [5, 32])
def test_task_store_keeps_the_previous_fact_when_windows_conflict_exhausts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int
) -> None:
    old_task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="old",
    )
    new_task = old_task.model_copy(update={"title": "new"})
    write_task_atomic(tmp_path, old_task)
    calls = 0

    def always_conflicted(_source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        raise PermissionError(winerror, "sharing violation", str(destination))

    monkeypatch.setattr(task_store.os, "replace", always_conflicted)
    monkeypatch.setattr(task_store.time, "sleep", lambda _delay: None)

    with pytest.raises(PermissionError):
        write_task_atomic(tmp_path, new_task)

    assert calls == len(task_store._WINDOWS_REPLACE_RETRY_DELAYS) + 1
    assert load_task(tmp_path) == old_task
    assert list(tmp_path.glob("*.tmp")) == []


def test_task_store_does_not_retry_a_non_windows_replacement_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="no retry",
    )
    calls = 0

    def fail_replace(_source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        raise OSError(22, "invalid argument", str(destination))

    monkeypatch.setattr(task_store.os, "replace", fail_replace)
    monkeypatch.setattr(task_store.time, "sleep", lambda _delay: None)

    with pytest.raises(OSError):
        write_task_atomic(tmp_path, task)

    assert calls == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_task_store_does_not_retry_errno_5_outside_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="non-windows error",
    )
    calls = 0

    def fail_replace(_source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        raise OSError(5, "input/output error", str(destination))

    monkeypatch.setattr(task_store, "_IS_WINDOWS", False)
    monkeypatch.setattr(task_store.os, "replace", fail_replace)
    monkeypatch.setattr(task_store.time, "sleep", lambda _delay: None)

    with pytest.raises(OSError):
        write_task_atomic(tmp_path, task)

    assert calls == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_task_store_revalidates_model_copy_before_persisting(tmp_path: Path) -> None:
    task = create_task(
        task_id="20260712-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
    )
    write_task_atomic(tmp_path, task)
    invalid = task.model_copy(
        update={
            "attempts": [
                TaskAttempt(
                    attempt_id="attempt-0001",
                    ordinal=1,
                    reason="initial",
                    from_stage="source",
                    status="running",
                    started_at=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
                )
            ]
        }
    )

    with pytest.raises(ValueError, match="active_attempt_id"):
        write_task_atomic(tmp_path, invalid)

    assert load_task(tmp_path) == task


def test_task_store_migrates_legacy_local_identity_with_windows_path_rules(
    tmp_path: Path,
) -> None:
    source = tmp_path / "MixedCase" / "Lesson.MP4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    legacy_path = str(source).upper().replace("/", "\\")
    payload = {
        "schema_version": "1.0",
        "task_id": "20260712-legacy01",
        "source_path": legacy_path,
        "source_fingerprint": "legacy-fingerprint",
        "title": "legacy",
    }
    task_json = tmp_path / "task.json"
    task_json.write_text(json.dumps(payload), encoding="utf-8")

    migrated = load_task(task_json)

    assert migrated.identities is not None
    assert migrated.identities.normalized_source == normalize_local_source(source)
    assert migrated.note_type_override is None
    assert json.loads(task_json.read_text(encoding="utf-8")) == payload


def test_task_store_migrates_missing_relative_legacy_source_from_task_directory(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "视频学习素材" / "legacy--offline01"
    task_dir.mkdir(parents=True)
    payload = {
        "schema_version": "1.0",
        "task_id": "20260712-offline01",
        "source_path": "offline/lesson.mp4",
        "source_fingerprint": "legacy-offline-fingerprint",
        "title": "offline legacy",
    }
    task_json = task_dir / "task.json"
    original = json.dumps(payload)
    task_json.write_text(original, encoding="utf-8")

    migrated = load_task(task_json)

    assert migrated.identities is not None
    assert migrated.identities.normalized_source == normalize_local_source(
        task_dir / "offline" / "lesson.mp4"
    )
    assert task_json.read_text(encoding="utf-8") == original


def test_identity_lookup_lazily_matches_legacy_task_by_existing_media_bytes(
    tmp_path: Path,
) -> None:
    from learnnest.execution_models import SourceIdentities
    from learnnest.identities import stream_sha256
    from learnnest.task_store import find_task_by_identities

    vault = tmp_path / "vault"
    media = tmp_path / "legacy.mp4"
    media.write_bytes(b"legacy-content-bytes")
    task_dir = vault / "视频学习素材" / "legacy--a1b2c3d4"
    task_dir.mkdir(parents=True)
    payload = {
        "schema_version": "1.0",
        "task_id": "20260712-a1b2c3d4",
        "source_path": str(media),
        "source_fingerprint": "legacy-path-fingerprint",
        "title": "legacy",
        "stages": {"content_pack": "completed"},
    }
    (task_dir / "task.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    found = find_task_by_identities(
        vault,
        SourceIdentities(
            normalized_source="C:/different/copy.mp4",
            content_sha256=stream_sha256(media),
        ),
        "different-fingerprint",
    )

    assert found is not None
    assert found[1].task_id == "20260712-a1b2c3d4"


def _base_create(*, now: datetime) -> task_store.TaskRecord:
    return create_task(
        task_id="20260907-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        now=now,
    )


def test_task_store_new_task_stamps_all_three_time_facts(tmp_path: Path) -> None:
    created = datetime(2026, 9, 7, 2, 30, tzinfo=UTC)

    task = _base_create(now=created)

    assert task.created_at == created
    assert task.updated_at == created
    assert task.completed_at is None


def test_task_store_frees_created_at_and_advances_updated_at_on_each_write(
    tmp_path: Path,
) -> None:
    created = datetime(2026, 9, 7, 2, 30, tzinfo=UTC)
    first_write = datetime(2026, 9, 7, 2, 31, tzinfo=UTC)
    second_write = datetime(2026, 9, 7, 2, 45, tzinfo=UTC)
    task = _base_create(now=created)

    write_task_atomic(tmp_path, task, now=first_write)
    write_task_atomic(tmp_path, task, now=second_write)

    persisted = load_task(tmp_path)
    assert persisted.created_at == created
    assert persisted.updated_at == second_write


def test_task_store_rejects_naive_or_reversed_task_times(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        _base_create(now=datetime(2026, 9, 7, 10, 0))

    created = datetime(2026, 9, 7, 2, 30, tzinfo=UTC)
    task = _base_create(now=created)
    with pytest.raises(ValueError, match="created_at"):
        write_task_atomic(
            tmp_path,
            complete_task_goal(task, now=datetime(2026, 9, 7, 2, 29, tzinfo=UTC)),
            now=datetime(2026, 9, 7, 2, 31, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="created_at"):
        write_task_atomic(
            tmp_path,
            task,
            now=datetime(2026, 9, 7, 2, 29, tzinfo=UTC),
        )


def test_task_store_completed_at_is_written_once_and_never_overwritten(
    tmp_path: Path,
) -> None:
    created = datetime(2026, 9, 7, 2, 30, tzinfo=UTC)
    completed = datetime(2026, 9, 7, 3, 0, tzinfo=UTC)
    rerun_write = datetime(2026, 9, 7, 3, 30, tzinfo=UTC)
    write_task_atomic(tmp_path, _base_create(now=created), now=created)

    write_task_atomic(
        tmp_path,
        complete_task_goal(load_task(tmp_path), now=completed),
        now=completed,
    )
    write_task_atomic(tmp_path, load_task(tmp_path), now=rerun_write)

    persisted = load_task(tmp_path)
    assert persisted.created_at == created
    assert persisted.updated_at == rerun_write
    assert persisted.completed_at == completed


def test_task_store_completed_at_silences_later_attempts(tmp_path: Path) -> None:
    first = datetime(2026, 9, 7, 3, 0, tzinfo=UTC)
    later = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
    write_task_atomic(
        tmp_path,
        complete_task_goal(_base_create(now=first), now=first),
        now=first,
    )

    same = complete_task_goal(load_task(tmp_path), now=later)

    assert same.completed_at == first


def test_task_store_failed_write_does_not_fabricate_success_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = datetime(2026, 9, 7, 2, 30, tzinfo=UTC)
    success_write = datetime(2026, 9, 7, 2, 40, tzinfo=UTC)
    write_task_atomic(tmp_path, _base_create(now=created), now=success_write)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replacement failed")

    monkeypatch.setattr(task_store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failed"):
        write_task_atomic(
            tmp_path,
            complete_task_goal(load_task(tmp_path), now=success_write),
            now=datetime(2026, 9, 7, 3, 0, tzinfo=UTC),
        )

    persisted = load_task(tmp_path)
    assert persisted.updated_at == success_write
    assert persisted.completed_at is None
