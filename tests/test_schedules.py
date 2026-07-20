from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from learnnest.models import StageStatus, TaskRecord
from learnnest.batch import run_batch
from learnnest.schedule_models import ScheduleRecord
from learnnest.schedule_store import load_schedule, schedule_path, write_schedule_atomic
from learnnest.schedules import run_schedule_once, tick_schedules
from learnnest.sources import parse_source


def _record(
    schedule_id: str,
    *,
    source: dict[str, object],
    now: datetime,
    next_tick_at: datetime | None = None,
) -> ScheduleRecord:
    return ScheduleRecord(
        schedule_id=schedule_id,
        status="enabled",
        source=source,
        trigger={"kind": "interval", "every_seconds": 300},
        profile="evidence",
        created_at=now,
        updated_at=now,
        next_tick_at=next_tick_at or now,
    )


def _processor(source, root, profile):
    del root
    return TaskRecord(
        task_id=f"20260712-{Path(source.input).stem[:8]}",
        source_path=source.input,
        source_fingerprint=f"fingerprint-{Path(source.input).stem}",
        title=Path(source.input).stem,
        profile=profile,
        stages={"content_pack": StageStatus.COMPLETED},
    )


def test_due_tick_runs_scheduled_batch_and_commits_candidate_cursor(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    video = tmp_path / "lesson.mp4"
    video.write_bytes(b"video")
    schedule = _record(
        "folder-lessons",
        source={"kind": "folder", "path": str(tmp_path), "recursive": False},
        now=now,
    )
    write_schedule_atomic(root, schedule)
    adapter = SimpleNamespace(
        discover=lambda cursor: SimpleNamespace(
            items=[parse_source(str(video))],
            cursor_after="folder-cursor-v1",
        )
    )

    outcomes = tick_schedules(
        root,
        adapter_factory=lambda record, credentials: adapter,
        processor=_processor,
        now=now,
    )

    assert [(item.schedule_id, item.status) for item in outcomes] == [
        ("folder-lessons", "completed")
    ]
    persisted = load_schedule(schedule_path(root, schedule.schedule_id))
    assert persisted.cursor == "folder-cursor-v1"
    assert persisted.active_batch_id is None
    assert persisted.last_batch_id == outcomes[0].batch_id
    assert persisted.next_tick_at == now + timedelta(seconds=300)
    batch_dir = root / "视频学习批次" / str(outcomes[0].batch_id)
    assert '"kind": "scheduled"' in (batch_dir / "batch.json").read_text(
        encoding="utf-8"
    )


def test_tick_skips_not_due_schedule(tmp_path: Path) -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    write_schedule_atomic(
        root,
        _record(
            "folder-later",
            source={"kind": "folder", "path": str(tmp_path), "recursive": False},
            now=now,
            next_tick_at=now + timedelta(hours=1),
        ),
    )

    assert (
        tick_schedules(
            root,
            adapter_factory=lambda *args: (_ for _ in ()).throw(
                AssertionError("not-due schedule must not construct an adapter")
            ),
            processor=_processor,
            now=now,
        )
        == []
    )


def test_tick_skips_busy_schedule_and_continues_with_other_due_work(
    tmp_path: Path,
) -> None:
    from learnnest.locks import schedule_lock

    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    for schedule_id in ("a-busy", "b-free"):
        write_schedule_atomic(
            root,
            _record(
                schedule_id,
                source={"kind": "folder", "path": str(tmp_path), "recursive": False},
                now=now,
            ),
        )
    adapter = SimpleNamespace(
        discover=lambda cursor: SimpleNamespace(items=[], cursor_after="next")
    )

    with schedule_lock(root, "a-busy", timeout=0):
        outcomes = tick_schedules(
            root,
            adapter_factory=lambda record, credentials: adapter,
            processor=_processor,
            now=now,
        )

    assert [(item.schedule_id, item.status) for item in outcomes] == [
        ("a-busy", "busy"),
        ("b-free", "completed"),
    ]
    busy = load_schedule(schedule_path(root, "a-busy"))
    free = load_schedule(schedule_path(root, "b-free"))
    assert busy.cursor is None
    assert busy.next_tick_at == now
    assert free.cursor == "next"
    assert free.next_tick_at == now + timedelta(seconds=300)


def test_adapter_auth_failure_blocks_only_its_schedule_and_never_persists_secret(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    write_schedule_atomic(
        root,
        _record(
            "douyin-favorites",
            source={"kind": "douyin", "url": "https://douyin.example/favorites"},
            now=now,
        ),
    )
    write_schedule_atomic(
        root,
        _record(
            "folder-safe",
            source={"kind": "folder", "path": str(tmp_path), "recursive": False},
            now=now,
        ),
    )
    sentinel = "cookie-sentinel-never-persist"

    class FailingAdapter:
        def discover(self, cursor):
            del cursor
            raise RuntimeError("login cookie verification required")

    safe = SimpleNamespace(
        discover=lambda cursor: SimpleNamespace(items=[], cursor_after="safe-cursor")
    )

    def factory(record, credentials):
        assert isinstance(credentials, SecretStr)
        assert credentials.get_secret_value() == sentinel
        return FailingAdapter() if record.source.kind == "douyin" else safe

    outcomes = tick_schedules(
        root,
        adapter_factory=factory,
        processor=_processor,
        runtime_credentials=SecretStr(sentinel),
        now=now,
    )

    assert [(item.schedule_id, item.status) for item in outcomes] == [
        ("douyin-favorites", "blocked"),
        ("folder-safe", "completed"),
    ]
    assert load_schedule(schedule_path(root, "douyin-favorites")).status == "blocked"
    for path in root.rglob("*"):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes()


def test_manual_schedule_run_uses_same_foreground_runner(tmp_path: Path) -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    schedule = ScheduleRecord(
        schedule_id="folder-manual",
        status="enabled",
        source={"kind": "folder", "path": str(tmp_path), "recursive": False},
        trigger={"kind": "manual"},
        profile="evidence",
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(root, schedule)
    adapter = SimpleNamespace(
        discover=lambda cursor: SimpleNamespace(items=[], cursor_after="manual-cursor")
    )

    outcome = run_schedule_once(
        root,
        schedule.schedule_id,
        adapter=adapter,
        processor=_processor,
        now=now,
    )

    assert outcome.status == "completed"
    assert load_schedule(schedule_path(root, schedule.schedule_id)).cursor == (
        "manual-cursor"
    )


def test_active_interrupted_batch_resumes_without_rediscovery(tmp_path: Path) -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    video = tmp_path / "resume.mp4"
    video.write_bytes(b"video")
    source = parse_source(str(video))
    schedule = ScheduleRecord(
        schedule_id="folder-resume",
        status="enabled",
        source={"kind": "folder", "path": str(tmp_path), "recursive": False},
        trigger={"kind": "manual"},
        profile="evidence",
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(root, schedule)

    def claim(batch_dir, manifest):
        del batch_dir
        write_schedule_atomic(
            root,
            schedule.model_copy(update={"active_batch_id": manifest.batch_id}),
        )

    try:
        run_batch(
            [source],
            root,
            processor=lambda *args: (_ for _ in ()).throw(KeyboardInterrupt),
            kind="scan",
            schedule_id=schedule.schedule_id,
            cursor_after="resume-cursor",
            on_batch_started=claim,
            now=now,
        )
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("batch fixture must be interrupted")

    adapter = SimpleNamespace(
        discover=lambda cursor: (_ for _ in ()).throw(
            AssertionError("active batch must resume without rediscovery")
        )
    )
    outcome = run_schedule_once(
        root,
        schedule.schedule_id,
        adapter=adapter,
        processor=_processor,
        now=now + timedelta(minutes=1),
    )

    assert outcome.status == "completed"
    persisted = load_schedule(schedule_path(root, schedule.schedule_id))
    assert persisted.cursor == "resume-cursor"
    assert persisted.active_batch_id is None


def test_tick_rechecks_disabled_fact_after_adapter_factory(tmp_path: Path) -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    schedule = _record(
        "folder-disable-race",
        source={"kind": "folder", "path": str(tmp_path), "recursive": False},
        now=now,
    )
    write_schedule_atomic(root, schedule)

    def factory(record, credentials):
        del credentials
        write_schedule_atomic(root, record.model_copy(update={"status": "disabled"}))
        return SimpleNamespace(
            discover=lambda cursor: SimpleNamespace(
                items=[], cursor_after="must-not-commit"
            )
        )

    outcomes = tick_schedules(root, adapter_factory=factory, now=now)

    assert outcomes == []
    persisted = load_schedule(schedule_path(root, schedule.schedule_id))
    assert persisted.status == "disabled"
    assert persisted.cursor is None
    assert list((root / "视频学习批次").glob("*/batch.json")) == []


def test_factory_failure_is_secret_safe_and_does_not_stop_later_schedule(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    for schedule_id in ("a-douyin", "b-folder"):
        write_schedule_atomic(
            root,
            _record(
                schedule_id,
                source=(
                    {"kind": "douyin", "url": "https://douyin.example/favorites"}
                    if schedule_id.startswith("a-")
                    else {"kind": "folder", "path": str(tmp_path), "recursive": False}
                ),
                now=now,
            ),
        )
    sentinel = "factory-cookie-sentinel"

    def factory(record, credentials):
        if record.source.kind == "douyin":
            raise RuntimeError(f"login cookie verification required: {sentinel}")
        return SimpleNamespace(
            discover=lambda cursor: SimpleNamespace(items=[], cursor_after="safe")
        )

    outcomes = tick_schedules(
        root,
        adapter_factory=factory,
        runtime_credentials=SecretStr(sentinel),
        now=now,
    )

    assert [(item.schedule_id, item.status) for item in outcomes] == [
        ("a-douyin", "blocked"),
        ("b-folder", "completed"),
    ]
    for path in root.rglob("*"):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes()


def test_active_batch_must_match_schedule_kind_owner_and_cursor(tmp_path: Path) -> None:
    from learnnest.batch_models import BatchManifest
    from learnnest.batch_store import write_batch_atomic

    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    schedule = ScheduleRecord(
        schedule_id="folder-owner",
        status="enabled",
        source={"kind": "folder", "path": str(tmp_path), "recursive": False},
        trigger={"kind": "manual"},
        cursor="committed",
        active_batch_id="foreign-batch",
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(root, schedule)
    write_batch_atomic(
        root / "视频学习批次" / "foreign-batch",
        BatchManifest(
            batch_id="foreign-batch",
            kind="scan",
            status="completed",
            created_at=now,
            updated_at=now,
            finished_at=now,
            profile="evidence",
            schedule_id="another-schedule",
            cursor_before="committed",
            cursor_after="foreign",
            results=[],
        ),
    )

    with pytest.raises(RuntimeError, match="another schedule"):
        run_schedule_once(
            root,
            schedule.schedule_id,
            adapter=SimpleNamespace(),
            now=now,
        )

    assert (
        load_schedule(schedule_path(root, schedule.schedule_id)).cursor == "committed"
    )


def test_active_directory_cannot_hide_a_different_internal_batch_id(
    tmp_path: Path,
) -> None:
    from learnnest.batch_models import BatchManifest
    from learnnest.batch_store import write_batch_atomic

    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    schedule = ScheduleRecord(
        schedule_id="folder-batch-identity",
        status="enabled",
        source={"kind": "folder", "path": str(tmp_path), "recursive": False},
        trigger={"kind": "manual"},
        cursor="committed",
        active_batch_id="expected-batch",
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(root, schedule)
    write_batch_atomic(
        root / "视频学习批次" / "expected-batch",
        BatchManifest(
            batch_id="internal-other-id",
            kind="scan",
            status="completed",
            created_at=now,
            updated_at=now,
            finished_at=now,
            profile="evidence",
            schedule_id=schedule.schedule_id,
            cursor_before="committed",
            cursor_after="next",
            results=[],
        ),
    )

    with pytest.raises(RuntimeError, match="identity"):
        run_schedule_once(
            root,
            schedule.schedule_id,
            adapter=SimpleNamespace(),
            now=now,
        )

    persisted = load_schedule(schedule_path(root, schedule.schedule_id))
    assert persisted.active_batch_id == "expected-batch"
    assert persisted.cursor == "committed"


@pytest.mark.parametrize(
    ("kind", "cursor_before", "message"),
    [
        ("scheduled", "committed", "kind"),
        ("scan", "wrong-base", "cursor"),
    ],
)
def test_active_batch_independently_rejects_wrong_kind_or_cursor_base(
    tmp_path: Path,
    kind: str,
    cursor_before: str,
    message: str,
) -> None:
    from learnnest.batch_models import BatchManifest
    from learnnest.batch_store import write_batch_atomic

    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    schedule = ScheduleRecord(
        schedule_id="folder-owner-check",
        status="enabled",
        source={"kind": "folder", "path": str(tmp_path), "recursive": False},
        trigger={"kind": "manual"},
        cursor="committed",
        active_batch_id="owned-batch",
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(root, schedule)
    write_batch_atomic(
        root / "视频学习批次" / "owned-batch",
        BatchManifest(
            batch_id="owned-batch",
            kind=kind,
            status="completed",
            created_at=now,
            updated_at=now,
            finished_at=now,
            profile="evidence",
            schedule_id=schedule.schedule_id,
            cursor_before=cursor_before,
            cursor_after="next",
            results=[],
        ),
    )

    with pytest.raises(RuntimeError, match=message):
        run_schedule_once(
            root,
            schedule.schedule_id,
            adapter=SimpleNamespace(),
            now=now,
        )


def test_missing_active_batch_fact_never_falls_back_to_another_batch(
    tmp_path: Path,
) -> None:
    from learnnest.batch_models import BatchManifest
    from learnnest.batch_store import write_batch_atomic

    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    schedule = ScheduleRecord(
        schedule_id="folder-missing-active",
        status="enabled",
        source={"kind": "folder", "path": str(tmp_path), "recursive": False},
        trigger={"kind": "manual"},
        cursor="committed",
        active_batch_id="missing-batch",
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(root, schedule)
    write_batch_atomic(
        root / "视频学习批次" / "other-unfinished",
        BatchManifest(
            batch_id="other-unfinished",
            kind="scan",
            status="interrupted",
            created_at=now,
            updated_at=now,
            finished_at=now,
            profile="evidence",
            schedule_id=schedule.schedule_id,
            cursor_before="committed",
            cursor_after="next",
            results=[],
        ),
    )

    with pytest.raises(RuntimeError, match="active batch fact is missing"):
        run_schedule_once(
            root,
            schedule.schedule_id,
            adapter=SimpleNamespace(),
            now=now,
        )

    persisted = load_schedule(schedule_path(root, schedule.schedule_id))
    assert persisted.active_batch_id == "missing-batch"
    assert persisted.cursor == "committed"


def test_terminal_active_batch_reconciles_when_wall_clock_moves_backward(
    tmp_path: Path,
) -> None:
    from learnnest.batch_models import BatchManifest
    from learnnest.batch_store import write_batch_atomic

    created = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    schedule = ScheduleRecord(
        schedule_id="folder-clock",
        status="enabled",
        source={"kind": "folder", "path": str(tmp_path), "recursive": False},
        trigger={"kind": "manual"},
        active_batch_id="clock-batch",
        created_at=created,
        updated_at=created,
    )
    write_schedule_atomic(root, schedule)
    write_batch_atomic(
        root / "视频学习批次" / "clock-batch",
        BatchManifest(
            batch_id="clock-batch",
            kind="scan",
            status="completed",
            created_at=created,
            updated_at=created,
            finished_at=created,
            profile="evidence",
            schedule_id=schedule.schedule_id,
            cursor_before=None,
            cursor_after="clock-cursor",
            results=[],
        ),
    )

    outcome = run_schedule_once(
        root,
        schedule.schedule_id,
        adapter=SimpleNamespace(),
        now=created - timedelta(hours=1),
    )

    assert outcome.status == "completed"
    persisted = load_schedule(schedule_path(root, schedule.schedule_id))
    assert persisted.cursor == "clock-cursor"
    assert persisted.active_batch_id is None
    assert persisted.updated_at >= created


def test_orphan_batch_is_claimed_before_resume_so_second_crash_cannot_duplicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.schedules as schedule_module
    from learnnest.batch_store import load_batch

    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    root = tmp_path / "vault"
    video = tmp_path / "orphan.mp4"
    video.write_bytes(b"video")
    source = parse_source(str(video))
    schedule = ScheduleRecord(
        schedule_id="folder-orphan",
        status="enabled",
        source={"kind": "folder", "path": str(tmp_path), "recursive": False},
        trigger={"kind": "manual"},
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(root, schedule)
    with pytest.raises(KeyboardInterrupt):
        run_batch(
            [source],
            root,
            processor=lambda *args: (_ for _ in ()).throw(KeyboardInterrupt),
            kind="scan",
            schedule_id=schedule.schedule_id,
            cursor_before=None,
            cursor_after="orphan-cursor",
            now=now,
        )
    batch_dir = next((root / "视频学习批次").glob("*/batch.json")).parent
    original_commit = schedule_module._commit_manifest
    monkeypatch.setattr(
        schedule_module,
        "_commit_manifest",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        run_schedule_once(
            root,
            schedule.schedule_id,
            adapter=SimpleNamespace(),
            processor=_processor,
            now=now + timedelta(minutes=1),
        )

    claimed = load_schedule(schedule_path(root, schedule.schedule_id))
    assert claimed.active_batch_id == batch_dir.name
    assert load_batch(batch_dir).status == "completed"
    monkeypatch.setattr(schedule_module, "_commit_manifest", original_commit)
    outcome = run_schedule_once(
        root,
        schedule.schedule_id,
        adapter=SimpleNamespace(
            discover=lambda cursor: (_ for _ in ()).throw(
                AssertionError("terminal active batch must reconcile")
            )
        ),
        processor=_processor,
        now=now + timedelta(minutes=2),
    )

    assert outcome.batch_id == batch_dir.name
    assert len(list((root / "视频学习批次").glob("*/batch.json"))) == 1
