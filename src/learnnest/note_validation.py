"""Semantic validation for generated structured notes."""

from __future__ import annotations

import re
from collections.abc import Iterator
from learnnest.models import ContentPack
from learnnest.note_models import (
    GENERATED_NOTE_ADAPTER,
    AnyGeneratedNote,
    ConceptExplanationNote,
    GeneratedNote,
    NoteStatement,
    PracticalTutorialNote,
    ResourceShareNote,
)
from learnnest.note_types import ConcreteNoteType

_URL_TOKEN_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_COGNITIVE_VERBS = ("了解", "认识", "掌握", "理解", "对比")
_EXECUTABLE_MARKERS = (
    "打开",
    "点击",
    "选择",
    "输入",
    "复制",
    "粘贴",
    "运行",
    "执行",
    "安装",
    "下载",
    "上传",
    "保存",
    "创建",
    "修改",
    "配置",
    "连接",
    "访问",
    "拖动",
    "切换",
    "确认",
    "提交",
    "启动",
    "关闭",
    "添加",
    "移除",
)


def parse_generated_note(raw_text: str) -> AnyGeneratedNote:
    """Parse one provider response as a supported versioned contract."""
    return GENERATED_NOTE_ADAPTER.validate_json(raw_text)


def validate_generated_note(
    note: AnyGeneratedNote,
    content_pack: ContentPack,
    *,
    requested_note_type: ConcreteNoteType | None = None,
    require_classification_evidence: bool = False,
) -> list[str]:
    """Return stable semantic errors without mutating either input."""
    if isinstance(note, GeneratedNote):
        return _validate_identity_and_evidence(note, content_pack)
    errors = _validate_identity_and_evidence(note, content_pack)
    if requested_note_type is not None and note.note_type != requested_note_type:
        errors.append("generated note type does not match requested note type")
    if require_classification_evidence and not note.classification_evidence_ids:
        errors.append("automatic note classification requires evidence")
    errors.extend(_validate_v3_evidence_lists(note, content_pack))
    errors.extend(_validate_v3_urls(note, content_pack))
    errors.extend(_validate_practical_actions(note, content_pack))
    return errors


def iter_factual_statements(note: AnyGeneratedNote) -> Iterator[NoteStatement]:
    """Yield every evidence-backed factual statement in contract order."""
    for _, statement in iter_factual_statement_entries(note):
        yield statement


def iter_factual_statement_entries(
    note: AnyGeneratedNote,
) -> Iterator[tuple[str, NoteStatement]]:
    """Yield factual statements together with their stable JSON Pointer paths."""
    if isinstance(note, GeneratedNote):
        yield "/audience", note.audience
        yield "/summary", note.summary
        for index, statement in enumerate(note.key_points):
            yield f"/key_points/{index}", statement
        for index, step in enumerate(note.steps):
            yield (
                f"/steps/{index}",
                NoteStatement(text=step.text, evidence_ids=step.evidence_ids),
            )
        for index, statement in enumerate(note.cautions):
            yield f"/cautions/{index}", statement
        return

    yield "/summary", note.summary
    if isinstance(note, ConceptExplanationNote):
        if note.background is not None:
            yield "/background", note.background
        for index, concept in enumerate(note.concepts):
            yield f"/concepts/{index}/explanation", concept.explanation
        for index, statement in enumerate(note.relationships):
            yield f"/relationships/{index}", statement
        for index, statement in enumerate(note.misconceptions):
            yield f"/misconceptions/{index}", statement
        if note.review is not None:
            yield "/review", note.review
        return

    if isinstance(note, ResourceShareNote):
        for index, resource in enumerate(note.resources):
            prefix = f"/resources/{index}"
            yield f"{prefix}/name", resource.name
            yield f"{prefix}/value", resource.value
            if resource.suitable_for is not None:
                yield f"{prefix}/suitable_for", resource.suitable_for
            if resource.access_or_usage is not None:
                yield f"{prefix}/access_or_usage", resource.access_or_usage
            for limitation_index, statement in enumerate(resource.limitations):
                yield f"{prefix}/limitations/{limitation_index}", statement
        for index, statement in enumerate(note.reminders):
            yield f"/reminders/{index}", statement
        return

    yield "/goal", note.goal
    for index, statement in enumerate(note.prerequisites):
        yield f"/prerequisites/{index}", statement
    for index, step in enumerate(note.steps):
        prefix = f"/steps/{index}"
        yield f"{prefix}/action", step.action
        if step.expected_result is not None:
            yield f"{prefix}/expected_result", step.expected_result
    for index, item in enumerate(note.troubleshooting):
        prefix = f"/troubleshooting/{index}"
        yield f"{prefix}/symptom", item.symptom
        yield f"{prefix}/resolution", item.resolution
    for index, statement in enumerate(note.completion_checks):
        yield f"/completion_checks/{index}", statement
    for index, statement in enumerate(note.cautions):
        yield f"/cautions/{index}", statement


