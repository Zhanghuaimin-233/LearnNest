from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from learnnest.models import ContentPack, StageStatus, TaskRecord
from learnnest.note_models import GENERATED_NOTE_ADAPTER
from learnnest.podcast_models import PodcastScript
from learnnest.rendering import render_podcast_speech
from learnnest.rendering import render_generated_note
from learnnest.standard_note_publication import (
    publish_standard_note,
    write_standard_note_bundle,
)
from learnnest.task_store import create_task, load_task, write_task_atomic
from learnnest.validation import validate_note_markdown_text, validate_task


def _write_json(path: Path, content: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")


def _task(**changes: object) -> TaskRecord:
    data: dict[str, object] = {
        "task_id": "20260711-a1b2c3d4",
        "source_path": "C:/videos/lesson.mp4",
        "source_fingerprint": "a1b2c3d4",
        "title": "lesson",
        "stages": {"content_pack": StageStatus.COMPLETED},
        "artifacts": {"content_pack": ["content_pack.json"]},
    }
    data.update(changes)
    return TaskRecord(**data)


def _write_content_pack(task_dir: Path, artifact_path: str = "transcript.json") -> None:
    _write_json(
        task_dir / "content_pack.json",
        {
            "schema_version": "1.0",
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "evidence": [
                {
                    "id": "tr_0001",
                    "kind": "transcript",
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "text": "打开设置。",
                    "artifact_path": artifact_path,
                }
            ],
        },
    )


def _note_task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    task_dir.mkdir(parents=True)
    return task_dir


def test_validate_task_accepts_consistent_completed_artifacts(tmp_path: Path) -> None:
    task = _task(
        stages={
            "content_pack": StageStatus.COMPLETED,
            "note": StageStatus.COMPLETED,
        },
        artifacts={
            "content_pack": ["content_pack.json"],
            "note": ["llm_outputs/note.json", "llm_outputs/note.md"],
        },
    )
    _write_content_pack(tmp_path)
    _write_json(tmp_path / "transcript.json", {})
    _write_json(
        tmp_path / "llm_outputs" / "note.json",
        {"key_points": [{"evidence_ids": ["tr_0001"]}]},
    )
    (tmp_path / "llm_outputs" / "note.md").write_text(
        "- 字幕 [tr_0001]",
        encoding="utf-8",
    )
    write_task_atomic(tmp_path, task)

    assert validate_task(tmp_path) == []


def test_validate_task_rejects_ambiguous_standard_note_artifacts(
    tmp_path: Path,
) -> None:
    task_dir = _note_task_dir(tmp_path)
    _write_content_pack(task_dir)
    _write_json(task_dir / "transcript.json", {})
    task = _task(
        stages={
            "content_pack": StageStatus.COMPLETED,
            "note": StageStatus.COMPLETED,
        },
        artifacts={"content_pack": ["content_pack.json"]},
    )
    write_task_atomic(task_dir, task)
    first = task_dir / "assisted-draft" / "plan-a" / "reviewed"
    second = task_dir / "assisted-draft" / "plan-b" / "reviewed"
    write_standard_note_bundle(
        task_dir,
        first,
        task,
        "- 第一套 [tr_0001]\n",
        route="assisted_draft",
        status="model_reviewed",
    )
    write_standard_note_bundle(
        task_dir,
        second,
        task,
        "- 第二套 [tr_0001]\n",
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
                        "assisted-draft/plan-a/reviewed/metadata.json",
                        "assisted-draft/plan-a/reviewed/note.md",
                        "assisted-draft/plan-b/reviewed/metadata.json",
                        "assisted-draft/plan-b/reviewed/note.md",
                    ],
                }
            }
        ),
    )

    assert validate_task(task_dir) == [
        "invalid standard note: active standard note metadata/bundle is missing or ambiguous"
    ]


