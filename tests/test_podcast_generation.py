from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from helpers.note_v3_fixtures import (
    concept_payload,
    practical_payload,
    resource_payload,
)
from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.task_store import load_task, write_task_atomic


class FakePodcastProvider:
    name = "fake-podcast"
    model = "queued"

    def __init__(
        self, responses: list[str] | None = None, error: Exception | None = None
    ):
        self.responses = iter(responses or [])
        self.error = error
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def generate(self, context: str, feedback: tuple[str, ...]) -> str:
        self.calls.append((context, feedback))
        if self.error is not None:
            raise self.error
        return next(self.responses)


def _legacy_workspace(tmp_path: Path) -> tuple[Path, str]:
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    pack = ContentPack(
        task_id="20260711-a1b2c3d4",
        source_fingerprint="a1b2c3d4",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="打开设置。",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="tr_0002",
                kind="transcript",
                start_ms=1_000,
                end_ms=2_000,
                text="保存配置。",
                artifact_path="transcript.json",
            ),
        ],
    )
    (task_dir / "content_pack.json").write_text(
        pack.model_dump_json(indent=2), encoding="utf-8"
    )
    note_dir = task_dir / "generated_notes" / "note-run"
    note_dir.mkdir(parents=True)
    note_payload = {
        "schema_version": "2.0",
        "task_id": pack.task_id,
        "source_fingerprint": pack.source_fingerprint,
        "title": "模型设置",
        "audience": {"text": "工具学习者", "evidence_ids": ["tr_0001"]},
        "summary": {"text": "打开并保存设置", "evidence_ids": ["tr_0001"]},
        "key_points": [{"text": "保存前确认", "evidence_ids": ["tr_0002"]}],
        "steps": [{"order": 1, "text": "打开设置", "evidence_ids": ["tr_0001"]}],
        "cautions": [],
        "ai_supplements": [],
    }
    note_bytes = (
        json.dumps(note_payload, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    (note_dir / "note.json").write_bytes(note_bytes)
    (note_dir / "note.md").write_text("# 模型设置\n", encoding="utf-8")
    write_task_atomic(
        task_dir,
        TaskRecord(
            task_id=pack.task_id,
            source_path="C:/videos/lesson.mp4",
            source_fingerprint=pack.source_fingerprint,
            title="lesson",
            profile="full",
            stages={
                "content_pack": "completed",
                "note": "completed",
                "publish": "completed",
                "podcast_script": "skipped",
                "tts": "skipped",
            },
            artifacts={
                "content_pack": ["content_pack.json"],
                "note": [
                    "generated_notes/note-run/note.json",
                    "generated_notes/note-run/note.md",
                ],
            },
            providers={"note": "fake-note"},
            models={"note": "queued"},
        ),
    )
    return task_dir, hashlib.sha256(note_bytes).hexdigest()


def workspace(tmp_path: Path) -> tuple[Path, str]:
    task_dir, _ = _legacy_workspace(tmp_path)
    task = load_task(task_dir)
    bundle = task_dir / "assisted-draft" / "reviewed"
    body = (
        "<!-- LearnNest: assisted_draft; model_reviewed is not source_valid or human-reviewed. -->\n\n"
        "# 标准笔记\n\n正文。\n"
    )
    from learnnest.standard_note_publication import (
        publish_standard_note,
        write_standard_note_bundle,
    )

    write_standard_note_bundle(
        task_dir,
        bundle,
        task,
        body,
        route="assisted_draft",
        status="model_reviewed",
    )
    legacy_dir = task_dir / "generated_notes" / "note-run"
    (legacy_dir / "note.json").unlink()
    (legacy_dir / "note.md").unlink()
    standard_task = task.model_copy(
        update={
            "artifacts": {
                **task.artifacts,
                "note": [
                    "assisted-draft/reviewed/metadata.json",
                    "assisted-draft/reviewed/note.md",
                ],
            }
        }
    )
    write_task_atomic(
        task_dir,
        standard_task,
    )
    publish_standard_note(
        task_dir,
        bundle,
        tmp_path,
        provider="fake-reviewer",
        model="fake-1",
        expected_route="assisted_draft",
        expected_status="model_reviewed",
    )
    return task_dir, hashlib.sha256(body.encode("utf-8")).hexdigest()


def valid_script(note_sha: str) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "note_content_sha256": note_sha,
            "title": "模型设置复习",
            "segments": [
                {
                    "order": 1,
                    "kind": "intro",
                    "text": "今天复习模型设置。",
                    "evidence_ids": ["tr_0001"],
                },
                {
                    "order": 2,
                    "kind": "body",
                    "text": "打开设置并确认配置。",
                    "evidence_ids": ["tr_0001"],
                },
                {
                    "order": 3,
                    "kind": "outro",
                    "text": "最后记得保存。",
                    "evidence_ids": ["tr_0002"],
                },
            ],
            "ai_supplements": [],
        },
        ensure_ascii=False,
    )


