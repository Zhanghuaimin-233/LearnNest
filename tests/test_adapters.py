from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from learnnest.adapters.base import ScanDiscovery
from learnnest.batch_store import load_batch
from learnnest.execution_models import SourceIdentities
from learnnest.index import rebuild_index
from learnnest.models import StageStatus, TaskRecord
from learnnest.schedule_models import ScheduleRecord
from learnnest.schedule_store import write_schedule_atomic
from learnnest.schedules import run_schedule_once
from learnnest.source_models import SourceItem
from learnnest.task_store import write_task_atomic


def test_fake_douyin_item_auth_failure_is_isolated_and_cookie_stays_runtime_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    schedule = ScheduleRecord(
        schedule_id="douyin-fake",
        status="enabled",
        source={"kind": "douyin", "url": "https://douyin.example/favorites"},
        trigger={"kind": "manual"},
        profile="evidence",
        created_at=now,
        updated_at=now,
    )
    write_schedule_atomic(root, schedule)
    secret_value = "douyin-cookie-sentinel-9c4a"
    credentials = SecretStr(secret_value)

    class FakeDouyinAdapter:
        def discover(self, cursor):
            assert cursor is None
            assert credentials.get_secret_value() == secret_value
            return ScanDiscovery(
                items=[
                    SourceItem(
                        input=f"https://douyin.example/video/{index}",
                        input_type="url",
                    )
                    for index in range(1, 4)
                ],
                cursor_after="fake-douyin-page-1",
            )

    def processor(source, output_root, profile):
        if source.input.endswith("/2"):
            raise RuntimeError(f"login cookie verification required: {secret_value}")
        task = TaskRecord(
            task_id=f"20260712-douyin0{source.input[-1]}",
            source_path=source.input,
            source_type="url",
            source_fingerprint=f"douyin-{source.input[-1]}",
            title=f"douyin {source.input[-1]}",
            profile=profile,
            identities=SourceIdentities(normalized_source=source.input),
            stages={"content_pack": StageStatus.COMPLETED},
        )
        write_task_atomic(
            output_root / "视频学习素材" / f"douyin--{task.task_id[-8:]}",
            task,
        )
        return task

    outcome = run_schedule_once(
        root,
        schedule.schedule_id,
        adapter=FakeDouyinAdapter(),
        processor=processor,
        runtime_credentials=credentials,
        now=now,
    )

    assert outcome.status == "partial"
    manifest = load_batch(root / "视频学习批次" / str(outcome.batch_id))
    assert [item.status for item in manifest.results] == [
        "completed",
        "failed",
        "completed",
    ]
    assert manifest.results[1].failure is not None
    assert manifest.results[1].failure.disposition == "manual"
    assert secret_value not in str(manifest.results[1].error)
    rebuild_index(root)
    for path in root.rglob("*"):
        if path.is_file():
            assert secret_value.encode() not in path.read_bytes()


def test_adapter_cursor_or_source_cannot_contain_runtime_cookie(
    tmp_path: Path,
) -> None:
    for leak_kind in ("cursor", "source"):
        root = tmp_path / leak_kind
        now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
        schedule = ScheduleRecord(
            schedule_id=f"douyin-{leak_kind}",
            status="enabled",
            source={"kind": "douyin", "url": "https://douyin.example/favorites"},
            trigger={"kind": "manual"},
            profile="evidence",
            created_at=now,
            updated_at=now,
        )
        write_schedule_atomic(root, schedule)
        sentinel = f"runtime-cookie-{leak_kind}-sentinel"

        class LeakingAdapter:
            def discover(self, cursor):
                del cursor
                return ScanDiscovery(
                    items=(
                        [
                            SourceItem(
                                input=f"https://douyin.example/video?cookie={sentinel}",
                                input_type="url",
                            )
                        ]
                        if leak_kind == "source"
                        else []
                    ),
                    cursor_after=sentinel if leak_kind == "cursor" else "safe-cursor",
                )

        outcome = run_schedule_once(
            root,
            schedule.schedule_id,
            adapter=LeakingAdapter(),
            runtime_credentials=SecretStr(sentinel),
            now=now,
        )

        assert outcome.status == "blocked"
        assert sentinel not in repr(outcome)
        assert list((root / "视频学习批次").glob("*/batch.json")) == []
        for path in root.rglob("*"):
            if path.is_file():
                assert sentinel.encode() not in path.read_bytes()