def test_validate_task_checks_standard_podcast_generation_identity_and_shas(
    tmp_path: Path,
) -> None:
    task_dir = _note_task_dir(tmp_path)
    _write_content_pack(task_dir)
    _write_json(task_dir / "transcript.json", {})
    task = _task()
    write_task_atomic(task_dir, task)
    bundle = task_dir / "assisted-draft" / "reviewed"
    body = "- 字幕 [tr_0001]\n"
    write_standard_note_bundle(
        task_dir,
        bundle,
        task,
        body,
        route="assisted_draft",
        status="model_reviewed",
    )
    publish_standard_note(
        task_dir,
        bundle,
        tmp_path,
        provider="fake-reviewer",
        model="fake-1",
    )
    task = load_task(task_dir)
    note_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    script = PodcastScript.model_validate(
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
                    "text": "今天复习设置。",
                    "evidence_ids": ["tr_0001"],
                },
                {
                    "order": 2,
                    "kind": "body",
                    "text": "打开设置。",
                    "evidence_ids": ["tr_0001"],
                },
                {
                    "order": 3,
                    "kind": "outro",
                    "text": "完成复习。",
                    "evidence_ids": ["tr_0001"],
                },
            ],
            "ai_supplements": [],
        }
    )
    podcast_dir = task_dir / "generated_podcasts" / "run-1"
    podcast_dir.mkdir(parents=True)
    script_bytes = (script.model_dump_json(indent=2) + "\n").encode("utf-8")
    speech_bytes = render_podcast_speech(script).encode("utf-8")
    (podcast_dir / "podcast_script.json").write_bytes(script_bytes)
    (podcast_dir / "speech.txt").write_bytes(speech_bytes)
    _write_json(
        podcast_dir / "generation.json",
        {
            "schema_version": "1.0",
            "run_id": "run-1",
            "provider": "fake-podcast",
            "model": "fake-1",
            "attempt_count": 1,
            "status": "completed",
            "task_id": task.task_id,
            "source_fingerprint": task.source_fingerprint,
            "note_content_sha256": note_sha,
            "podcast_script_sha256": hashlib.sha256(script_bytes).hexdigest(),
            "speech_sha256": hashlib.sha256(speech_bytes).hexdigest(),
            "errors": [],
        },
    )
    write_task_atomic(
        task_dir,
        task.model_copy(
            update={
                "stages": {**task.stages, "podcast_script": StageStatus.COMPLETED},
                "artifacts": {
                    **task.artifacts,
                    "podcast_script": [
                        "generated_podcasts/run-1/generation.json",
                        "generated_podcasts/run-1/podcast_script.json",
                        "generated_podcasts/run-1/speech.txt",
                    ],
                },
            }
        ),
    )

    assert validate_task(task_dir) == []
    generation_path = podcast_dir / "generation.json"
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    generation["speech_sha256"] = "0" * 64
    _write_json(generation_path, generation)
    assert "generation.json speech_sha256 does not match speech" in validate_task(
        task_dir
    )


def test_validate_task_rejects_completed_stage_without_artifact(tmp_path: Path) -> None:
    write_task_atomic(
        tmp_path,
        _task(
            stages={"transcript": StageStatus.COMPLETED},
            artifacts={},
        ),
    )

    assert validate_task(tmp_path) == ["completed stage has no artifact: transcript"]


def test_validate_task_rejects_missing_and_escaping_artifact_paths(
    tmp_path: Path,
) -> None:
    write_task_atomic(
        tmp_path,
        _task(
            stages={
                "transcript": StageStatus.COMPLETED,
                "ocr": StageStatus.COMPLETED,
            },
            artifacts={
                "transcript": ["transcript.json"],
                "ocr": ["../outside.json"],
            },
        ),
    )

    assert set(validate_task(tmp_path)) == {
        "artifact is missing: transcript.json",
        "artifact path escapes task root: ../outside.json",
    }


def test_validate_task_rejects_evidence_and_note_references_outside_content_pack(
    tmp_path: Path,
) -> None:
    task = _task(
        stages={
            "content_pack": StageStatus.COMPLETED,
            "note": StageStatus.COMPLETED,
        },
        artifacts={
            "content_pack": ["content_pack.json"],
            "note": ["llm_outputs/note.json", "llm_outputs/note.md"],
        },
    )
    _write_content_pack(tmp_path, artifact_path="../outside.json")
    _write_json(tmp_path / "transcript.json", {})
    _write_json(
        tmp_path / "llm_outputs" / "note.json",
        {"key_points": [{"evidence_ids": ["missing_evidence"]}]},
    )
    (tmp_path / "llm_outputs" / "note.md").write_text("", encoding="utf-8")
    write_task_atomic(tmp_path, task)

    assert validate_task(tmp_path) == [
        "artifact path escapes task root: ../outside.json",
        "note references unknown evidence id: missing_evidence",
    ]


def test_validate_task_accepts_completed_stage_file_and_directory_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / "transcript.json").write_text("{}", encoding="utf-8")
    (tmp_path / "transcript.md").write_text("# lesson", encoding="utf-8")
    (tmp_path / "frames").mkdir()
    write_task_atomic(
        tmp_path,
        _task(
            stages={
                "transcript": StageStatus.COMPLETED,
                "frames": StageStatus.COMPLETED,
            },
            artifacts={
                "transcript": ["transcript.json", "transcript.md"],
                "frames": ["frames"],
            },
        ),
    )

    assert validate_task(tmp_path) == []


def test_validate_task_rejects_artifacts_for_non_completed_stages(
    tmp_path: Path,
) -> None:
    _write_json(tmp_path / "transcript.json", {})
    _write_json(
        tmp_path / "task.json",
        {
            "schema_version": "1.0",
            "task_id": "20260711-a1b2c3d4",
            "source_path": "C:/videos/lesson.mp4",
            "source_fingerprint": "a1b2c3d4",
            "title": "lesson",
            "stages": {"transcript": "pending"},
            "artifacts": {"transcript": ["transcript.json"]},
        },
    )

    assert validate_task(tmp_path) == ["artifact stage is not completed: transcript"]


@pytest.mark.parametrize("forged_reference", ["tr_bad", "custom-id"])
def test_validate_task_rejects_forged_single_bracket_references(
    tmp_path: Path, forged_reference: str
) -> None:
    task_dir = _note_task_dir(tmp_path)
    _write_content_pack(task_dir)
    _write_json(task_dir / "transcript.json", {})
    _write_json(task_dir / "note.json", {"evidence_ids": ["tr_0001"]})
    (task_dir / "note.md").write_text(
        f"- 字幕 [{forged_reference}]",
        encoding="utf-8",
    )
    write_task_atomic(
        task_dir,
        _task(
            stages={"content_pack": "completed", "note": "completed"},
            artifacts={
                "content_pack": ["content_pack.json"],
                "note": ["note.json", "note.md"],
            },
        ),
    )

    assert (
        f"note Markdown references unknown evidence id: {forged_reference}"
        in validate_task(task_dir)
    )


@pytest.mark.parametrize("missing_artifact", ["note.json", "note.md"])
def test_validate_task_requires_both_completed_note_artifacts(
    tmp_path: Path, missing_artifact: str
) -> None:
    task_dir = _note_task_dir(tmp_path)
    _write_content_pack(task_dir)
    _write_json(task_dir / "transcript.json", {})
    note_artifacts = ["note.json", "note.md"]
    note_artifacts.remove(missing_artifact)
    if "note.json" in note_artifacts:
        _write_json(task_dir / "note.json", {"evidence_ids": ["tr_0001"]})
    if "note.md" in note_artifacts:
        (task_dir / "note.md").write_text("- 字幕 [tr_0001]", encoding="utf-8")
    write_task_atomic(
        task_dir,
        _task(
            stages={"content_pack": "completed", "note": "completed"},
            artifacts={
                "content_pack": ["content_pack.json"],
                "note": note_artifacts,
            },
        ),
    )

    assert (
        f"completed note stage is missing required artifact: {missing_artifact}"
        in validate_task(task_dir)
    )