def _v3_payload_for_workspace(
    payload_factory: Callable[[], dict[str, object]],
) -> dict[str, object]:
    payload = payload_factory()
    payload["task_id"] = "20260711-a1b2c3d4"
    payload["source_fingerprint"] = "a1b2c3d4"

    def bind_evidence(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"classification_evidence_ids", "evidence_ids"}:
                    value[key] = ["tr_0001"]
                else:
                    bind_evidence(child)
        elif isinstance(value, list):
            for child in value:
                bind_evidence(child)

    bind_evidence(payload)
    if payload.get("note_type") == "resource_share":
        resources = payload["resources"]
        assert isinstance(resources, list)
        for resource in resources:
            assert isinstance(resource, dict)
            resource["locator"] = None
    return payload


def _write_active_v3_note(
    task_dir: Path,
    payload_factory: Callable[[], dict[str, object]],
) -> bytes:
    payload = _v3_payload_for_workspace(payload_factory)
    note_bytes = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + " \n"
    ).encode("utf-8")
    (task_dir / "generated_notes" / "note-run" / "note.json").write_bytes(note_bytes)
    return note_bytes


def _standard_workspace(tmp_path: Path) -> tuple[Path, str, str]:
    task_dir, note_sha = workspace(tmp_path)
    body = (task_dir / "assisted-draft" / "reviewed" / "note.md").read_text(
        encoding="utf-8"
    )
    return task_dir, note_sha, body


def test_podcast_context_consumes_standard_markdown_and_metadata(
    tmp_path: Path,
) -> None:
    import learnnest.podcast_generation as podcast_module

    task_dir, note_sha, body = _standard_workspace(tmp_path)

    context = podcast_module._load_context(task_dir)

    provider_context = json.loads(context.provider_context_json)
    assert context.note_sha256 == note_sha
    assert provider_context["note"]["markdown"] == body
    assert provider_context["note"]["metadata"]["route"] == "assisted_draft"


@pytest.mark.parametrize(
    "payload_factory",
    [concept_payload, resource_payload, practical_payload],
    ids=["concept", "resource", "practical"],
)
def test_podcast_context_rejects_legacy_v3_without_standard_note(
    payload_factory: Callable[[], dict[str, object]],
    tmp_path: Path,
) -> None:
    import learnnest.podcast_generation as podcast_module

    task_dir, _ = _legacy_workspace(tmp_path)
    _write_active_v3_note(task_dir, payload_factory)

    with pytest.raises(ValueError, match="standard note metadata"):
        podcast_module._load_context(task_dir)


def test_podcast_context_does_not_parse_legacy_v3_note(
    tmp_path: Path,
) -> None:
    import learnnest.podcast_generation as podcast_module

    task_dir, _ = _legacy_workspace(tmp_path)
    payload = _v3_payload_for_workspace(concept_payload)
    summary = payload["summary"]
    assert isinstance(summary, dict)
    summary["evidence_ids"] = ["tr_9999"]
    note_path = task_dir / "generated_notes" / "note-run" / "note.json"
    note_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="standard note metadata"):
        podcast_module._load_context(task_dir)


