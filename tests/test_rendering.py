from __future__ import annotations

import re

from learnnest.models import ContentPack, Evidence
from learnnest.note_models import GeneratedNote
from learnnest.rendering import render_note, render_report
from learnnest.task_store import create_task


def _pack() -> ContentPack:
    return ContentPack(
        task_id="20260711-a1b2c3d4",
        source_fingerprint="a1b2c3d4",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_500,
                text="打开设置。",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=1_000,
                artifact_path="frames/selected/fr_0001.png",
                related_evidence_ids=["ocr_0001"],
            ),
            Evidence(
                id="ocr_0001",
                kind="ocr",
                text="设置",
                artifact_path="ocr.json",
                frame_id="fr_0001",
            ),
        ],
    )


def test_render_note_only_lists_known_evidence() -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
    )
    pack = _pack()

    note = render_note(
        task,
        pack,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )

    references = set(re.findall(r"\[([a-z]+_\d+)\]", note))
    assert references == {item.id for item in pack.evidence}
    assert "学习摘要" not in note


def test_render_note_organizes_known_evidence_without_interpretation() -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )

    note = render_note(
        task,
        _pack(),
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )

    assert "<!-- learnnest-task-id: 20260711-a1b2c3d4 -->" in note
    assert "## 内容索引" in note
    assert "## 关键画面" in note
    assert "## 原始证据" in note
    assert "同期字幕 [tr_0001]" in note
    assert "OCR [ocr_0001]" in note
    assert "AI 总结" not in note


def test_render_note_uses_the_actual_task_directory_for_frame_embeds() -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="renamed title",
        profile="note",
    )

    note = render_note(
        task,
        _pack(),
        asset_prefix="视频学习素材/custom-task-directory",
    )

    assert "![[视频学习素材/custom-task-directory/frames/selected/fr_0001.png]]" in note
    assert "renamed title--a1b2c3d4" not in note


def test_render_note_treats_transcript_intervals_as_half_open() -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    pack = ContentPack(
        task_id=task.task_id,
        source_fingerprint=task.source_fingerprint,
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="前一段",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="tr_0002",
                kind="transcript",
                start_ms=1_000,
                end_ms=2_000,
                text="后一段",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=1_000,
                artifact_path="frames/selected/fr_0001.png",
            ),
        ],
    )

    note = render_note(
        task,
        pack,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )

    frame_section = note.split("### 00:00:01 [fr_0001]", maxsplit=1)[1]
    assert "同期字幕 [tr_0002]：后一段" in frame_section
    assert "同期字幕 [tr_0001]：前一段" not in frame_section


def test_render_note_does_not_guess_nearest_transcript() -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    pack = ContentPack(
        task_id=task.task_id,
        source_fingerprint=task.source_fingerprint,
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="较早字幕",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=2_000,
                artifact_path="frames/selected/fr_0001.png",
            ),
        ],
    )

    note = render_note(
        task,
        pack,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )

    frame_section = note.split("### 00:00:02 [fr_0001]", maxsplit=1)[1]
    assert "同期字幕" not in frame_section
    assert "仅有画面证据" in frame_section


def test_render_report_includes_failed_stage_and_error() -> None:
    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
    ).model_copy(
        update={
            "stages": {"source": "completed", "transcript": "failed"},
            "error_summary": "audio stream is required",
        }
    )

    report = render_report(task)

    assert "transcript: failed" in report
    assert "audio stream is required" in report


def test_render_generated_note_uses_fixed_sections_and_real_source_types() -> None:
    from learnnest.rendering import render_generated_note

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    note = GeneratedNote.model_validate(
        {
            "schema_version": "2.0",
            "task_id": task.task_id,
            "source_fingerprint": task.source_fingerprint,
            "title": "设置模型参数",
            "audience": {"text": "工具学习者", "evidence_ids": ["tr_0001"]},
            "summary": {
                "text": "演示配置流程",
                "evidence_ids": ["tr_0001", "fr_0001"],
            },
            "key_points": [{"text": "确认模型名称", "evidence_ids": ["ocr_0001"]}],
            "steps": [{"order": 1, "text": "打开设置", "evidence_ids": ["fr_0001"]}],
            "cautions": [],
            "ai_supplements": [{"text": "入口名称可能变化"}],
        }
    )

    rendered = render_generated_note(
        task,
        _pack(),
        note,
        asset_prefix="视频学习素材/custom-task-directory",
    )

    headings = [
        "## 适用读者",
        "## 一句话总结",
        "## 核心知识点",
        "## 操作步骤",
        "## 注意事项",
        "## AI 补充",
        "## 引用画面",
        "## 来源与追溯",
    ]
    assert [rendered.index(heading) for heading in headings] == sorted(
        rendered.index(heading) for heading in headings
    )
    assert "AI 补充，不属于视频事实" in rendered
    assert "OCR [ocr_0001]" in rendered
    assert "画面 [fr_0001]" in rendered
    assert (
        "![[视频学习素材/custom-task-directory/frames/selected/fr_0001.png]]"
        in rendered
    )


