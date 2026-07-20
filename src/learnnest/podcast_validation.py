"""Semantic validation for PodcastScript 1.0."""

from __future__ import annotations

from pydantic import ValidationError

from learnnest.models import ContentPack
from learnnest.podcast_models import PodcastScript


def parse_podcast_script(raw_text: str) -> PodcastScript:
    return PodcastScript.model_validate_json(raw_text)


def validate_podcast_script(
    script: PodcastScript,
    content_pack: ContentPack,
    active_note_sha256: str,
) -> list[str]:
    errors: list[str] = []
    if script.task_id != content_pack.task_id:
        errors.append("podcast task_id does not match content pack")
    if script.source_fingerprint != content_pack.source_fingerprint:
        errors.append("podcast source_fingerprint does not match content pack")
    if script.note_content_sha256 != active_note_sha256:
        errors.append("podcast note_content_sha256 does not match active note")

    evidence_by_id = {item.id: item for item in content_pack.evidence}
    seen_unknown: set[str] = set()
    seen_ai: set[str] = set()
    for segment in script.segments:
        for evidence_id in segment.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None and evidence_id not in seen_unknown:
                errors.append(f"podcast references unknown evidence id: {evidence_id}")
                seen_unknown.add(evidence_id)
            elif (
                evidence is not None
                and evidence.kind == "ai_supplement"
                and evidence_id not in seen_ai
            ):
                errors.append(
                    "podcast segments cannot cite AI supplement evidence: "
                    f"{evidence_id}"
                )
                seen_ai.add(evidence_id)
    return errors


def safe_podcast_schema_errors(error: ValidationError) -> tuple[str, ...]:
    messages: list[str] = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "root"
        messages.append(f"{location}: {item['msg']}")
    return tuple(messages or ["podcast JSON failed schema validation"])