def test_generate_podcast_activates_immutable_bundle(tmp_path: Path) -> None:
    from learnnest.podcast_generation import generate_and_activate_podcast

    task_dir, note_sha = workspace(tmp_path)
    old_task = load_task(task_dir)
    write_task_atomic(
        task_dir,
        old_task.model_copy(
            update={
                "providers": {**old_task.providers, "tts": "old-tts"},
                "models": {**old_task.models, "tts": "old-model"},
            }
        ),
    )
    provider = FakePodcastProvider([valid_script(note_sha)])

    task = generate_and_activate_podcast(task_dir, provider)

    assert len(provider.calls) == 1
    assert task.stages["podcast_script"] is StageStatus.COMPLETED
    assert task.stages["tts"] is StageStatus.PENDING
    assert task.providers["podcast_script"] == "fake-podcast"
    assert "tts" not in task.providers
    assert "tts" not in task.models
    assert task.active_attempt_id is None
    assert task.attempts[-1].from_stage == "podcast_script"
    assert task.attempts[-1].status == "completed"
    bundle = task_dir / Path(task.artifacts["podcast_script"][0]).parent
    generation = json.loads((bundle / "generation.json").read_text(encoding="utf-8"))
    assert generation["status"] == "completed"
    assert generation["task_id"] == task.task_id
    assert generation["source_fingerprint"] == task.source_fingerprint
    assert generation["note_content_sha256"] == note_sha
    assert (
        generation["podcast_script_sha256"]
        == hashlib.sha256((bundle / "podcast_script.json").read_bytes()).hexdigest()
    )
    assert (
        generation["speech_sha256"]
        == hashlib.sha256((bundle / "speech.txt").read_bytes()).hexdigest()
    )
    assert {Path(path).name for path in task.artifacts["podcast_script"]} == {
        "generation.json",
        "podcast_script.json",
        "speech.txt",
    }
    assert (bundle / "podcast_script.json").is_file()
    assert not (bundle / "podcast_script.md").exists()
    assert not list(bundle.glob("attempt-*.raw.txt"))
    speech = (bundle / "speech.txt").read_text(encoding="utf-8")
    assert "tr_" not in speech
    assert load_task(task_dir) == task


def test_generate_podcast_rejects_ambiguous_standard_note_before_provider_call(
    tmp_path: Path,
) -> None:
    from learnnest.podcast_generation import generate_and_activate_podcast
    from learnnest.standard_note_publication import write_standard_note_bundle

    task_dir, note_sha = workspace(tmp_path)
    task = load_task(task_dir)
    second = task_dir / "assisted-draft" / "plan-b" / "reviewed"
    write_standard_note_bundle(
        task_dir,
        second,
        task,
        "<!-- LearnNest: assisted_draft; model_reviewed is not source_valid or human-reviewed. -->\n\n"
        "# 标准笔记\n\n正文。\n",
        route="assisted_draft",
        status="model_reviewed",
    )
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "artifacts": {
                    **task.artifacts,
                    "note": [
                        *task.artifacts["note"],
                        "assisted-draft/plan-b/reviewed/metadata.json",
                        "assisted-draft/plan-b/reviewed/note.md",
                    ],
                }
            }
        ),
    )
    provider = FakePodcastProvider([valid_script(note_sha)])

    with pytest.raises(ValueError, match="ambiguous"):
        generate_and_activate_podcast(task_dir, provider)

    failed = load_task(task_dir)
    assert provider.calls == []
    assert failed.stages["podcast_script"] is not StageStatus.COMPLETED
    assert failed.attempts[-1].status == "failed"


