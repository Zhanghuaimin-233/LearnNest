from datetime import UTC, datetime
import json
import wave
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import pytest

from learnnest.automation_models import AutomationBudget, AutomationPolicy
from learnnest.automation_runner import AutomationProviders, run_automation_tasks
from learnnest.automation_store import (
    authorize,
    load_task_state,
    load_status,
    save_policy,
)
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.models import (
    ContentPack,
    Evidence,
    ProviderBindingSnapshot,
    StageStatus,
    TaskRecord,
)
from learnnest.provider_profiles import load_settings, settings_sha256
from learnnest.standard_note_publication import (
    load_active_standard_note,
    write_standard_note_bundle,
)
from learnnest.task_store import load_task, write_task_atomic
from learnnest.validation import validate_task


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


class FakePodcastProvider:
    name = "fake-podcast"
    model = "fake-podcast-1"
    endpoint_identity = "https://example.test/podcast"

    def generate(self, context: str, _images: tuple[object, ...]) -> str:
        payload = json.loads(context)
        pack = payload["content_pack"]
        return json.dumps(
            {
                "schema_version": "1.0",
                "task_id": pack["task_id"],
                "source_fingerprint": pack["source_fingerprint"],
                "note_content_sha256": payload["note_content_sha256"],
                "title": "自动化音频",
                "segments": [
                    {
                        "order": 1,
                        "kind": "intro",
                        "text": "欢迎收听自动化音频",
                        "evidence_ids": ["tr_0001"],
                    },
                    {
                        "order": 2,
                        "kind": "body",
                        "text": "这里是已经验证的材料内容",
                        "evidence_ids": ["tr_0001"],
                    },
                    {
                        "order": 3,
                        "kind": "outro",
                        "text": "感谢收听",
                        "evidence_ids": ["tr_0001"],
                    },
                ],
                "ai_supplements": [],
            },
            ensure_ascii=False,
        )


class FakeLocalTtsProvider:
    name = "fake-local-tts"
    model = "fake-local-tts-1"
    voice = "Test"
    billing = "local"

    def synthesize(self, _speech_text: str, _style_instruction: str) -> bytes:
        stream = BytesIO()
        with wave.open(stream, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\0\0" * 1_600)
        return stream.getvalue()


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


def _binding(
    name: str, settings_sha: str, *, capability: str = "llm"
) -> ProviderBindingSnapshot:
    return ProviderBindingSnapshot(
        capability=capability,  # type: ignore[arg-type]
        connection_id=name,
        provider="fake-openai" if capability == "llm" else "windows-tts",
        endpoint="https://example.test/v1" if capability == "llm" else "voice=Test",
        model="fake-1" if capability == "llm" else "system-speech",
        adapter_revision="1",
        settings_sha256=settings_sha,
    )


def _snapshot_named(name: str, settings_sha: str) -> AssistedConnectionSnapshot:
    return AssistedConnectionSnapshot(
        connection_name=name,
        connection_id=name,
        provider="fake-openai",
        endpoint_identity="https://example.test/v1",
        model="fake-1",
        adapter_revision="1",
        settings_sha256=settings_sha,
    )


def _write_frozen_task(
    root: Path, bindings: dict[str, ProviderBindingSnapshot]
) -> None:
    task_dir = root / "视频学习素材" / "automation-task"
    task = TaskRecord(
        task_id="20260728-automation",
        source_path="C:/videos/automation.mp4",
        source_fingerprint="automation-fingerprint",
        title="自动化测试",
        stages={"content_pack": StageStatus.COMPLETED},
        artifacts={"content_pack": ["content_pack.json"]},
        provider_bindings=bindings,
    )
    write_task_atomic(task_dir, task)


def _assert_pre_provider_attention(
    root: Path, policy: AutomationPolicy, factory_calls: list[str]
) -> None:
    result = run_automation_tasks(
        root,
        ["20260728-automation"],
        provider_factory=lambda task_id: factory_calls.append(task_id),  # type: ignore[arg-type]
    )
    status = load_status(root)
    assert status is not None
    state = load_task_state(root, "20260728-automation", status.policy_sha256)
    assert result.failed_task_ids == ("20260728-automation",)
    assert factory_calls == []
    assert state is not None
    assert state.status == "needs_attention"
    assert state.blocked_reason == "non_retryable_failure"
    assert state.default_output == policy.default_output
    summaries = " ".join(attempt.safe_summary or "" for attempt in state.attempts)
    assert "C:/videos" not in summaries
    assert "fake-secret" not in summaries
    assert "connection-a" not in summaries
    assert "connection-b" not in summaries


def test_authorized_runner_retries_writer_on_the_next_due_tick_and_preserves_attempt_facts(
    monkeypatch, tmp_path: Path
) -> None:
    root = _root(tmp_path)
    snapshot = _snapshot()
    save_policy(
        root,
        AutomationPolicy(
            schedule_id="douyin-favorites",
            writer=snapshot,
            reviewer=snapshot,
            default_output="complete_note",
            paid_retry_limit=1,
            budget=AutomationBudget(
                writer_per_day=2, reviewer_per_day=1, podcast_per_day=1, tts_per_day=1
            ),
        ),
    )
    authorize(root, now=datetime(2026, 7, 28, tzinfo=UTC))
    writer = FakeAssistedProvider(fail_first_writer=True)
    reviewer = FakeAssistedProvider()

    first = run_automation_tasks(
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
    result = run_automation_tasks(
        root,
        ["20260728-automation"],
        AutomationProviders(
            writer=writer,
            reviewer=reviewer,
            podcast=UnusedProvider(),
            tts=UnusedProvider(),
        ),
        now=datetime(2026, 7, 28, 0, 1, tzinfo=UTC),
    )

    status = load_status(root)
    assert status is not None
    state = load_task_state(root, "20260728-automation", status.policy_sha256)
    assert first.failed_task_ids == ("20260728-automation",)
    assert result.completed_task_ids == ("20260728-automation",)
    assert writer.writer_calls == 2
    assert reviewer.reviewer_calls == 1
    assert state is not None
    assert state.status == "completed"
    assert [(item.stage, item.attempt, item.status) for item in state.attempts] == [
        ("writer", 1, "failed"),
        ("writer", 2, "completed"),
        ("reviewer", 1, "completed"),
    ]


def test_audio_success_activates_validated_delivery_in_task_record(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.automation_delivery as delivery
    import learnnest.validation as validation

    root = _root(tmp_path)
    snapshot = _snapshot()
    save_policy(
        root,
        AutomationPolicy(
            schedule_id="audio",
            writer=snapshot,
            reviewer=snapshot,
            default_output="complete_note_with_audio",
        ),
    )
    authorize(root, now=datetime(2026, 7, 28, tzinfo=UTC))
    monkeypatch.setattr(
        delivery,
        "probe_audio",
        lambda _path: {
            "streams": [{"codec_type": "audio", "codec_name": "mp3"}],
            "format": {"duration": "1.0"},
        },
    )
    monkeypatch.setattr(
        validation,
        "probe_audio",
        lambda _path: {
            "streams": [{"codec_type": "audio", "codec_name": "mp3"}],
            "format": {"duration": "1.0"},
        },
    )
    monkeypatch.setattr(
        delivery,
        "convert_wav_to_mp3",
        lambda _source, destination: destination.write_bytes(b"fake-mp3"),
    )
    writer = FakeAssistedProvider()
    result = run_automation_tasks(
        root,
        ["20260728-automation"],
        AutomationProviders(
            writer=writer,
            reviewer=writer,
            podcast=FakePodcastProvider(),
            tts=FakeLocalTtsProvider(),
        ),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    task_dir = root / "视频学习素材" / "automation-task"
    task = load_task(task_dir)
    status = load_status(root)
    assert status is not None
    state = load_task_state(root, "20260728-automation", status.policy_sha256)
    assert result.completed_task_ids == ("20260728-automation",)
    assert state is not None and state.status == "completed", state
    assert task.stages["podcast_script"] is StageStatus.COMPLETED
    assert task.stages["tts"] is StageStatus.COMPLETED
    assert {Path(item).name for item in task.artifacts["podcast_script"]} == {
        "generation.json",
        "podcast_script.json",
        "speech.txt",
    }
    assert {Path(item).name for item in task.artifacts["tts"]} == {
        "audio.json",
        "audio.wav",
    }
    validation_errors = validate_task(task_dir)
    assert validation_errors == [], validation_errors


def test_audio_activation_rejects_a_non_podcast_artifact_result(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.automation_runner as automation_runner

    root = _root(tmp_path)
    snapshot = _snapshot()
    save_policy(
        root,
        AutomationPolicy(
            schedule_id="audio",
            writer=snapshot,
            reviewer=snapshot,
            default_output="complete_note_with_audio",
        ),
    )
    authorize(root, now=datetime(2026, 7, 28, tzinfo=UTC))
    monkeypatch.setattr(
        automation_runner,
        "generate_model_reviewed_podcast",
        lambda _source, _provider, *, delivery_dir: object(),
    )

    result = run_automation_tasks(
        root,
        ["20260728-automation"],
        AutomationProviders(
            writer=FakeAssistedProvider(),
            reviewer=FakeAssistedProvider(),
            podcast=FakePodcastProvider(),
            tts=FakeLocalTtsProvider(),
        ),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    task_dir = root / "视频学习素材" / "automation-task"
    task = load_task(task_dir)
    status = load_status(root)
    assert status is not None
    state = load_task_state(root, "20260728-automation", status.policy_sha256)
    assert result.failed_task_ids == ("20260728-automation",)
    assert state is not None
    assert state.status == "needs_attention"
    assert state.blocked_reason == "non_retryable_failure"
    assert [attempt.stage for attempt in state.attempts] == [
        "writer",
        "reviewer",
        "podcast",
    ]
    assert task.stages.get("podcast_script") is not StageStatus.COMPLETED
    assert task.stages.get("tts") is not StageStatus.COMPLETED


def test_audio_activation_rejects_a_standard_note_replaced_while_waiting_for_lock(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.automation_delivery as delivery
    import learnnest.automation_runner as automation_runner

    root = _root(tmp_path)
    snapshot = _snapshot()
    save_policy(
        root,
        AutomationPolicy(
            schedule_id="audio",
            writer=snapshot,
            reviewer=snapshot,
            default_output="complete_note_with_audio",
        ),
    )
    authorize(root, now=datetime(2026, 7, 28, tzinfo=UTC))
    monkeypatch.setattr(
        delivery,
        "probe_audio",
        lambda _path: {
            "streams": [{"codec_type": "audio", "codec_name": "mp3"}],
            "format": {"duration": "1.0"},
        },
    )
    monkeypatch.setattr(
        delivery,
        "convert_wav_to_mp3",
        lambda _source, destination: destination.write_bytes(b"fake-mp3"),
    )
    real_task_lock = automation_runner.task_lock

    @contextmanager
    def replace_active_note_at_lock(output_root: Path, task_id: str, *, timeout: float):
        with real_task_lock(output_root, task_id, timeout=timeout):
            task_dir = root / "视频学习素材" / "automation-task"
            task = load_task(task_dir)
            active = load_active_standard_note(task_dir)
            write_standard_note_bundle(
                task_dir,
                active.bundle_dir,
                task,
                "# 并发更新的笔记\n",
                route="assisted_draft",
                status="model_reviewed",
            )
            yield

    monkeypatch.setattr(automation_runner, "task_lock", replace_active_note_at_lock)
    result = run_automation_tasks(
        root,
        ["20260728-automation"],
        AutomationProviders(
            writer=FakeAssistedProvider(),
            reviewer=FakeAssistedProvider(),
            podcast=FakePodcastProvider(),
            tts=FakeLocalTtsProvider(),
        ),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    task_dir = root / "视频学习素材" / "automation-task"
    task = load_task(task_dir)
    status = load_status(root)
    assert status is not None
    state = load_task_state(root, "20260728-automation", status.policy_sha256)
    assert result.failed_task_ids == ("20260728-automation",)
    assert state is not None and state.status == "needs_attention"
    assert state.blocked_reason == "non_retryable_failure"
    assert task.stages.get("podcast_script") is not StageStatus.COMPLETED
    assert task.stages.get("tts") is not StageStatus.COMPLETED
    assert not list(task_dir.glob("automated-delivery/*/task-record-activation"))


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


def test_runner_rejects_same_sha_but_different_frozen_note_connection_before_factory(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    current_sha = settings_sha256(load_settings(root))
    policy = AutomationPolicy(
        writer=_snapshot_named("connection-a", current_sha),
        reviewer=_snapshot_named("connection-a", current_sha),
        default_output="complete_note",
    )
    save_policy(root, policy)
    authorized = authorize(root, now=datetime(2026, 8, 7, tzinfo=UTC)).policy
    _write_frozen_task(
        root,
        {
            "note_writer": _binding("connection-b", current_sha),
            "note_reviewer": _binding("connection-b", current_sha),
        },
    )
    factory_calls: list[str] = []

    _assert_pre_provider_attention(root, authorized, factory_calls)


def test_runner_rejects_old_frozen_settings_sha_before_factory(tmp_path: Path) -> None:
    root = _root(tmp_path)
    current_sha = settings_sha256(load_settings(root))
    old_sha = "a" * 64
    policy = AutomationPolicy(
        writer=_snapshot_named("connection-a", current_sha),
        reviewer=_snapshot_named("connection-a", current_sha),
        default_output="complete_note",
    )
    save_policy(root, policy)
    authorized = authorize(root, now=datetime(2026, 8, 7, tzinfo=UTC)).policy
    _write_frozen_task(
        root,
        {
            "note_writer": _binding("connection-a", old_sha),
            "note_reviewer": _binding("connection-a", old_sha),
        },
    )
    factory_calls: list[str] = []

    _assert_pre_provider_attention(root, authorized, factory_calls)


def test_runner_rejects_audio_policy_without_frozen_audio_bindings_before_factory(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    current_sha = settings_sha256(load_settings(root))
    policy = AutomationPolicy(
        writer=_snapshot_named("connection-a", current_sha),
        reviewer=_snapshot_named("connection-a", current_sha),
        default_output="complete_note_with_audio",
    )
    save_policy(root, policy)
    authorized = authorize(root, now=datetime(2026, 8, 7, tzinfo=UTC)).policy
    _write_frozen_task(
        root,
        {
            "note_writer": _binding("connection-a", current_sha),
            "note_reviewer": _binding("connection-a", current_sha),
        },
    )
    factory_calls: list[str] = []

    _assert_pre_provider_attention(root, authorized, factory_calls)


def test_automation_tts_recovers_persisted_audio_without_a_second_provider_call(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.automation_delivery as delivery
    from learnnest.automation_delivery import PodcastArtifact, ReviewedMarkdownSource

    pack = ContentPack(
        task_id="20260805-automation-audio",
        source_fingerprint="audio-fingerprint",
        evidence=[],
    )
    source = ReviewedMarkdownSource(
        task_id=pack.task_id,
        task_dir=tmp_path / "task",
        content_pack=pack,
        content_pack_sha256="pack",
        markdown="# reviewed\n",
        markdown_sha256="markdown",
        plan_id="plan",
        dossier_sha256="dossier",
    )
    podcast = PodcastArtifact(
        directory=tmp_path / "podcast",
        script=None,  # type: ignore[arg-type]
        speech="同一份已验证的 speech.txt。",
        script_sha256="script",
        speech_sha256="speech",
    )

    class Provider:
        name = "fake"
        model = "fake"
        voice = "fake"
        calls = 0

        def synthesize(self, speech: str, style: str) -> bytes:
            assert speech == podcast.speech
            self.calls += 1
            return b"wav"

    provider = Provider()
    monkeypatch.setattr(delivery, "validate_wav_bytes", lambda content: None)
    monkeypatch.setattr(delivery, "probe_audio", lambda path: {"ok": True})
    monkeypatch.setattr(
        delivery,
        "convert_wav_to_mp3",
        lambda source, destination: destination.write_bytes(b"mp3"),
    )
    original_publish = delivery._publish_audio
    monkeypatch.setattr(
        delivery,
        "_publish_audio",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("interrupted publish")),
    )

    with pytest.raises(OSError, match="interrupted publish"):
        delivery.generate_model_reviewed_tts(
            source,
            podcast,
            provider,
            output_root=tmp_path,
            delivery_dir=tmp_path / "delivery",
            style_instruction="自然",
        )

    monkeypatch.setattr(delivery, "_publish_audio", original_publish)
    recovered = delivery.generate_model_reviewed_tts(
        source,
        podcast,
        provider,
        output_root=tmp_path,
        delivery_dir=tmp_path / "delivery",
        style_instruction="自然",
    )

    assert provider.calls == 1
    assert recovered.is_file()
    marker = recovered.with_suffix(".learnnest.json")
    assert '"status": "completed"' in marker.read_text(encoding="utf-8")


def test_automation_tts_refuses_tampered_cached_mp3_without_republishing(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.automation_delivery as delivery
    from learnnest.automation_delivery import PodcastArtifact, ReviewedMarkdownSource

    pack = ContentPack(
        task_id="20260805-automation-tamper",
        source_fingerprint="tamper-fingerprint",
        evidence=[],
    )
    source = ReviewedMarkdownSource(
        task_id=pack.task_id,
        task_dir=tmp_path / "task",
        content_pack=pack,
        content_pack_sha256="pack",
        markdown="# reviewed\n",
        markdown_sha256="markdown",
        plan_id="plan",
        dossier_sha256="dossier",
    )
    podcast = PodcastArtifact(
        directory=tmp_path / "podcast",
        script=None,  # type: ignore[arg-type]
        speech="同一份已验证的 speech.txt。",
        script_sha256="script",
        speech_sha256="speech",
    )

    class Provider:
        name = "fake"
        model = "fake"
        voice = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def synthesize(self, speech: str, style: str) -> bytes:
            self.calls += 1
            return b"wav"

    monkeypatch.setattr(delivery, "validate_wav_bytes", lambda content: None)
    monkeypatch.setattr(delivery, "probe_audio", lambda path: {"ok": True})
    monkeypatch.setattr(
        delivery,
        "convert_wav_to_mp3",
        lambda source, destination: destination.write_bytes(b"mp3"),
    )
    first_provider = Provider()
    published = delivery.generate_model_reviewed_tts(
        source,
        podcast,
        first_provider,
        output_root=tmp_path,
        delivery_dir=tmp_path / "delivery",
        style_instruction="自然",
    )
    marker = published.with_suffix(".learnnest.json")
    original_published = published.read_bytes()
    original_marker = marker.read_bytes()
    (tmp_path / "delivery" / "tts" / "audio.mp3").write_bytes(b"tampered")
    recovery_provider = Provider()

    with pytest.raises(ValueError, match="cached TTS"):
        delivery.generate_model_reviewed_tts(
            source,
            podcast,
            recovery_provider,
            output_root=tmp_path,
            delivery_dir=tmp_path / "delivery",
            style_instruction="自然",
        )

    assert recovery_provider.calls == 0
    assert published.read_bytes() == original_published
    assert marker.read_bytes() == original_marker
