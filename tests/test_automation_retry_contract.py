from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.automation_models import (
    AutomationAttempt,
    AutomationBudget,
    AutomationPolicy,
    AutomationTaskState,
)
from learnnest.automation_runner import AutomationProviders, run_automation_tasks
from learnnest.automation_store import (
    authorize,
    disable,
    load_status,
    load_task_state,
    provider_call_usage,
    save_policy,
    save_task_state,
)
from learnnest.index import query_failure_queue, rebuild_index
from learnnest.discovery_models import DiscoveredLink
from learnnest.discovery_store import (
    claim_pending_discoveries,
    load_discovery_manifest,
    mark_discovery_results,
    persist_discovery_links,
)
from learnnest.execution import (
    FailureInfo,
    StageAttemptLimitError,
    begin_attempt,
    ensure_stage_attempts_available,
    finish_attempt,
    stage_attempt_count,
)
from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.pipeline import _record_failure, _run_stage
from learnnest.provider_profiles import update_limits
from learnnest.provider_service import execute_direct_provider_call
from learnnest.queue_runner import run_failure_queue
from learnnest.task_store import load_task, write_task_atomic


BASE_TIME = datetime(2026, 7, 28, tzinfo=UTC)


def _snapshot() -> AssistedConnectionSnapshot:
    return AssistedConnectionSnapshot(
        connection_name="main",
        provider="fake-openai",
        endpoint_identity="https://example.test/v1",
        model="fake-1",
        adapter_revision="1",
    )


def _root(tmp_path: Path, task_id: str = "20260728-retry") -> Path:
    root = tmp_path / "vault"
    task_dir = root / "视频学习素材" / f"task-{task_id}"
    task_dir.mkdir(parents=True)
    pack = ContentPack(
        task_id=task_id,
        source_fingerprint=f"fingerprint-{task_id}",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="重试合同测试。",
                artifact_path="content_pack.json",
            )
        ],
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2), encoding="utf-8"
    )
    write_task_atomic(
        task_dir,
        TaskRecord(
            task_id=task_id,
            source_path="C:/videos/retry.mp4",
            source_fingerprint=pack.source_fingerprint,
            title="重试测试",
            stages={"content_pack": StageStatus.COMPLETED},
            artifacts={"content_pack": ["content_pack.json"]},
        ),
    )
    return root


def _configure(root: Path, *, budget: int = 80, retries: int = 3) -> str:
    update_limits(
        root,
        retries_per_role=retries,
        global_calls_per_day=budget,
        budget_group_calls_per_day={
            "note": 20,
            "podcast": 20,
            "tts": 20,
            "asr": 20,
            "ocr": 20,
        },
    )
    snapshot = _snapshot()
    save_policy(
        root,
        AutomationPolicy(
            schedule_id="douyin-favorites",
            writer=snapshot,
            reviewer=snapshot,
            default_output="complete_note",
            retries_per_stage=retries,
            budget=AutomationBudget(provider_calls_per_day=budget),
        ),
    )
    return authorize(root, now=BASE_TIME).policy_sha256


class _WriterReviewer:
    name = "fake-openai"
    model = "fake-1"
    endpoint_identity = "https://example.test/v1"

    def __init__(self, failures: int = 0, *, timeout: bool = False) -> None:
        self.writer_calls = 0
        self.reviewer_calls = 0
        self.failures = failures
        self.timeout = timeout

    def write_markdown(self, dossier_json: str) -> str:
        del dossier_json
        self.writer_calls += 1
        if self.timeout:
            raise TimeoutError("provider timed out before result confirmation")
        if self.writer_calls <= self.failures:
            raise RuntimeError("HTTP 503 temporary writer failure")
        return "# 重试测试\n\n正文。"

    def review_markdown(self, dossier_json: str, candidate_markdown: str) -> str:
        del dossier_json, candidate_markdown
        self.reviewer_calls += 1
        return "# 重试测试\n\n复核正文。"


class _UnusedProvider:
    name = "unused"
    model = "unused"
    voice = "unused"


def _providers(provider: _WriterReviewer) -> AutomationProviders:
    return AutomationProviders(
        writer=provider,
        reviewer=provider,
        podcast=_UnusedProvider(),
        tts=_UnusedProvider(),
    )


