from __future__ import annotations

import pytest
from pydantic import ValidationError


def valid_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "task_id": "20260711-a1b2c3d4",
        "source_fingerprint": "a1b2c3d4",
        "note_content_sha256": "a" * 64,
        "title": "设置模型参数",
        "segments": [
            {
                "order": 1,
                "kind": "intro",
                "text": "今天我们聊聊模型配置。",
                "evidence_ids": ["tr_0001"],
            },
            {
                "order": 2,
                "kind": "body",
                "text": "先打开设置，再选择模型。",
                "evidence_ids": ["tr_0001", "fr_0001"],
            },
            {
                "order": 3,
                "kind": "outro",
                "text": "这就是今天的重点。",
                "evidence_ids": ["tr_0002"],
            },
        ],
        "ai_supplements": [],
    }


def test_podcast_script_accepts_a_contiguous_listening_structure() -> None:
    from learnnest.podcast_models import PodcastScript

    script = PodcastScript.model_validate(valid_payload())

    assert [segment.kind for segment in script.segments] == [
        "intro",
        "body",
        "outro",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "打开 [[设置]]",
        "访问 https://example.com",
        "执行 `uv run`",
        "```python",
        "来源是 tr_0001",
        "第一行\n第二行",
    ],
)
def test_podcast_segment_rejects_non_speech_markup(text: str) -> None:
    from learnnest.podcast_models import PodcastScript

    payload = valid_payload()
    payload["segments"][1]["text"] = text

    with pytest.raises(ValidationError):
        PodcastScript.model_validate(payload)


def test_podcast_script_rejects_markup_in_title() -> None:
    from learnnest.podcast_models import PodcastScript

    payload = valid_payload()
    payload["title"] = "[点击查看](https://example.com)"

    with pytest.raises(ValidationError, match="title must not contain markup"):
        PodcastScript.model_validate(payload)


@pytest.mark.parametrize(
    "segments",
    [
        [
            {"order": 2, "kind": "intro", "text": "开场", "evidence_ids": ["tr_0001"]},
            {"order": 3, "kind": "body", "text": "正文", "evidence_ids": ["tr_0001"]},
            {"order": 4, "kind": "outro", "text": "结尾", "evidence_ids": ["tr_0002"]},
        ],
        [
            {"order": 1, "kind": "body", "text": "正文", "evidence_ids": ["tr_0001"]},
            {"order": 2, "kind": "outro", "text": "结尾", "evidence_ids": ["tr_0002"]},
        ],
        [
            {"order": 1, "kind": "intro", "text": "开场", "evidence_ids": ["tr_0001"]},
            {"order": 2, "kind": "outro", "text": "结尾", "evidence_ids": ["tr_0002"]},
        ],
    ],
)
def test_podcast_script_rejects_invalid_section_order(segments: list[dict]) -> None:
    from learnnest.podcast_models import PodcastScript

    payload = valid_payload()
    payload["segments"] = segments

    with pytest.raises(ValidationError):
        PodcastScript.model_validate(payload)
