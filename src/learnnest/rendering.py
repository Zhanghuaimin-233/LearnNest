"""Deterministic Markdown renderers for task evidence and processing state."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from learnnest.models import ContentPack, Evidence, StageStatus, TaskRecord
from learnnest.note_models import (
    AnyGeneratedNote,
    ConceptExplanationNote,
    GeneratedNote,
    NoteStatement,
    PracticalTutorialNote,
    ResourceShareNote,
)
from learnnest.note_validation import referenced_evidence_ids
from learnnest.podcast_models import PodcastScript
from learnnest.util import format_timestamp, safe_title


def render_note(
    task: TaskRecord,
    content_pack: ContentPack,
    *,
    asset_prefix: str,
) -> str:
    """Render a readable evidence view without generating new claims."""
    transcripts = sorted(
        (item for item in content_pack.evidence if item.kind == "transcript"),
        key=lambda item: item.start_ms or 0,
    )
    frames = sorted(
        (item for item in content_pack.evidence if item.kind == "frame"),
        key=lambda item: item.start_ms or 0,
    )
    evidence_by_id = {item.id: item for item in content_pack.evidence}
    lines = [
        f"<!-- learnnest-task-id: {task.task_id} -->",
        f"# {task.title}",
        "",
        f"- 任务：`{task.task_id}`",
        f"- 来源指纹：`{task.source_fingerprint}`",
        "",
        "## 内容索引",
        "",
    ]
    if transcripts:
        lines.extend(
            f"- {format_timestamp(item.start_ms or 0)} [{item.id}] 字幕 — {item.text}"
            for item in transcripts
        )
    else:
        lines.append("- 没有可用字幕证据。")

    lines.extend(["", "## 关键画面", ""])
    if not frames:
        lines.append("- 没有精选画面证据。")
    for frame in frames:
        timestamp = format_timestamp(frame.start_ms or 0)
        lines.extend(
            [
                f"### {timestamp} [{frame.id}]",
                "",
                f"![[{_obsidian_frame_target(asset_prefix, frame)}]]",
                "",
            ]
        )
        transcript = _transcript_at(frame, transcripts)
        if transcript is not None:
            lines.append(f"- 同期字幕 [{transcript.id}]：{transcript.text}")
        for evidence_id in frame.related_evidence_ids:
            related = evidence_by_id[evidence_id]
            lines.append(f"- OCR [{related.id}]：{related.text}")
        if transcript is None and not frame.related_evidence_ids:
            lines.append("- 仅有画面证据，没有可确认的字幕或 OCR 说明。")
        lines.append("")

    lines.extend(["## 原始证据", ""])
    lines.extend(_render_evidence(item) for item in content_pack.evidence)
    return "\n".join(lines) + "\n"


def render_trace(content_pack: ContentPack) -> str:
    """Render the sole human-readable source trace from the canonical pack."""
    transcripts = sorted(
        (item for item in content_pack.evidence if item.kind == "transcript"),
        key=lambda item: item.start_ms or 0,
    )
    frames = sorted(
        (item for item in content_pack.evidence if item.kind == "frame"),
        key=lambda item: item.start_ms or 0,
    )
    evidence_by_id = {item.id: item for item in content_pack.evidence}
    lines = ["# 追溯材料", "", "## 字幕", ""]
    lines.extend(
        f"- {format_timestamp(item.start_ms or 0)} [{item.id}] {item.text}"
        for item in transcripts
    )
    lines.extend(["", "## 精选画面与 OCR", ""])
    for frame in frames:
        lines.append(
            f"- {format_timestamp(frame.start_ms or 0)} [{frame.id}] {frame.artifact_path}"
        )
        lines.extend(
            f"  - OCR [{evidence_id}]：{evidence_by_id[evidence_id].text}"
            for evidence_id in frame.related_evidence_ids
        )
    return "\n".join(lines) + "\n"


def _transcript_at(frame: Evidence, transcripts: list[Evidence]) -> Evidence | None:
    timestamp = frame.start_ms
    if timestamp is None:
        return None
    return next(
        (
            item
            for item in transcripts
            if item.start_ms is not None
            and item.end_ms is not None
            and item.start_ms <= timestamp < item.end_ms
        ),
        None,
    )


def _obsidian_frame_target(asset_prefix: str, frame: Evidence) -> str:
    return str(PurePosixPath(asset_prefix) / PurePosixPath(frame.artifact_path))


def render_note_manifest(
    task: TaskRecord, content_pack: ContentPack
) -> dict[str, object]:
    """Return the JSON validation companion for a rendered note."""
    return {
        "schema_version": "1.0",
        "task_id": task.task_id,
        "evidence_ids": [item.id for item in content_pack.evidence],
    }


def render_generated_note(
    task: TaskRecord,
    content_pack: ContentPack,
    note: AnyGeneratedNote,
    *,
    asset_prefix: str,
) -> str:
    """Render one supported generated note without trusting model Markdown."""
    if isinstance(note, GeneratedNote):
        return _render_generated_note_v2(
            task,
            content_pack,
            note,
            asset_prefix=asset_prefix,
        )
    return _render_generated_note_v3(
        task,
        content_pack,
        note,
        asset_prefix=asset_prefix,
    )


def _render_generated_note_v2(
    task: TaskRecord,
    content_pack: ContentPack,
    note: GeneratedNote,
    *,
    asset_prefix: str,
) -> str:
    evidence_by_id = {item.id: item for item in content_pack.evidence}
    referenced_ids = _generated_note_evidence_ids(note)
    lines = [
        f"<!-- learnnest-task-id: {task.task_id} -->",
        f"# {_escape_markdown_text(note.title)}",
        "",
        "## 适用读者",
        "",
        _escape_markdown_text(note.audience.text),
        "",
        _render_statement_sources(note.audience, evidence_by_id),
        "",
        "## 一句话总结",
        "",
        _escape_markdown_text(note.summary.text),
        "",
        _render_statement_sources(note.summary, evidence_by_id),
        "",
        "## 核心知识点",
        "",
    ]
    for index, statement in enumerate(note.key_points, start=1):
        lines.extend(
            [
                f"### {index}. {_escape_markdown_text(statement.text)}",
                "",
                _render_statement_sources(statement, evidence_by_id),
                "",
            ]
        )

    lines.extend(["## 操作步骤", ""])
    if note.steps:
        for step in note.steps:
            lines.extend(
                [
                    f"{step.order}. {_escape_markdown_text(step.text)}",
                    f"   {_render_statement_sources(step, evidence_by_id)}",
                ]
            )
    else:
        lines.append("- 视频中没有可确认的操作步骤。")

    lines.extend(["", "## 注意事项", ""])
    if note.cautions:
        for caution in note.cautions:
            lines.extend(
                [
                    f"- {_escape_markdown_text(caution.text)}",
                    f"  {_render_statement_sources(caution, evidence_by_id)}",
                ]
            )
    else:
        lines.append("- 视频中没有可确认的注意事项。")

    lines.extend(
        [
            "",
            "## AI 补充",
            "",
            "> AI 补充，不属于视频事实。",
            "",
        ]
    )
    if note.ai_supplements:
        lines.extend(
            f"- {_escape_markdown_text(item.text)}" for item in note.ai_supplements
        )
    else:
        lines.append("- 无。")

    lines.extend(["", "## 引用画面", ""])
    frame_ids = sorted(
        (
            evidence_id
            for evidence_id in referenced_ids
            if evidence_by_id[evidence_id].kind == "frame"
        ),
        key=lambda evidence_id: evidence_by_id[evidence_id].start_ms or 0,
    )
    if frame_ids:
        for frame_id in frame_ids:
            frame = evidence_by_id[frame_id]
            lines.extend(
                [
                    f"### {format_timestamp(frame.start_ms or 0)} [{frame.id}]",
                    "",
                    f"![[{_obsidian_frame_target(asset_prefix, frame)}]]",
                    "",
                ]
            )
    else:
        lines.append("- 本笔记没有直接引用画面证据。")

    lines.extend(
        _render_source_trace(
            referenced_ids,
            evidence_by_id,
            asset_prefix=asset_prefix,
        )
    )
    lines.extend(
        _render_derived_materials(
            task,
            asset_prefix=asset_prefix,
        )
    )
    return "\n".join(lines) + "\n"


def _evidence_comment(evidence_ids: list[str]) -> str:
    return f"<!-- evidence: {', '.join(evidence_ids)} -->"


@dataclass
class _V3RenderState:
    lines: list[str]
    evidence_by_id: dict[str, Evidence]
    embedded_frames: set[str]
    asset_prefix: str


def _append_fact(
    state: _V3RenderState,
    statement: NoteStatement,
    *,
    prefix: str = "",
) -> None:
    state.lines.extend(
        [
            f"{prefix}{_escape_markdown_text(statement.text)}",
            "",
            _evidence_comment(statement.evidence_ids),
            "",
        ]
    )
    _append_first_use_frames(state, statement.evidence_ids)


def _append_first_use_frames(state: _V3RenderState, evidence_ids: list[str]) -> None:
    for evidence_id in evidence_ids:
        evidence = state.evidence_by_id[evidence_id]
        if evidence.kind != "frame" or evidence_id in state.embedded_frames:
            continue
        state.embedded_frames.add(evidence_id)
        state.lines.extend(
            [
                f"![[{_obsidian_frame_target(state.asset_prefix, evidence)}]]",
                "",
            ]
        )


def _append_optional_section(
    state: _V3RenderState,
    heading: str,
    statements: list[NoteStatement],
    *,
    bullet: bool = False,
) -> None:
    if not statements:
        return
    state.lines.extend([f"## {heading}", ""])
    for statement in statements:
        _append_fact(state, statement, prefix="- " if bullet else "")


def _append_callout(
    state: _V3RenderState,
    kind: str,
    title: str,
    statement: NoteStatement,
) -> None:
    state.lines.extend(
        [
            f"> [!{kind}] {title}",
            f"> {_escape_markdown_text(statement.text)}",
            "",
            _evidence_comment(statement.evidence_ids),
            "",
        ]
    )
    _append_first_use_frames(state, statement.evidence_ids)


def _render_concept_body(state: _V3RenderState, note: ConceptExplanationNote) -> None:
    _append_callout(state, "summary", "一句话总结", note.summary)
    if note.background is not None:
        state.lines.extend(["## 问题背景", ""])
        _append_fact(state, note.background)
    state.lines.extend(["## 核心概念", ""])
    for concept in note.concepts:
        state.lines.extend([f"### {_escape_markdown_text(concept.title)}", ""])
        _append_fact(state, concept.explanation)
    _append_optional_section(state, "概念关系", note.relationships)
    _append_optional_section(state, "容易误解", note.misconceptions, bullet=True)
    if note.review is not None:
        state.lines.extend(["## 快速复习", ""])
        _append_callout(state, "tip", "用自己的话复述", note.review)


def _render_resource_body(state: _V3RenderState, note: ResourceShareNote) -> None:
    _append_callout(state, "summary", "一句话价值", note.summary)
    state.lines.extend(["## 资源速览", ""])
    for resource in note.resources:
        state.lines.extend(
            [
                f"### {_escape_markdown_text(resource.name.text)}",
                "",
                _evidence_comment(resource.name.evidence_ids),
                "",
            ]
        )
        _append_first_use_frames(state, resource.name.evidence_ids)
        _append_fact(state, resource.value)
        if resource.suitable_for is not None:
            _append_fact(state, resource.suitable_for, prefix="**适合场景：** ")
        if resource.access_or_usage is not None:
            _append_fact(state, resource.access_or_usage, prefix="**获取或使用：** ")
        if resource.locator is not None:
            state.lines.extend(
                [
                    f"- 资源地址：<{resource.locator.url}>",
                    _evidence_comment(resource.locator.evidence_ids),
                    "",
                ]
            )
            _append_first_use_frames(state, resource.locator.evidence_ids)
        for limitation in resource.limitations:
            _append_fact(state, limitation, prefix="- **限制：** ")
    _append_optional_section(state, "必要提醒", note.reminders, bullet=True)


def _render_practical_body(state: _V3RenderState, note: PracticalTutorialNote) -> None:
    _append_callout(state, "summary", "内容摘要", note.summary)
    state.lines.extend(["## 最终目标", ""])
    _append_fact(state, note.goal)
    _append_optional_section(state, "开始前准备", note.prerequisites, bullet=True)
    state.lines.extend(["## 操作步骤", ""])
    for step in note.steps:
        state.lines.extend(
            [f"### 第 {step.order} 步：{_escape_markdown_text(step.title)}", ""]
        )
        _append_fact(state, step.action, prefix="**要做什么：** ")
        if step.expected_result is not None:
            _append_fact(state, step.expected_result, prefix="**预期结果：** ")
    if note.troubleshooting:
        state.lines.extend(["## 故障处理", ""])
        for item in note.troubleshooting:
            _append_fact(state, item.symptom, prefix="**表现：** ")
            _append_fact(state, item.resolution, prefix="**处理：** ")
    _append_optional_section(
        state,
        "完成检查",
        note.completion_checks,
        bullet=True,
    )
    _append_optional_section(state, "注意事项", note.cautions, bullet=True)


def _render_generated_note_v3(
    task: TaskRecord,
    content_pack: ContentPack,
    note: ConceptExplanationNote | ResourceShareNote | PracticalTutorialNote,
    *,
    asset_prefix: str,
) -> str:
    evidence_by_id = {item.id: item for item in content_pack.evidence}
    state = _V3RenderState(
        lines=[
            f"<!-- learnnest-task-id: {task.task_id} -->",
            f"# {_escape_markdown_text(note.title)}",
            "",
        ],
        evidence_by_id=evidence_by_id,
        embedded_frames=set(),
        asset_prefix=asset_prefix,
    )
    if isinstance(note, ConceptExplanationNote):
        _render_concept_body(state, note)
    elif isinstance(note, ResourceShareNote):
        _render_resource_body(state, note)
    else:
        _render_practical_body(state, note)

    if note.ai_supplements:
        state.lines.extend(
            [
                "## AI 补充",
                "",
                "> AI 补充，不属于视频事实。",
                "",
            ]
        )
        state.lines.extend(
            f"- {_escape_markdown_text(item.text)}" for item in note.ai_supplements
        )

    state.lines.extend(
        _render_v3_trace(
            task,
            referenced_evidence_ids(note),
            evidence_by_id,
            asset_prefix=asset_prefix,
        )
    )
    state.lines.extend(_render_derived_materials(task, asset_prefix=asset_prefix))
    return "\n".join(state.lines) + "\n"


def _render_v3_trace(
    task: TaskRecord,
    evidence_ids: list[str],
    evidence_by_id: dict[str, Evidence],
    *,
    asset_prefix: str,
) -> list[str]:
    prefix = PurePosixPath(asset_prefix)
    groups = _group_evidence_ids(evidence_ids, evidence_by_id)
    lines = ["", "<details>", "<summary>证据与追溯</summary>", ""]
    if task.source_type == "url" and task.source_input is not None:
        lines.append(f"- 原始来源：<{task.source_input}>")
    compact_trace = all(
        item.artifact_path == "content_pack.json"
        for item in evidence_by_id.values()
        if item.kind in {"transcript", "ocr"}
    )
    if compact_trace:
        lines.append(f"- [[{prefix / 'trace.md'}|字幕、画面与 OCR 追溯]]")
    else:
        lines.extend(
            [
                f"- [[{prefix / 'transcript.md'}|字幕原文]]",
                f"- [[{prefix / 'evidence.md'}|证据清单]]",
            ]
        )
    lines.extend(
        [
            f"- [[{prefix / 'content_pack.json'}|结构化证据]]",
            f"- [[{prefix / 'process_report.md'}|处理报告]]",
        ]
    )
    lines.extend(
        f"- {label}：{_format_evidence_id_ranges(group_ids)}"
        for label, group_ids in groups
    )
    lines.extend(["", "</details>"])
    return lines


def _render_derived_materials(
    task: TaskRecord,
    *,
    asset_prefix: str,
) -> list[str]:
    links: list[str] = []
    if task.stages.get("podcast_script") == StageStatus.COMPLETED:
        podcast_paths = [
            PurePosixPath(path) for path in task.artifacts.get("podcast_script", [])
        ]
        script = next(
            (path for path in podcast_paths if path.name == "podcast_script.md"),
            next((path for path in podcast_paths if path.name == "speech.txt"), None),
        )
        if script is not None:
            label = "播客稿" if script.name == "podcast_script.md" else "口播稿"
            links.append(f"- [[{PurePosixPath(asset_prefix) / script}|{label}]]")
    if task.stages.get("tts") == StageStatus.COMPLETED:
        audio = (
            PurePosixPath("视频学习音频")
            / f"{safe_title(task.title)}--{task.task_id[-8:]}.mp3"
        )
        links.append(f"- [[{audio}|音频]]")
    if not links:
        return []
    return ["", "## 延伸材料", "", *links]


def render_report(task: TaskRecord) -> str:
    """Render a short, deterministic report that also records failures."""
    lines = [
        "# 处理报告",
        "",
        f"- 任务：`{task.task_id}`",
        f"- 标题：{task.title}",
        "",
        "## 阶段状态",
        "",
    ]
    lines.extend(f"- {stage}: {status}" for stage, status in task.stages.items())
    if task.error_summary:
        lines.extend(["", "## 错误", "", task.error_summary])
    return "\n".join(lines) + "\n"


def render_podcast_script(script: PodcastScript) -> str:
    """Render an auditable Markdown view while keeping evidence out of speech."""
    section_labels = {
        "intro": "开场",
        "body": "正文",
        "recap": "回顾",
        "outro": "收束",
    }
    lines = [f"# {script.title}", ""]
    for segment in script.segments:
        lines.extend(
            [
                f"## {section_labels[segment.kind]}",
                "",
                segment.text,
                "",
                "来源：" + "、".join(f"[{item}]" for item in segment.evidence_ids),
                "",
            ]
        )
    lines.extend(["## AI 补充", "", "> 以下内容不属于视频事实。", ""])
    if script.ai_supplements:
        lines.extend(f"- {item.text}" for item in script.ai_supplements)
    else:
        lines.append("- 无。")
    return "\n".join(lines) + "\n"


def render_podcast_speech(script: PodcastScript) -> str:
    """Return provider-ready text with no Markdown or evidence identifiers."""
    paragraphs = [segment.text for segment in script.segments]
    if script.ai_supplements:
        paragraphs.append("下面是补充说明。")
        paragraphs.extend(item.text for item in script.ai_supplements)
    return "\n\n".join(paragraphs) + "\n"


def _render_evidence(evidence: Evidence) -> str:
    label = f"[{evidence.id}] {evidence.kind}"
    if evidence.start_ms is not None:
        label += f" @ {format_timestamp(evidence.start_ms)}"
    if evidence.text:
        label += f" — {evidence.text}"
    return f"- {label}"


def _generated_note_evidence_ids(note: GeneratedNote) -> list[str]:
    statements: list[NoteStatement] = [
        note.audience,
        note.summary,
        *note.key_points,
        *note.steps,
        *note.cautions,
    ]
    ordered: list[str] = []
    for statement in statements:
        for evidence_id in statement.evidence_ids:
            if evidence_id not in ordered:
                ordered.append(evidence_id)
    return ordered


def _render_statement_sources(
    statement: NoteStatement, evidence_by_id: dict[str, Evidence]
) -> str:
    groups = _group_evidence_ids(statement.evidence_ids, evidence_by_id)
    sources = "；".join(
        f"{label} {_format_evidence_id_ranges(evidence_ids)}"
        for label, evidence_ids in groups
    )
    return f"来源：{sources}"


def _render_source_trace(
    referenced_ids: list[str],
    evidence_by_id: dict[str, Evidence],
    *,
    asset_prefix: str,
) -> list[str]:
    groups = _group_evidence_ids(referenced_ids, evidence_by_id)
    counts = "；".join(
        f"{label}：{len(evidence_ids)} 条" for label, evidence_ids in groups
    )
    prefix = PurePosixPath(asset_prefix)
    lines = [
        "",
        "## 来源与追溯",
        "",
        f"- 引用规模：{counts}",
        "- 完整材料："
        f"[[{prefix / 'transcript.md'}|字幕原文]] · "
        f"[[{prefix / 'evidence.md'}|证据清单]] · "
        f"[[{prefix / 'content_pack.json'}|结构化证据]]",
        "",
        "<details>",
        f"<summary>展开证据 ID（{len(referenced_ids)} 项）</summary>",
        "",
    ]
    lines.extend(
        f"- {label}：{_format_evidence_id_ranges(evidence_ids)}"
        for label, evidence_ids in groups
    )
    lines.extend(["", "</details>"])
    return lines


def _group_evidence_ids(
    evidence_ids: list[str], evidence_by_id: dict[str, Evidence]
) -> list[tuple[str, list[str]]]:
    kind_order = ("transcript", "frame", "ocr", "ai_supplement")
    grouped: dict[str, list[str]] = {kind: [] for kind in kind_order}
    for evidence_id in evidence_ids:
        evidence = evidence_by_id[evidence_id]
        grouped[evidence.kind].append(evidence_id)
    return [
        (
            _evidence_kind_label(evidence_by_id[grouped[kind][0]]),
            grouped[kind],
        )
        for kind in kind_order
        if grouped[kind]
    ]


def _format_evidence_id_ranges(evidence_ids: list[str]) -> str:
    parsed: list[tuple[str, int, int, str]] = []
    for evidence_id in evidence_ids:
        match = re.fullmatch(r"([a-z]+)_(\d+)", evidence_id)
        if match is None:
            parsed.append((evidence_id, -1, 0, evidence_id))
        else:
            digits = match.group(2)
            parsed.append((match.group(1), int(digits), len(digits), evidence_id))
    parsed.sort(key=lambda item: (item[0], item[1], item[3]))

    tokens: list[str] = []
    index = 0
    while index < len(parsed):
        prefix, number, width, original = parsed[index]
        if number < 0:
            tokens.append(f"[{original}]")
            index += 1
            continue
        end = index
        while (
            end + 1 < len(parsed)
            and parsed[end + 1][0] == prefix
            and parsed[end + 1][2] == width
            and parsed[end + 1][1] == parsed[end][1] + 1
        ):
            end += 1
        if end == index:
            tokens.append(f"[{original}]")
        else:
            tokens.append(
                f"[{prefix}_{number:0{width}d}–{prefix}_{parsed[end][1]:0{width}d}]"
            )
        index = end + 1
    return "、".join(tokens)


def _evidence_kind_label(evidence: Evidence) -> str:
    return {
        "transcript": "字幕",
        "frame": "画面",
        "ocr": "OCR",
        "ai_supplement": "AI 补充",
    }[evidence.kind]


def _escape_markdown_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )
