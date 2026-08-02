from __future__ import annotations

import hashlib
import io
import json
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest

from learnnest.assisted_note_generation import (
    build_reader_dossier,
    create_assisted_plan,
    generate_assisted_plan,
    load_assisted_plan,
    load_assisted_state,
    recover_assisted_plan,
    review_assisted_plan,
)
from learnnest.assisted_note_models import AssistedConnectionSnapshot
from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.task_store import load_task, write_task_atomic


def _snapshot(name: str = "default") -> AssistedConnectionSnapshot:
    return AssistedConnectionSnapshot(
        connection_name=name,
        provider="fake-openai",
        endpoint_identity="https://example.test/v1",
        model="fake-1",
        adapter_revision="1",
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "output"
    task_dir = root / "视频学习素材" / "assisted-task"
    task_dir.mkdir(parents=True)
    pack = ContentPack(
        task_id="20260726-assisted",
        source_fingerprint="assisted-fingerprint",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="打开设置。",
                artifact_path="content_pack.json",
            ),
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=500,
                artifact_path="frames/selected/fr_0001.png",
                related_evidence_ids=["ocr_0001"],
            ),
            Evidence(
                id="ocr_0001",
                kind="ocr",
                start_ms=500,
                end_ms=700,
                text="设置",
                artifact_path="content_pack.json",
                frame_id="fr_0001",
            ),
        ],
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2), encoding="utf-8"
    )
    write_task_atomic(
        task_dir,
        TaskRecord(
            task_id=pack.task_id,
            source_path="C:/videos/assisted.mp4",
            source_fingerprint=pack.source_fingerprint,
            title="辅助笔记",
            stages={"content_pack": StageStatus.COMPLETED},
            artifacts={"content_pack": ["content_pack.json"]},
        ),
    )
    return root


class FakeAssistedProvider:
    name = "fake-openai"
    model = "fake-1"
    endpoint_identity = "https://example.test/v1"

    def __init__(
        self,
        *,
        fail_review: bool = False,
        review_markdowns: list[str] | None = None,
    ) -> None:
        self.writer_calls = 0
        self.reviewer_calls = 0
        self.fail_review = fail_review
        self.review_markdowns = iter(review_markdowns or [])

    def write_markdown(self, dossier_json: str) -> str:
        self.writer_calls += 1
        assert "tr_0001" not in dossier_json
        assert "打开设置" in dossier_json
        return "# 设置\n\n打开设置后完成修改。"

    def review_markdown(self, dossier_json: str, candidate_markdown: str) -> str:
        self.reviewer_calls += 1
        if self.fail_review:
            raise RuntimeError("review transport unavailable")
        assert "打开设置" in dossier_json
        assert candidate_markdown.startswith("# 设置")
        return next(
            self.review_markdowns,
            "# 设置\n\n先打开设置，再完成修改。",
        )


def _plan(
    root: Path,
    *,
    now: datetime = datetime(2026, 7, 26, 8, 0, tzinfo=UTC),
) -> Path:
    snapshot = _snapshot()
    return create_assisted_plan(
        root,
        ["20260726-assisted"],
        writer=snapshot,
        reviewer=snapshot,
        now=now,
    )


def test_plan_freezes_dossier_and_connection_without_provider_call(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)

    plan = load_assisted_plan(plan_path)
    dossier = next((root / "视频学习素材").rglob("dossier.json")).read_text(
        encoding="utf-8"
    )

    assert plan.total_max_calls == 2
    assert plan.writer.connection_name == "default"
    assert "tr_0001" not in dossier
    assert "打开设置" in dossier
    assert load_assisted_state(plan_path).tasks[0].status == "planned"