def test_render_generated_note_v2_output_is_byte_compatible() -> None:
    from learnnest.rendering import render_generated_note

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    note = GeneratedNote.model_validate(
        {
            "schema_version": "2.0",
            "task_id": task.task_id,
            "source_fingerprint": task.source_fingerprint,
            "title": "兼容快照",
            "audience": {"text": "读者", "evidence_ids": ["tr_0001"]},
            "summary": {"text": "总结", "evidence_ids": ["tr_0001"]},
            "key_points": [{"text": "知识点", "evidence_ids": ["tr_0001"]}],
            "steps": [],
            "cautions": [],
            "ai_supplements": [],
        }
    )

    rendered = render_generated_note(
        task,
        _pack(),
        note,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )

    assert (
        rendered
        == """<!-- learnnest-task-id: 20260711-a1b2c3d4 -->
# 兼容快照

## 适用读者

读者

来源：字幕 [tr_0001]

## 一句话总结

总结

来源：字幕 [tr_0001]

## 核心知识点

### 1. 知识点

来源：字幕 [tr_0001]

## 操作步骤

- 视频中没有可确认的操作步骤。

## 注意事项

- 视频中没有可确认的注意事项。

## AI 补充

> AI 补充，不属于视频事实。

- 无。

## 引用画面

- 本笔记没有直接引用画面证据。

## 来源与追溯

- 引用规模：字幕：1 条
- 完整材料：[[视频学习素材/lesson--a1b2c3d4/transcript.md|字幕原文]] · [[视频学习素材/lesson--a1b2c3d4/evidence.md|证据清单]] · [[视频学习素材/lesson--a1b2c3d4/content_pack.json|结构化证据]]

<details>
<summary>展开证据 ID（1 项）</summary>

- 字幕：[tr_0001]

</details>
"""
    )


def test_render_generated_note_links_only_completed_derived_materials() -> None:
    from learnnest.rendering import render_generated_note

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="full",
    ).model_copy(
        update={
            "stages": {"podcast_script": "completed", "tts": "completed"},
            "artifacts": {
                "podcast_script": [
                    "generated_podcasts/run-1/podcast_script.json",
                    "generated_podcasts/run-1/podcast_script.md",
                    "generated_podcasts/run-1/speech.txt",
                ],
                "tts": [
                    "generated_audio/run-2/audio.json",
                    "generated_audio/run-2/audio.wav",
                    "generated_audio/run-2/audio.mp3",
                ],
            },
        }
    )
    note = GeneratedNote.model_validate(
        {
            "schema_version": "2.0",
            "task_id": task.task_id,
            "source_fingerprint": task.source_fingerprint,
            "title": "设置教程",
            "audience": {"text": "初学者", "evidence_ids": ["tr_0001"]},
            "summary": {"text": "打开设置", "evidence_ids": ["tr_0001"]},
            "key_points": [{"text": "确认入口", "evidence_ids": ["tr_0001"]}],
            "steps": [],
            "cautions": [],
            "ai_supplements": [],
        }
    )

    rendered = render_generated_note(
        task,
        _pack(),
        note,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )

    assert "## 延伸材料" in rendered
    assert (
        "[[视频学习素材/lesson--a1b2c3d4/generated_podcasts/"
        "run-1/podcast_script.md|播客稿]]" in rendered
    )
    assert "[[视频学习音频/lesson--a1b2c3d4.mp3|音频]]" in rendered


def test_render_generated_note_escapes_model_markdown_deterministically() -> None:
    from learnnest.rendering import render_generated_note

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    payload = {
        "schema_version": "2.0",
        "task_id": task.task_id,
        "source_fingerprint": task.source_fingerprint,
        "title": "配置 `model` [参数]",
        "audience": {"text": "使用 C:\\tools 的人", "evidence_ids": ["tr_0001"]},
        "summary": {"text": "确认 [tr_9999] 只是文本", "evidence_ids": ["tr_0001"]},
        "key_points": [{"text": "使用 `tiny`", "evidence_ids": ["tr_0001"]}],
        "steps": [],
        "cautions": [],
        "ai_supplements": [],
    }
    note = GeneratedNote.model_validate(payload)

    first = render_generated_note(
        task,
        _pack(),
        note,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )
    second = render_generated_note(
        task,
        _pack(),
        note,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )

    assert first == second
    assert "配置 \\`model\\` \\[参数\\]" in first
    assert "确认 \\[tr_9999\\] 只是文本" in first
    assert "C:\\\\tools" in first


def test_render_generated_note_orders_referenced_frames_by_timestamp() -> None:
    from learnnest.rendering import render_generated_note

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    pack = ContentPack(
        task_id=task.task_id,
        source_fingerprint=task.source_fingerprint,
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=3_000,
                text="教程字幕",
                artifact_path="transcript.json",
            ),
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=2_000,
                artifact_path="frames/selected/fr_0001.png",
            ),
            Evidence(
                id="fr_0002",
                kind="frame",
                start_ms=1_000,
                artifact_path="frames/selected/fr_0002.png",
            ),
        ],
    )
    note = GeneratedNote.model_validate(
        {
            "schema_version": "2.0",
            "task_id": task.task_id,
            "source_fingerprint": task.source_fingerprint,
            "title": "教程",
            "audience": {"text": "学习者", "evidence_ids": ["tr_0001"]},
            "summary": {"text": "后一个画面", "evidence_ids": ["fr_0001"]},
            "key_points": [{"text": "前一个画面", "evidence_ids": ["fr_0002"]}],
            "steps": [],
            "cautions": [],
            "ai_supplements": [],
        }
    )

    rendered = render_generated_note(
        task,
        pack,
        note,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )
    frame_section = rendered.split("## 引用画面", maxsplit=1)[1].split(
        "## 来源与追溯", maxsplit=1
    )[0]

    assert frame_section.index("[fr_0002]") < frame_section.index("[fr_0001]")


def test_render_generated_note_compresses_sources_and_does_not_repeat_raw_text() -> (
    None
):
    from learnnest.rendering import render_generated_note

    task = create_task(
        task_id="20260711-a1b2c3d4",
        source_path="C:/videos/lesson.mp4",
        source_fingerprint="a1b2c3d4",
        title="lesson",
        profile="note",
    )
    evidence = [
        Evidence(
            id=f"tr_{index:04d}",
            kind="transcript",
            start_ms=(index - 1) * 1_000,
            end_ms=index * 1_000,
            text=f"不应复制到索引的字幕 {index}",
            artifact_path="transcript.json",
        )
        for index in range(1, 5)
    ]
    pack = ContentPack(
        task_id=task.task_id,
        source_fingerprint=task.source_fingerprint,
        evidence=evidence,
    )
    statement = {
        "text": "四段字幕共同支持这个结论",
        "evidence_ids": [item.id for item in evidence],
    }
    note = GeneratedNote.model_validate(
        {
            "schema_version": "2.0",
            "task_id": task.task_id,
            "source_fingerprint": task.source_fingerprint,
            "title": "精简来源",
            "audience": statement,
            "summary": statement,
            "key_points": [statement],
            "steps": [],
            "cautions": [],
            "ai_supplements": [],
        }
    )

    rendered = render_generated_note(
        task,
        pack,
        note,
        asset_prefix="视频学习素材/lesson--a1b2c3d4",
    )
    source_section = rendered.split("## 来源与追溯", maxsplit=1)[1]

    assert "来源：字幕 [tr_0001–tr_0004]" in rendered
    assert "字幕：4 条" in source_section
    assert "[[视频学习素材/lesson--a1b2c3d4/transcript.md|字幕原文]]" in source_section
    assert "<details>" in source_section
    assert "tr_0001–tr_0004" in source_section
    assert "不应复制到索引的字幕" not in source_section
    assert "## 来源索引" not in rendered