def referenced_evidence_ids(note: AnyGeneratedNote) -> list[str]:
    """Return referenced evidence IDs once, preserving contract order."""
    ordered: list[str] = []
    candidates: list[str] = []
    if not isinstance(note, GeneratedNote):
        candidates.extend(note.classification_evidence_ids)
    for statement in iter_factual_statements(note):
        candidates.extend(statement.evidence_ids)
    if isinstance(note, ResourceShareNote):
        for resource in note.resources:
            if resource.locator is not None:
                candidates.extend(resource.locator.evidence_ids)
    for evidence_id in candidates:
        if evidence_id not in ordered:
            ordered.append(evidence_id)
    return ordered


def _iter_v3_non_locator_text(
    note: ConceptExplanationNote | ResourceShareNote | PracticalTutorialNote,
) -> Iterator[str]:
    yield note.title
    for statement in iter_factual_statements(note):
        yield statement.text
    if isinstance(note, ConceptExplanationNote):
        for concept in note.concepts:
            yield concept.title
    elif isinstance(note, PracticalTutorialNote):
        for step in note.steps:
            yield step.title
    for supplement in note.ai_supplements:
        yield supplement.text


def _validate_identity_and_evidence(
    note: AnyGeneratedNote, content_pack: ContentPack
) -> list[str]:
    errors: list[str] = []
    if note.task_id != content_pack.task_id:
        errors.append("generated note task_id does not match content pack")
    if note.source_fingerprint != content_pack.source_fingerprint:
        errors.append("generated note source_fingerprint does not match content pack")

    evidence_by_id = {item.id: item for item in content_pack.evidence}
    seen_unknown: set[str] = set()
    seen_ai_supplement: set[str] = set()
    for evidence_id in referenced_evidence_ids(note):
        if evidence_id not in evidence_by_id and evidence_id not in seen_unknown:
            errors.append(
                f"generated note references unknown evidence id: {evidence_id}"
            )
            seen_unknown.add(evidence_id)
        elif (
            evidence_id in evidence_by_id
            and evidence_by_id[evidence_id].kind == "ai_supplement"
            and evidence_id not in seen_ai_supplement
        ):
            errors.append(
                "generated note factual statements cannot cite AI supplement "
                f"evidence: {evidence_id}"
            )
            seen_ai_supplement.add(evidence_id)

    if isinstance(note, GeneratedNote):
        for step in note.steps:
            known_kinds = {
                evidence_by_id[evidence_id].kind
                for evidence_id in step.evidence_ids
                if evidence_id in evidence_by_id
            }
            if not known_kinds.intersection({"transcript", "frame"}):
                errors.append(
                    "generated note step "
                    f"{step.order} requires transcript or frame evidence"
                )
    return errors


def _duplicate_evidence_ids(evidence_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for evidence_id in evidence_ids:
        if evidence_id in seen and evidence_id not in duplicates:
            duplicates.append(evidence_id)
        seen.add(evidence_id)
    return duplicates


def _validate_v3_evidence_lists(
    note: ConceptExplanationNote | ResourceShareNote | PracticalTutorialNote,
    content_pack: ContentPack,
) -> list[str]:
    errors: list[str] = []

    for evidence_id in _duplicate_evidence_ids(note.classification_evidence_ids):
        errors.append(
            "generated note classification_evidence_ids contains duplicate "
            f"evidence id: {evidence_id}"
        )

    statements = list(iter_factual_statements(note))
    for index, statement in enumerate(statements, start=1):
        for evidence_id in _duplicate_evidence_ids(statement.evidence_ids):
            errors.append(
                f"generated note factual statement {index} contains duplicate "
                f"evidence id: {evidence_id}"
            )

    if isinstance(note, ResourceShareNote):
        for index, resource in enumerate(note.resources, start=1):
            if resource.locator is None:
                continue
            for evidence_id in _duplicate_evidence_ids(resource.locator.evidence_ids):
                errors.append(
                    f"generated note resource {index} locator contains duplicate "
                    f"evidence id: {evidence_id}"
                )

    evidence_by_id = {item.id: item for item in content_pack.evidence}
    for index, statement in enumerate(statements, start=1):
        statement_ids = set(statement.evidence_ids)
        for evidence_id in dict.fromkeys(statement.evidence_ids):
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None or evidence.kind != "ocr":
                continue
            frame_id = evidence.frame_id
            if frame_id is not None and frame_id not in statement_ids:
                errors.append(
                    f"generated note factual statement {index} cites OCR evidence "
                    f"{evidence_id} without parent frame {frame_id}"
                )
    return errors


def _validate_v3_urls(
    note: ConceptExplanationNote | ResourceShareNote | PracticalTutorialNote,
    content_pack: ContentPack,
) -> list[str]:
    errors: list[str] = []
    evidence_by_id = {item.id: item for item in content_pack.evidence}

    if isinstance(note, ResourceShareNote):
        for resource in note.resources:
            locator = resource.locator
            if locator is None:
                continue
            url = locator.url
            if (
                not url.lower().startswith(("http://", "https://"))
                or any(character.isspace() for character in url)
                or "<" in url
                or ">" in url
            ):
                errors.append("resource locator must be a safe http(s) URL")

            cited_evidence = [
                evidence_by_id[evidence_id]
                for evidence_id in locator.evidence_ids
                if evidence_id in evidence_by_id
            ]
            if any(
                evidence.kind not in {"transcript", "ocr"}
                for evidence in cited_evidence
            ):
                errors.append("resource locator requires transcript or OCR evidence")
            if not any(
                evidence.kind in {"transcript", "ocr"}
                and evidence.text is not None
                and url in evidence.text
                for evidence in cited_evidence
            ):
                errors.append(
                    "resource locator is not present in cited transcript or OCR evidence"
                )

    if any(_URL_TOKEN_PATTERN.search(text) for text in _iter_v3_non_locator_text(note)):
        errors.append("generated note factual text must not contain a URL")
    return errors


def _validate_practical_actions(
    note: ConceptExplanationNote | ResourceShareNote | PracticalTutorialNote,
    content_pack: ContentPack,
) -> list[str]:
    if not isinstance(note, PracticalTutorialNote):
        return []

    errors: list[str] = []
    evidence_by_id = {item.id: item for item in content_pack.evidence}
    for step in note.steps:
        known_kinds = {
            evidence_by_id[evidence_id].kind
            for evidence_id in step.action.evidence_ids
            if evidence_id in evidence_by_id
        }
        if not known_kinds.intersection({"transcript", "frame"}):
            errors.append(
                f"generated note step {step.order} requires transcript or frame evidence"
            )
        if any(verb in step.action.text for verb in _COGNITIVE_VERBS) and not any(
            marker in step.action.text for marker in _EXECUTABLE_MARKERS
        ):
            errors.append(f"practical step {step.order} is not an executable action")
    return errors