def test_reader_dossier_keeps_transcript_and_frame_ocr_separate(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    task_dir = next((root / "视频学习素材").rglob("task.json")).parent
    task = TaskRecord.model_validate_json((task_dir / "task.json").read_bytes())
    pack = ContentPack.model_validate_json(
        (task_dir / "content_pack.json").read_bytes()
    )

    dossier = json.loads(build_reader_dossier(task, pack))

    assert dossier["schema_version"] == "1.1"
    assert dossier["instructions"]["source_separation"]
    assert dossier["source_material"]["transcript"]["segments"] == [
        {"start_ms": 0, "end_ms": 1_000, "content": "打开设置。"}
    ]
    assert dossier["source_material"]["visual_frames"] == [
        {
            "start_ms": 500,
            "visual_context": (
                "Only OCR extracted from this frame is supplied; frame pixels "
                "are not included."
            ),
            "ocr": [{"content": "设置"}],
        }
    ]
    assert dossier["source_material"]["orphaned_ocr"] == []
    serialized = json.dumps(dossier, ensure_ascii=False)
    assert "tr_0001" not in serialized
    assert "fr_0001" not in serialized
    assert "ocr_0001" not in serialized


def test_writer_and_reviewer_are_each_called_once_and_stay_isolated(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)
    provider = FakeAssistedProvider()

    generate_assisted_plan(plan_path, root, provider)
    generate_assisted_plan(plan_path, root, provider)
    review_assisted_plan(plan_path, root, provider)
    review_assisted_plan(plan_path, root, provider)

    state = load_assisted_state(plan_path).tasks[0]
    bundle = (
        next((root / "视频学习素材").rglob("assisted-draft"))
        / load_assisted_plan(plan_path).plan_id
    )
    metadata = json.loads(
        (bundle / "reviewed" / "metadata.json").read_text(encoding="utf-8")
    )
    assert provider.writer_calls == provider.reviewer_calls == 1
    assert state.status == "model_reviewed"
    assert state.writer.actual_call_count == state.reviewer.actual_call_count == 1
    assert (bundle / "candidate" / "note.md").is_file()
    assert (bundle / "reviewed" / "note.md").is_file()
    assert metadata["route"] == "assisted_draft"
    assert "quality-first" not in str(bundle)
    reviewed_body = (bundle / "reviewed" / "note.md").read_bytes()
    published = root / "视频学习笔记" / "辅助笔记--assisted.md"
    assert published.read_bytes() == reviewed_body
    task = load_task(next((root / "视频学习素材").rglob("task.json")).parent)
    assert task.stages["note"] is StageStatus.COMPLETED
    assert task.stages["publish"] is StageStatus.COMPLETED
    assert task.artifacts["note"] == [
        f"assisted-draft/{load_assisted_plan(plan_path).plan_id}/reviewed/metadata.json",
        f"assisted-draft/{load_assisted_plan(plan_path).plan_id}/reviewed/note.md",
    ]


def test_two_assisted_plans_replace_same_task_without_extra_provider_calls(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    first_plan = _plan(root)
    second_plan = _plan(root, now=datetime(2026, 7, 26, 8, 1, tzinfo=UTC))
    assert (
        load_assisted_plan(first_plan).plan_id
        != load_assisted_plan(second_plan).plan_id
    )
    provider = FakeAssistedProvider(
        review_markdowns=[
            "# 设置\n\n第一版审核正文。",
            "# 设置\n\n第二版审核正文。",
        ]
    )

    generate_assisted_plan(first_plan, root, provider)
    review_assisted_plan(first_plan, root, provider)
    generate_assisted_plan(second_plan, root, provider)
    review_assisted_plan(second_plan, root, provider)

    first_id = load_assisted_plan(first_plan).plan_id
    second_id = load_assisted_plan(second_plan).plan_id
    task_dir = next((root / "视频学习素材").rglob("task.json")).parent
    task = load_task(task_dir)
    active_reviewed = (
        f"assisted-draft/{second_id}/reviewed/metadata.json",
        f"assisted-draft/{second_id}/reviewed/note.md",
    )
    published = root / "视频学习笔记" / "辅助笔记--assisted.md"
    assert provider.writer_calls == provider.reviewer_calls == 2
    assert task.artifacts["note"] == list(active_reviewed)
    assert published.read_bytes() == (task_dir / active_reviewed[1]).read_bytes()
    assert task.artifacts["note"] != [
        f"assisted-draft/{first_id}/reviewed/metadata.json",
        f"assisted-draft/{first_id}/reviewed/note.md",
    ]


def test_reviewer_failure_keeps_candidate_and_requires_new_plan_for_retry(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)
    provider = FakeAssistedProvider(fail_review=True)

    generate_assisted_plan(plan_path, root, provider)
    review_assisted_plan(plan_path, root, provider)
    review_assisted_plan(plan_path, root, provider)

    state = load_assisted_state(plan_path).tasks[0]
    assert provider.writer_calls == 1
    assert provider.reviewer_calls == 1
    assert state.status == "review_failed"
    assert state.candidate_path is not None
    assert state.reviewed_path is None


def test_recover_recreates_missing_local_candidate_without_provider_call(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)
    provider = FakeAssistedProvider()
    generate_assisted_plan(plan_path, root, provider)
    candidate = next((root / "视频学习素材").rglob("candidate/note.md"))
    candidate.unlink()

    recover_assisted_plan(plan_path, root)

    assert candidate.is_file()
    assert provider.writer_calls == 1
    assert provider.reviewer_calls == 0


def test_recover_finishes_a_persisted_writer_response_without_a_second_call(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)
    provider = FakeAssistedProvider()
    generate_assisted_plan(plan_path, root, provider)
    state_path = plan_path.parent / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["tasks"][0]["writer"]["status"] = "running"
    payload["tasks"][0]["status"] = "local_recovery_failed"
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    candidate = next((root / "视频学习素材").rglob("candidate/note.md"))
    candidate.unlink()

    recover_assisted_plan(plan_path, root)

    state = load_assisted_state(plan_path).tasks[0]
    assert provider.writer_calls == 1
    assert provider.reviewer_calls == 0
    assert candidate.is_file()
    assert state.status == "draft_ready"
    assert state.writer.status == "completed"


def test_changed_content_pack_fails_locally_before_writer_call(tmp_path: Path) -> None:
    root = _root(tmp_path)
    plan_path = _plan(root)
    provider = FakeAssistedProvider()
    pack_path = next((root / "视频学习素材").rglob("content_pack.json"))
    pack_path.write_text("{}", encoding="utf-8")

    generate_assisted_plan(plan_path, root, provider)

    state = load_assisted_state(plan_path).tasks[0]
    assert provider.writer_calls == 0
    assert state.status == "local_recovery_failed"


def test_assisted_reviewed_note_flows_through_podcast_and_tts_without_body_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from learnnest.podcast_generation import generate_and_activate_podcast
    from learnnest.tts_generation import generate_and_activate_tts

    root = _root(tmp_path)
    plan_path = _plan(root)
    note_provider = FakeAssistedProvider()
    generate_assisted_plan(plan_path, root, note_provider)
    review_assisted_plan(plan_path, root, note_provider)

    task_dir = next((root / "视频学习素材").rglob("task.json")).parent
    reviewed_path = next((task_dir).rglob("reviewed/note.md"))
    reviewed_bytes = reviewed_path.read_bytes()
    task = load_task(task_dir)
    note_sha = hashlib.sha256(reviewed_bytes).hexdigest()
    response = json.dumps(
        {
            "schema_version": "1.0",
            "task_id": task.task_id,
            "source_fingerprint": task.source_fingerprint,
            "note_content_sha256": note_sha,
            "title": "设置复习",
            "segments": [
                {
                    "order": 1,
                    "kind": "intro",
                    "text": "今天复习设置流程。",
                    "evidence_ids": ["tr_0001"],
                },
                {
                    "order": 2,
                    "kind": "body",
                    "text": "先打开设置，再完成修改。",
                    "evidence_ids": ["tr_0001"],
                },
                {
                    "order": 3,
                    "kind": "outro",
                    "text": "本期复习到这里。",
                    "evidence_ids": ["tr_0001"],
                },
            ],
            "ai_supplements": [],
        },
        ensure_ascii=False,
    )

    class FakePodcastProvider:
        name = "fake-podcast"
        model = "fake-1"

        def __init__(self) -> None:
            self.calls = 0

        def generate(self, context: str, feedback: tuple[str, ...]) -> str:
            self.calls += 1
            payload = json.loads(context)
            assert payload["note"]["metadata"]["route"] == "assisted_draft"
            assert payload["note"]["markdown"].encode("utf-8") == reviewed_bytes
            assert feedback == ()
            return response

    podcast_provider = FakePodcastProvider()
    podcast_task = generate_and_activate_podcast(
        task_dir,
        podcast_provider,
        root,
    )
    assert podcast_provider.calls == 1
    assert podcast_task.stages["podcast_script"] is StageStatus.COMPLETED

    content_pack_path = task_dir / "content_pack.json"
    metadata_path = next(task_dir.rglob("reviewed/metadata.json"))
    saved_files = {
        content_pack_path: content_pack_path.read_bytes(),
        metadata_path: metadata_path.read_bytes(),
        reviewed_path: reviewed_bytes,
    }
    for path in saved_files:
        path.unlink()

    def wav_bytes() -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16_000)
            stream.writeframes(b"\x00\x00" * 1_600)
        return output.getvalue()

    class FakeTtsProvider:
        name = "fake-tts"
        model = "fake-wav"
        voice = "test"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def synthesize(self, speech: str, style: str) -> bytes:
            self.calls.append(speech)
            assert style
            return wav_bytes()

    import learnnest.tts_generation as tts_module

    def convert(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"fake-mp3")

    def probe(path: Path) -> dict[str, object]:
        codec = "mp3" if path.suffix == ".mp3" else "pcm_s16le"
        return {
            "format": {"duration": "0.1"},
            "streams": [{"codec_type": "audio", "codec_name": codec}],
        }

    monkeypatch.setattr(tts_module, "convert_wav_to_mp3", convert)
    monkeypatch.setattr(tts_module, "probe_audio", probe)
    tts_provider = FakeTtsProvider()
    tts_task = generate_and_activate_tts(task_dir, tts_provider, root)

    for path, content in saved_files.items():
        path.write_bytes(content)
    assert len(tts_provider.calls) == 1
    assert tts_task.stages["tts"] is StageStatus.COMPLETED
    assert reviewed_path.read_bytes() == reviewed_bytes
    published = root / "视频学习笔记" / "辅助笔记--assisted.md"
    assert published.read_bytes() == reviewed_bytes
