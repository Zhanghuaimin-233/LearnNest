from __future__ import annotations

from learnnest.models import ContentPack, Evidence


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
                "text": "开场。",
                "evidence_ids": ["tr_0001"],
            },
            {
                "order": 2,
                "kind": "body",
                "text": "打开设置。",
                "evidence_ids": ["tr_0001", "fr_0001"],
            },
            {
                "order": 3,
                "kind": "outro",
                "text": "结尾。",
                "evidence_ids": ["tr_0002"],
            },
        ],
        "ai_supplements": [],
    }


def content_pack() -> ContentPack:
    return ContentPack(
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
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=500,
                artifact_path="frames/selected/fr_0001.png",
            ),
            Evidence(
                id="ai_0001",
                kind="ai_supplement",
                text="不同版本入口不同。",
                artifact_path="content_pack.json",
            ),
        ],
    )


def test_validate_podcast_accepts_matching_note_and_evidence() -> None:
    from learnnest.podcast_models import PodcastScript
    from learnnest.podcast_validation import validate_podcast_script

    script = PodcastScript.model_validate(valid_payload())

    assert validate_podcast_script(script, content_pack(), "a" * 64) == []


def test_validate_podcast_reports_identity_note_and_unknown_evidence() -> None:
    from learnnest.podcast_models import PodcastScript
    from learnnest.podcast_validation import validate_podcast_script

    payload = valid_payload()
    payload["task_id"] = "wrong"
    payload["source_fingerprint"] = "wrong"
    payload["note_content_sha256"] = "b" * 64
    payload["segments"][1]["evidence_ids"] = ["tr_9999"]
    script = PodcastScript.model_validate(payload)

    assert validate_podcast_script(script, content_pack(), "a" * 64) == [
        "podcast task_id does not match content pack",
        "podcast source_fingerprint does not match content pack",
        "podcast note_content_sha256 does not match active note",
        "podcast references unknown evidence id: tr_9999",
    ]


def test_validate_podcast_rejects_ai_supplement_as_segment_evidence() -> None:
    from learnnest.podcast_models import PodcastScript
    from learnnest.podcast_validation import validate_podcast_script

    payload = valid_payload()
    payload["segments"][1]["evidence_ids"] = ["ai_0001"]
    script = PodcastScript.model_validate(payload)

    assert validate_podcast_script(script, content_pack(), "a" * 64) == [
        "podcast segments cannot cite AI supplement evidence: ai_0001"
    ]