def test_validate_task_reports_non_utf8_note_markdown(tmp_path: Path) -> None:
    task_dir = _note_task_dir(tmp_path)
    _write_content_pack(task_dir)
    _write_json(task_dir / "transcript.json", {})
    _write_json(task_dir / "note.json", {"evidence_ids": ["tr_0001"]})
    (task_dir / "note.md").write_bytes(b"\xff\xfe")
    write_task_atomic(
        task_dir,
        _task(
            stages={"content_pack": "completed", "note": "completed"},
            artifacts={
                "content_pack": ["content_pack.json"],
                "note": ["note.json", "note.md"],
            },
        ),
    )

    assert "invalid note.md: file is not valid UTF-8" in validate_task(task_dir)


def test_validate_task_rejects_unknown_evidence_in_note_markdown(
    tmp_path: Path,
) -> None:
    task_dir = _note_task_dir(tmp_path)
    _write_content_pack(task_dir)
    _write_json(task_dir / "transcript.json", {})
    _write_json(
        task_dir / "note.json",
        {"schema_version": "1.0", "evidence_ids": ["tr_0001"]},
    )
    (task_dir / "note.md").write_text("- 字幕 [tr_9999]", encoding="utf-8")
    write_task_atomic(
        task_dir,
        _task(
            stages={"content_pack": "completed", "note": "completed"},
            artifacts={
                "content_pack": ["content_pack.json"],
                "note": ["note.json", "note.md"],
            },
        ),
    )

    assert "note Markdown references unknown evidence id: tr_9999" in validate_task(
        task_dir
    )


def test_validate_task_rejects_missing_embedded_frame(tmp_path: Path) -> None:
    task_dir = _note_task_dir(tmp_path)
    pack = {
        "schema_version": "1.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "evidence": [
            {
                "id": "fr_0001",
                "kind": "frame",
                "start_ms": 1_000,
                "artifact_path": "frames/selected/fr_0001.png",
            }
        ],
    }
    _write_json(task_dir / "content_pack.json", pack)
    _write_json(task_dir / "note.json", {"evidence_ids": ["fr_0001"]})
    (task_dir / "note.md").write_text(
        "![[视频学习素材/lesson--a1b2c3d4/frames/selected/fr_0001.png]]",
        encoding="utf-8",
    )
    write_task_atomic(
        task_dir,
        _task(
            stages={"content_pack": "completed", "note": "completed"},
            artifacts={
                "content_pack": ["content_pack.json"],
                "note": ["note.json", "note.md"],
            },
        ),
    )

    errors = validate_task(task_dir)

    assert set(errors) == {
        "artifact is missing: frames/selected/fr_0001.png",
        (
            "embedded frame is missing: "
            "视频学习素材/lesson--a1b2c3d4/frames/selected/fr_0001.png"
        ),
    }


def test_validate_task_rejects_embedded_image_outside_content_pack(
    tmp_path: Path,
) -> None:
    task_dir = _note_task_dir(tmp_path)
    _write_content_pack(task_dir)
    _write_json(task_dir / "transcript.json", {})
    unrelated = tmp_path / "unrelated.png"
    unrelated.write_bytes(b"image")
    _write_json(task_dir / "note.json", {"evidence_ids": ["tr_0001"]})
    (task_dir / "note.md").write_text("![[unrelated.png]]", encoding="utf-8")
    write_task_atomic(
        task_dir,
        _task(
            stages={"content_pack": "completed", "note": "completed"},
            artifacts={
                "content_pack": ["content_pack.json"],
                "note": ["note.json", "note.md"],
            },
        ),
    )

    assert "embedded image is not frame evidence: unrelated.png" in validate_task(
        task_dir
    )


def _write_generated_note_task(
    tmp_path: Path, *, ocr_only_step: bool, schema_version: str = "2.0"
) -> Path:
    task_dir = tmp_path / "视频学习素材" / "lesson--a1b2c3d4"
    bundle = task_dir / "generated_notes" / "run-0001"
    bundle.mkdir(parents=True)
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    pack_payload = {
        "schema_version": "1.0",
        "task_id": task.task_id,
        "source_fingerprint": task.source_fingerprint,
        "evidence": [
            {
                "id": "tr_0001",
                "kind": "transcript",
                "start_ms": 0,
                "end_ms": 1_000,
                "text": "打开设置。",
                "artifact_path": "transcript.json",
            },
            {
                "id": "fr_0001",
                "kind": "frame",
                "start_ms": 500,
                "artifact_path": "frames/selected/fr_0001.png",
                "related_evidence_ids": ["ocr_0001"],
            },
            {
                "id": "ocr_0001",
                "kind": "ocr",
                "text": "模型配置",
                "artifact_path": "ocr.json",
                "frame_id": "fr_0001",
            },
        ],
    }
    pack = ContentPack.model_validate(pack_payload)
    note_payload: dict[str, object]
    if schema_version == "2.0":
        note_payload = {
            "schema_version": "2.0",
            "task_id": task.task_id,
            "source_fingerprint": task.source_fingerprint,
            "title": "设置模型参数",
            "audience": {"text": "工具学习者", "evidence_ids": ["tr_0001"]},
            "summary": {"text": "演示设置流程", "evidence_ids": ["tr_0001"]},
            "key_points": [{"text": "确认模型名称", "evidence_ids": ["ocr_0001"]}],
            "steps": [
                {
                    "order": 1,
                    "text": "打开设置",
                    "evidence_ids": ["ocr_0001" if ocr_only_step else "tr_0001"],
                }
            ],
            "cautions": [],
            "ai_supplements": [],
        }
    else:
        note_payload = {
            "schema_version": "3.0",
            "task_id": task.task_id,
            "source_fingerprint": task.source_fingerprint,
            "classification_evidence_ids": ["tr_0001"],
            "note_type": "concept_explanation",
            "title": "设置模型参数",
            "summary": {"text": "演示设置流程", "evidence_ids": ["tr_0001"]},
            "concepts": [
                {
                    "title": "模型配置",
                    "explanation": {
                        "text": "材料解释了模型配置入口。",
                        "evidence_ids": ["tr_0001", "fr_0001"],
                    },
                }
            ],
            "background": None,
            "relationships": [],
            "misconceptions": [],
            "review": None,
            "ai_supplements": [],
        }
    note = GENERATED_NOTE_ADAPTER.validate_python(note_payload)
    _write_json(task_dir / "content_pack.json", pack.model_dump(mode="json"))
    _write_json(task_dir / "transcript.json", {})
    _write_json(task_dir / "ocr.json", {})
    frame = task_dir / "frames" / "selected" / "fr_0001.png"
    frame.parent.mkdir(parents=True)
    frame.write_bytes(b"frame")
    _write_json(bundle / "note.json", note.model_dump(mode="json"))
    markdown = render_generated_note(
        task,
        pack,
        note,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )
    (bundle / "note.md").write_bytes(markdown.encode("utf-8"))
    (bundle / "published_note.md").write_bytes(markdown.encode("utf-8"))
    published = tmp_path / "视频学习笔记" / "lesson.md"
    published.parent.mkdir()
    published.write_bytes(markdown.encode("utf-8"))
    active = task.model_copy(
        update={
            "stages": {
                "content_pack": StageStatus.COMPLETED,
                "note": StageStatus.COMPLETED,
                "publish": StageStatus.COMPLETED,
            },
            "artifacts": {
                "content_pack": ["content_pack.json"],
                "note": [
                    "generated_notes/run-0001/note.json",
                    "generated_notes/run-0001/note.md",
                ],
                "publish": ["generated_notes/run-0001/published_note.md"],
            },
        }
    )
    write_task_atomic(task_dir, active)
    return task_dir


