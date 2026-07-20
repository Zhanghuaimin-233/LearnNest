from __future__ import annotations

from learnnest.podcast_models import PodcastScript


def script() -> PodcastScript:
    return PodcastScript.model_validate(
        {
            "schema_version": "1.0",
            "task_id": "20260711-a1b2c3d4",
            "source_fingerprint": "a1b2c3d4",
            "note_content_sha256": "a" * 64,
            "title": "模型配置复习",
            "segments": [
                {
                    "order": 1,
                    "kind": "intro",
                    "text": "今天一起复习模型配置。",
                    "evidence_ids": ["tr_0001"],
                },
                {
                    "order": 2,
                    "kind": "body",
                    "text": "第一步是打开设置。",
                    "evidence_ids": ["tr_0001", "fr_0001"],
                },
                {
                    "order": 3,
                    "kind": "outro",
                    "text": "记住先确认再保存。",
                    "evidence_ids": ["tr_0002"],
                },
            ],
            "ai_supplements": [{"text": "不同版本的入口名称可能变化。"}],
        }
    )


def test_render_podcast_markdown_keeps_machine_sources_out_of_prose() -> None:
    from learnnest.rendering import render_podcast_script

    rendered = render_podcast_script(script())

    assert "# 模型配置复习" in rendered
    assert "## 开场" in rendered
    assert "## 正文" in rendered
    assert "## 收束" in rendered
    assert "来源：[tr_0001]、[fr_0001]" in rendered
    assert "## AI 补充" in rendered


def test_render_podcast_speech_contains_only_listenable_text() -> None:
    from learnnest.rendering import render_podcast_speech

    speech = render_podcast_speech(script())

    assert speech.index("今天一起复习") < speech.index("第一步") < speech.index("记住")
    assert "下面是补充说明。" in speech
    assert "tr_" not in speech
    assert "fr_" not in speech
    assert "[[" not in speech
    assert "http" not in speech