def test_activate_podcast_rejects_tampered_generation_before_task_write(
    tmp_path: Path,
) -> None:
    import learnnest.podcast_generation as podcast_module

    task_dir, note_sha = workspace(tmp_path)
    bundle = podcast_module.generate_podcast_bundle(
        task_dir,
        FakePodcastProvider([valid_script(note_sha)]),
        run_id="activation-check",
    )
    generation_path = bundle / "generation.json"
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    generation["speech_sha256"] = "0" * 64
    generation_path.write_text(
        json.dumps(generation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    original_task = (task_dir / "task.json").read_bytes()

    with pytest.raises(ValueError, match="generation.json"):
        podcast_module._activate_podcast_bundle(
            task_dir,
            bundle,
            provider="fake-podcast",
            model="queued",
        )

    assert (task_dir / "task.json").read_bytes() == original_task


def test_generate_podcast_preserves_published_standard_note_body(
    tmp_path: Path,
) -> None:
    from learnnest.podcast_generation import generate_and_activate_podcast

    task_dir, note_sha = workspace(tmp_path)
    original_body = (task_dir / "assisted-draft" / "reviewed" / "note.md").read_bytes()
    provider = FakePodcastProvider([valid_script(note_sha)])

    task = generate_and_activate_podcast(task_dir, provider, tmp_path)

    active_note = next(
        task_dir / path
        for path in task.artifacts["note"]
        if Path(path).name == "note.md"
    )
    assert hashlib.sha256(active_note.read_bytes()).hexdigest() == note_sha
    published = tmp_path / "视频学习笔记" / "lesson--a1b2c3d4.md"
    assert published.read_bytes() == original_body


def test_generate_podcast_retries_once_on_invalid_json(tmp_path: Path) -> None:
    from learnnest.podcast_generation import generate_and_activate_podcast

    task_dir, note_sha = workspace(tmp_path)
    provider = FakePodcastProvider(["not-json", valid_script(note_sha)])

    generate_and_activate_podcast(task_dir, provider)

    assert len(provider.calls) == 2
    assert provider.calls[0][1] == ()
    assert provider.calls[1][1]


def test_generate_podcast_retains_raw_responses_only_when_explicitly_requested(
    tmp_path: Path,
) -> None:
    from learnnest.podcast_generation import generate_podcast_bundle

    task_dir, note_sha = workspace(tmp_path)
    raw = valid_script(note_sha)

    bundle = generate_podcast_bundle(
        task_dir,
        FakePodcastProvider([raw]),
        retain_debug_artifacts=True,
    )

    assert (bundle / "attempt-1.raw.txt").read_text(encoding="utf-8") == raw + "\n"


def test_generate_podcast_keeps_failed_bundle_without_mutating_task(
    tmp_path: Path,
) -> None:
    from learnnest.podcast_generation import (
        PodcastGenerationError,
        generate_podcast_bundle,
    )

    task_dir, _ = workspace(tmp_path)
    old_task = (task_dir / "task.json").read_bytes()
    provider = FakePodcastProvider(["bad", "still-bad"])

    with pytest.raises(PodcastGenerationError) as captured:
        generate_podcast_bundle(task_dir, provider, run_id="failed-run")

    assert len(provider.calls) == 2
    assert (task_dir / "task.json").read_bytes() == old_task
    metadata = json.loads(
        (captured.value.bundle_path / "generation.json").read_text(encoding="utf-8")
    )
    assert metadata["status"] == "failed"


def test_generate_podcast_rejects_tampered_standard_note_before_provider_call(
    tmp_path: Path,
) -> None:
    from learnnest.podcast_generation import generate_and_activate_podcast

    task_dir, note_sha = workspace(tmp_path)
    note_path = task_dir / "assisted-draft" / "reviewed" / "note.md"
    note_path.write_bytes(note_path.read_bytes() + b"tampered")
    provider = FakePodcastProvider([valid_script(note_sha)])

    with pytest.raises(ValueError, match="standard note body SHA"):
        generate_and_activate_podcast(task_dir, provider)

    assert provider.calls == []
    failed = load_task(task_dir)
    assert failed.attempts[-1].status == "failed"
    assert failed.attempts[-1].failed_stage == "podcast_script"


@pytest.mark.parametrize(
    ("artifact_update", "message"),
    [
        (
            {"note": ["assisted-draft/reviewed/note.md"]},
            "standard note metadata",
        ),
        ({"content_pack": ["../content_pack.json"]}, "outside task"),
    ],
)
def test_podcast_rejects_missing_or_escaping_active_artifact(
    tmp_path: Path,
    artifact_update: dict[str, list[str]],
    message: str,
) -> None:
    import learnnest.podcast_generation as podcast_module

    task_dir, _ = workspace(tmp_path)
    task = load_task(task_dir)
    write_task_atomic(
        task_dir,
        task.model_copy(update={"artifacts": {**task.artifacts, **artifact_update}}),
    )

    with pytest.raises(ValueError, match=message):
        podcast_module._load_context(task_dir)


def test_external_podcast_uses_same_validation_and_activation(tmp_path: Path) -> None:
    from learnnest.podcast_generation import build_and_activate_external_podcast

    task_dir, note_sha = workspace(tmp_path)
    external = tmp_path / "podcast.json"
    external.write_text(valid_script(note_sha), encoding="utf-8")

    task = build_and_activate_external_podcast(task_dir, external)

    assert task.providers["podcast_script"] == "external-agent"
    assert task.models["podcast_script"] == "external"