def test_validate_task_accepts_a_generated_note_2_0_bundle(tmp_path: Path) -> None:
    task_dir = _write_generated_note_task(tmp_path, ocr_only_step=False)

    assert validate_task(task_dir) == []


def test_validate_task_accepts_a_generated_note_3_0_bundle(tmp_path: Path) -> None:
    task_dir = _write_generated_note_task(
        tmp_path,
        ocr_only_step=False,
        schema_version="3.0",
    )

    assert validate_task(task_dir) == []


def _hidden_comment_content_pack() -> ContentPack:
    return ContentPack.model_validate(
        {
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "evidence": [
                {
                    "id": "tr_0001",
                    "kind": "transcript",
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "text": "字幕",
                    "artifact_path": "transcript.json",
                },
                {
                    "id": "tr_0002",
                    "kind": "transcript",
                    "start_ms": 1_000,
                    "end_ms": 2_000,
                    "text": "另一段字幕",
                    "artifact_path": "transcript.json",
                },
            ],
        }
    )


def _validate_hidden_comment(tmp_path: Path, markdown: str) -> list[str]:
    return validate_note_markdown_text(
        tmp_path / "视频学习素材" / "lesson--a1b2c3d4",
        markdown,
        _hidden_comment_content_pack(),
    )


@pytest.mark.parametrize(
    "markdown",
    [
        "<!-- evidence : tr_0001 -->",
        "<!-- Evidence: tr_0001 -->",
        "<!-- evidence: tr_0001 -- >",
        "<!-- evidence: tr_0001",
    ],
)
def test_validate_note_markdown_rejects_malformed_evidence_like_marker(
    tmp_path: Path,
    markdown: str,
) -> None:
    assert _validate_hidden_comment(tmp_path, markdown) == [
        "note Markdown contains invalid evidence comment or marker"
    ]


@pytest.mark.parametrize(
    ("markdown", "invalid_id"),
    [
        ("<!-- evidence: tr_0001,,tr_0002 -->", "<empty>"),
        ("<!-- evidence: tr_0001; tr_0002 -->", "tr_0001; tr_0002"),
    ],
)
def test_validate_note_markdown_rejects_non_comma_or_empty_evidence_tokens(
    tmp_path: Path,
    markdown: str,
    invalid_id: str,
) -> None:
    assert _validate_hidden_comment(tmp_path, markdown) == [
        f"note Markdown contains invalid evidence id: {invalid_id}"
    ]


def test_validate_note_markdown_reports_each_mixed_evidence_comment_issue(
    tmp_path: Path,
) -> None:
    markdown = "\n".join(
        [
            "<!-- evidence: tr_0001 -->",
            "<!-- Evidence: tr_0002 -->",
            "<!-- evidence: tr_9999 -->",
        ]
    )

    assert _validate_hidden_comment(tmp_path, markdown) == [
        "note Markdown contains invalid evidence comment or marker",
        "note Markdown references unknown evidence id: tr_9999",
    ]


def test_validate_note_markdown_ignores_unrelated_html_comments(
    tmp_path: Path,
) -> None:
    assert (
        _validate_hidden_comment(
            tmp_path,
            "<!-- processing metadata: evidence remains available -->",
        )
        == []
    )


def test_validate_note_markdown_rejects_malformed_hidden_evidence_id(
    tmp_path: Path,
) -> None:
    errors = _validate_hidden_comment(
        tmp_path,
        "<!-- evidence: tr_0001 tr_0002 -->",
    )

    assert errors == ["note Markdown contains invalid evidence id: tr_0001 tr_0002"]


def test_validate_task_rejects_ocr_only_step_in_generated_note_bundle(
    tmp_path: Path,
) -> None:
    task_dir = _write_generated_note_task(tmp_path, ocr_only_step=True)

    assert (
        "generated note step 1 requires transcript or frame evidence"
        in validate_task(task_dir)
    )
