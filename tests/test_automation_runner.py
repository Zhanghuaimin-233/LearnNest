from datetime import UTC, datetime
from pathlib import Path

from learnnest.automation_models import AutomationBudget, AutomationPolicy
from learnnest.automation_runner import AutomationProviders, run_automation_tasks
from learnnest.automation_store import (
    authorize,
    load_task_state,
    load_status,
    save_policy,
)
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.task_store import write_task_atomic


class FakeAssistedProvider:
    name = "fake-openai"
    model = "fake-1"
    endpoint_identity = "https://example.test/v1"

    def __init__(self, *, fail_first_writer: bool = False) -> None:
        self.writer_calls = 0
        self.reviewer_calls = 0
        self.fail_first_writer = fail_first_writer

    def write_markdown(self, dossier_json: str) -> str:
        del dossier_json
        self.writer_calls += 1
        if self.fail_first_writer and self.writer_calls == 1:
            raise RuntimeError("temporary writer failure")
        return "# 自动笔记\n\n只使用材料包内容。"

    def review_markdown(self, dossier_json: str, candidate_markdown: str) -> str:
        del dossier_json, candidate_markdown
        self.reviewer_calls += 1
        return "# 自动笔记\n\n已由模型复核。"


class UnusedProvider:
    name = "fake"
    model = "fake"
    voice = "fake"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    task_dir = root / "视频学习素材" / "automation-task"
    task_dir.mkdir(parents=True)
    pack = ContentPack(
        task_id="20260728-automation",
        source_fingerprint="automation-fingerprint",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="自动化测试内容。",
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
            task_id=pack.task_id,
            source_path="C:/videos/automation.mp4",
            source_fingerprint=pack.source_fingerprint,
            title="自动化测试",
            stages={"content_pack": StageStatus.COMPLETED},
            artifacts={"content_pack": ["content_pack.json"]},
        ),
    )
    return root


def _snapshot() -> AssistedConnectionSnapshot:
    return AssistedConnectionSnapshot(
        connection_name="main",
        provider="fake-openai",
        endpoint_identity="https://example.test/v1",
        model="fake-1",
        adapter_revision="1",
    )


def test_authorized_runner_retries_writer_once_and_preserves_attempt_facts(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.automation_runner as runner

    root = _root(tmp_path)
    snapshot = _snapshot()
    save_policy(
        root,
        AutomationPolicy(
            schedule_id="douyin-favorites",
            writer=snapshot,
            reviewer=snapshot,
            paid_retry_limit=1,
            budget=AutomationBudget(
                writer_per_day=2, reviewer_per_day=1, podcast_per_day=1, tts_per_day=1
            ),
        ),
    )
    authorize(root, now=datetime(2026, 7, 28, tzinfo=UTC))
    writer = FakeAssistedProvider(fail_first_writer=True)
    reviewer = FakeAssistedProvider()

    monkeypatch.setattr(
        runner,
        "generate_model_reviewed_podcast",
        lambda source, provider, *, delivery_dir: object(),
    )
    monkeypatch.setattr(
        runner,
        "generate_model_reviewed_tts",
        lambda source, podcast, provider, *, output_root, delivery_dir, style_instruction: (
            root / "audio.mp3"
        ),
    )

    result = run_automation_tasks(
        root,
        ["20260728-automation"],
        AutomationProviders(
            writer=writer,
            reviewer=reviewer,
            podcast=UnusedProvider(),
            tts=UnusedProvider(),
        ),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    status = load_status(root)
    assert status is not None
    state = load_task_state(root, "20260728-automation", status.policy_sha256)
    assert result.completed_task_ids == ("20260728-automation",)
    assert writer.writer_calls == 2
    assert reviewer.reviewer_calls == 1
    assert state is not None
    assert state.status == "completed"
    assert [(item.stage, item.attempt, item.status) for item in state.attempts] == [
        ("writer", 1, "failed"),
        ("writer", 2, "completed"),
        ("reviewer", 1, "completed"),
        ("podcast", 1, "completed"),
        ("tts", 1, "completed"),
    ]


def test_runner_rejects_disabled_policy_before_provider_calls(tmp_path: Path) -> None:
    root = _root(tmp_path)
    snapshot = _snapshot()
    save_policy(
        root, AutomationPolicy(schedule_id="douyin", writer=snapshot, reviewer=snapshot)
    )
    provider = FakeAssistedProvider()

    try:
        run_automation_tasks(
            root,
            ["20260728-automation"],
            AutomationProviders(provider, provider, UnusedProvider(), UnusedProvider()),
        )
    except ValueError as error:
        assert "not authorized" in str(error)
    else:
        raise AssertionError("disabled policy must not invoke a provider")
    assert provider.writer_calls == provider.reviewer_calls == 0