def test_local_retryable_stage_runs_four_times_and_not_a_fifth(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    write_task_atomic(
        task_dir,
        TaskRecord(
            task_id="local-retry",
            source_path="C:/videos/local.mp4",
            source_fingerprint="local-fingerprint",
            title="local",
            stages={"transcript": StageStatus.PENDING},
        ),
    )
    calls = 0
    for ordinal in range(1, 5):
        task = load_task(task_dir)
        started = begin_attempt(
            task,
            reason="initial" if ordinal == 1 else "retry",
            from_stage="transcript",
            now=BASE_TIME + timedelta(minutes=ordinal),
        )
        write_task_atomic(task_dir, started)

        def action() -> None:
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise RuntimeError("HTTP 503 temporary local failure")

        try:
            _run_stage(task_dir, started, "transcript", action)
        except RuntimeError as error:
            _record_failure(task_dir, error, "transcript")
        else:
            completed = load_task(task_dir)
            write_task_atomic(
                task_dir,
                finish_attempt(
                    completed,
                    status="completed",
                    now=BASE_TIME + timedelta(minutes=ordinal),
                ),
            )

    current = load_task(task_dir)
    assert calls == 4
    assert stage_attempt_count(current, "transcript") == 4
    with pytest.raises(StageAttemptLimitError):
        ensure_stage_attempts_available(current, ("transcript",))
    assert calls == 4


def test_non_retryable_failure_is_blocked_by_the_automatic_queue(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.queue_runner as queue_module

    monkeypatch.setattr(
        queue_module,
        "query_failure_queue",
        lambda *args, **kwargs: [
            {"task_id": "manual-task", "failure_disposition": "manual"}
        ],
    )
    calls: list[str] = []
    result = run_failure_queue(
        tmp_path,
        recoverer=lambda task_dir, **kwargs: calls.append(task_dir.name),
    )
    assert result[0].status == "blocked"
    assert calls == []


def _douyin_link(platform_id: str) -> DiscoveredLink:
    return DiscoveredLink(
        platform_id=platform_id,
        source={
            "input": f"https://www.douyin.com/video/{platform_id}",
            "input_type": "url",
            "content_type": "video",
        },
    )


def test_discovery_download_attempts_are_persistent_and_capped(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    persist_discovery_links(root, "schedule", [_douyin_link("1")], now=BASE_TIME)
    failure = FailureInfo(
        code="http_503",
        category="provider",
        disposition="retryable",
        safe_summary="temporary",
    )
    for attempt in range(1, 5):
        claims = claim_pending_discoveries(
            root,
            max_items=1,
            schedule_id="schedule",
            retry_failed=True,
            now=BASE_TIME + timedelta(minutes=attempt),
        )
        assert len(claims) == 1
        mark_discovery_results(
            root,
            [(claims[0], "failed", None, failure)],
            now=BASE_TIME + timedelta(minutes=attempt),
        )
    assert (
        claim_pending_discoveries(
            root,
            max_items=1,
            schedule_id="schedule",
            retry_failed=True,
            now=BASE_TIME + timedelta(hours=1),
        )
        == []
    )
    manifest = load_discovery_manifest(root, "schedule")
    assert manifest is not None
    assert manifest.records[0].attempt_count == 4


def test_discovery_retry_setting_zero_stops_after_the_initial_attempt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    persist_discovery_links(root, "schedule", [_douyin_link("zero")], now=BASE_TIME)
    claim = claim_pending_discoveries(
        root,
        max_items=1,
        max_attempts=1,
        schedule_id="schedule",
        now=BASE_TIME,
    )[0]
    mark_discovery_results(
        root,
        [
            (
                claim,
                "failed",
                None,
                FailureInfo(
                    code="http_503",
                    category="provider",
                    disposition="retryable",
                    safe_summary="temporary",
                ),
            )
        ],
        now=BASE_TIME,
    )

    assert (
        claim_pending_discoveries(
            root,
            max_items=1,
            max_attempts=1,
            retry_failed=True,
            schedule_id="schedule",
            now=BASE_TIME + timedelta(minutes=1),
        )
        == []
    )


def test_discovery_non_retryable_failure_is_not_reclaimed(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    persist_discovery_links(root, "schedule", [_douyin_link("2")], now=BASE_TIME)
    claim = claim_pending_discoveries(
        root, max_items=1, schedule_id="schedule", now=BASE_TIME
    )[0]
    mark_discovery_results(
        root,
        [
            (
                claim,
                "failed",
                None,
                FailureInfo(
                    code="http_404",
                    category="provider",
                    disposition="terminal",
                    safe_summary="not found",
                ),
            )
        ],
        now=BASE_TIME,
    )
    assert (
        claim_pending_discoveries(
            root,
            max_items=1,
            schedule_id="schedule",
            retry_failed=True,
            now=BASE_TIME + timedelta(minutes=1),
        )
        == []
    )


def test_disable_and_reauthorize_preserve_policy_identity(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    sha = _configure(root)
    assert disable(root).policy_sha256 == sha
    reauthorized = authorize(root, now=BASE_TIME + timedelta(hours=1))
    assert reauthorized.policy_sha256 == sha


def test_legacy_policy_and_task_state_are_migrated_without_losing_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    status_path = root / ".learnnest" / "automation" / "status.json"
    status_path.parent.mkdir(parents=True)
    old_sha = "a" * 64
    snapshot = _snapshot().model_dump(mode="json")
    status_path.write_text(
        json.dumps(
            {
                "policy": {
                    "schema_version": "1.0",
                    "enabled": False,
                    "authorized_at": None,
                    "schedule_id": "schedule",
                    "writer": snapshot,
                    "reviewer": snapshot,
                    "dossier_schema_version": "1.1",
                    "max_items_per_tick": 1,
                    "paid_retry_limit": 1,
                    "budget": {
                        "writer_per_day": 20,
                        "reviewer_per_day": 20,
                        "podcast_per_day": 20,
                        "tts_per_day": 20,
                    },
                },
                "policy_sha256": old_sha,
            }
        ),
        encoding="utf-8",
    )
    save_task_state(
        root,
        AutomationTaskState(
            task_id="old-task",
            policy_sha256=old_sha,
            attempts=[
                AutomationAttempt(
                    stage="writer",
                    attempt=1,
                    status="completed",
                    started_at=BASE_TIME,
                )
            ],
        ),
    )
    status = load_status(root)
    assert status is not None
    assert status.policy.schema_version == "1.2"
    assert status.policy.retries_per_stage == 1
    assert status.policy.budget.provider_calls_per_day == 20
    migrated = load_task_state(root, "old-task", status.policy_sha256)
    assert migrated is not None
    assert migrated.policy_sha256 == status.policy_sha256
    assert provider_call_usage(
        root,
        status.policy_sha256,
        now=BASE_TIME,
        limit=20,
    ) == (1, 20, 19)


def test_daily_provider_usage_is_shared_across_two_task_facts(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    policy_sha = _configure(root)
    for task_index, count in (("a", 40), ("b", 39)):
        attempts = [
            AutomationAttempt(
                stage="writer" if index % 2 == 0 else "tts",
                attempt=index % 4 + 1,
                status="completed",
                started_at=BASE_TIME,
            )
            for index in range(count)
        ]
        save_task_state(
            root,
            AutomationTaskState(
                task_id=f"history-{task_index}",
                policy_sha256=policy_sha,
                attempts=attempts,
            ),
        )
    assert provider_call_usage(root, policy_sha, now=BASE_TIME, limit=80) == (
        79,
        80,
        1,
    )
    state = load_task_state(root, "history-b", policy_sha)
    assert state is not None
    state.attempts.append(
        AutomationAttempt(
            stage="writer",
            attempt=1,
            status="unknown",
            started_at=BASE_TIME,
        )
    )
    save_task_state(root, state)
    assert provider_call_usage(root, policy_sha, now=BASE_TIME, limit=80) == (
        80,
        80,
        0,
    )


def test_daily_provider_usage_survives_policy_identity_changes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    old_sha = _configure(root)
    save_task_state(
        root,
        AutomationTaskState(
            task_id="old-policy-task",
            policy_sha256=old_sha,
            attempts=[
                AutomationAttempt(
                    call_id=f"old-call-{index}",
                    stage="writer",
                    attempt=index % 4 + 1,
                    status="completed",
                    started_at=BASE_TIME,
                )
                for index in range(80)
            ],
        ),
    )
    status = load_status(root)
    assert status is not None
    changed = save_policy(
        root,
        status.policy.model_copy(
            update={
                "enabled": False,
                "authorized_at": None,
                "max_items_per_tick": 2,
            }
        ),
    )

    assert changed.policy_sha256 != old_sha
    assert provider_call_usage(
        root,
        changed.policy_sha256,
        now=BASE_TIME,
        limit=80,
    ) == (80, 80, 0)
    authorize(root, now=BASE_TIME)
    provider = _WriterReviewer()
    run_automation_tasks(
        root,
        ["20260728-retry"],
        _providers(provider),
        now=BASE_TIME,
    )
    assert provider.writer_calls == 0


def test_local_failure_queue_counts_opportunities_per_stage(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    task_dir = root / "视频学习素材" / "task--cross-stage"
    task = TaskRecord(
        task_id="cross-stage",
        source_path="C:/videos/cross-stage.mp4",
        source_fingerprint="cross-stage",
        title="cross-stage",
        stages={"ocr": StageStatus.FAILED},
    )
    failure = FailureInfo(
        code="http_503",
        category="provider",
        disposition="retryable",
        safe_summary="temporary",
    )
    for ordinal, stage in enumerate(
        ("transcript", "transcript", "transcript", "ocr"),
        start=1,
    ):
        task = begin_attempt(
            task,
            reason="initial" if ordinal == 1 else "retry",
            from_stage=stage,
            now=BASE_TIME,
        )
        task = finish_attempt(
            task,
            status="failed",
            now=BASE_TIME,
            failed_stage=stage,
            failure=failure,
            next_retry_at=BASE_TIME,
        )
    write_task_atomic(task_dir, task)
    rebuild_index(root)

    assert [
        row["failed_stage"] for row in query_failure_queue(root, now=BASE_TIME)
    ] == ["ocr"]
    assert (
        query_failure_queue(
            root,
            now=BASE_TIME,
            max_stage_attempts=1,
        )
        == []
    )


def test_paid_retryable_stage_uses_four_due_opportunities(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _configure(root)
    provider = _WriterReviewer(failures=3)
    results = []
    for minutes in (0, 1, 6, 36):
        results.append(
            run_automation_tasks(
                root,
                ["20260728-retry"],
                _providers(provider),
                now=BASE_TIME + timedelta(minutes=minutes),
            )
        )
    status = load_status(root)
    assert status is not None
    state = load_task_state(root, "20260728-retry", status.policy_sha256)
    assert state is not None
    assert provider.writer_calls == 4
    assert [item.status for item in state.attempts if item.stage == "writer"] == [
        "failed",
        "failed",
        "failed",
        "completed",
    ]
    assert results[-1].completed_task_ids == ("20260728-retry",)
    run_automation_tasks(
        root,
        ["20260728-retry"],
        _providers(provider),
        now=BASE_TIME + timedelta(minutes=37),
    )
    assert provider.writer_calls == 4


def test_paid_timeout_becomes_unknown_and_is_never_retried(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _configure(root)
    provider = _WriterReviewer(timeout=True)
    first = run_automation_tasks(
        root, ["20260728-retry"], _providers(provider), now=BASE_TIME
    )
    second = run_automation_tasks(
        root,
        ["20260728-retry"],
        _providers(provider),
        now=BASE_TIME + timedelta(hours=1),
    )
    status = load_status(root)
    assert status is not None
    state = load_task_state(root, "20260728-retry", status.policy_sha256)
    assert first.failed_task_ids == second.failed_task_ids == ("20260728-retry",)
    assert provider.writer_calls == 1
    assert state is not None
    assert state.status == "needs_attention"
    assert [item.status for item in state.attempts if item.stage == "writer"] == [
        "unknown"
    ]


def test_exhausted_provider_budget_leaves_task_pending_without_a_call(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _configure(root, budget=0)
    provider = _WriterReviewer()
    result = run_automation_tasks(
        root, ["20260728-retry"], _providers(provider), now=BASE_TIME
    )
    status = load_status(root)
    assert status is not None
    state = load_task_state(root, "20260728-retry", status.policy_sha256)
    assert result.failed_task_ids == ("20260728-retry",)
    assert provider.writer_calls == 0
    assert state is not None
    assert state.attempts == []
    assert state.blocked_reason == "provider_budget_exhausted"


def test_manual_call_consumes_the_same_frozen_cap_as_automation(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _configure(root, budget=1, retries=0)
    manual_calls = 0

    def manual_writer() -> None:
        nonlocal manual_calls
        manual_calls += 1

    execute_direct_provider_call(
        root, "manual-source", "writer", manual_writer, now=BASE_TIME
    )
    provider = _WriterReviewer()
    result = run_automation_tasks(
        root, ["20260728-retry"], _providers(provider), now=BASE_TIME
    )

    assert manual_calls == 1
    assert result.failed_task_ids == ("20260728-retry",)
    assert provider.writer_calls == 0


def test_reauthorization_freezes_webui_retry_zero_before_the_first_fake_failure(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    _configure(root, retries=1)
    update_limits(
        root,
        retries_per_role=0,
        global_calls_per_day=80,
        budget_group_calls_per_day={
            "note": 20,
            "podcast": 20,
            "tts": 20,
            "asr": 20,
            "ocr": 20,
        },
    )
    authorize(root, now=BASE_TIME)
    provider = _WriterReviewer(failures=1)

    first = run_automation_tasks(
        root, ["20260728-retry"], _providers(provider), now=BASE_TIME
    )
    second = run_automation_tasks(
        root,
        ["20260728-retry"],
        _providers(provider),
        now=BASE_TIME + timedelta(hours=1),
    )

    assert first.failed_task_ids == second.failed_task_ids == ("20260728-retry",)
    assert provider.writer_calls == 1


def test_local_tts_attempt_is_observable_but_not_a_paid_usage_fact(
    tmp_path: Path,
) -> None:
    policy_sha = "a" * 64
    state = AutomationTaskState(
        task_id="local-tts",
        policy_sha256=policy_sha,
        attempts=[
            AutomationAttempt(
                stage="tts",
                billing="local",
                attempt=1,
                status="completed",
                started_at=BASE_TIME,
                completed_at=BASE_TIME,
            )
        ],
    )
    save_task_state(tmp_path, state)

    used, limit, remaining = provider_call_usage(
        tmp_path, policy_sha, now=BASE_TIME, limit=1
    )

    assert used == 0
    assert limit == 1
    assert remaining == 1
