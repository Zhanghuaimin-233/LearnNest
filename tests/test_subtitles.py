from __future__ import annotations

from pathlib import Path

import pytest


def test_parse_webvtt_to_existing_transcript_contract(tmp_path: Path) -> None:
    from learnnest.subtitles import parse_subtitle_file

    subtitle = tmp_path / "lesson.zh.vtt"
    subtitle.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:01.250 align:start\n"
        "<c.colorE5E5E5>打开设置</c>\n\n"
        "cue-2\n"
        "00:00:01.250 --> 00:00:03.000\n"
        "保存 &amp; 完成\n",
        encoding="utf-8",
    )

    payload = parse_subtitle_file(subtitle)

    assert payload["provider"] == "yt-dlp"
    assert payload["model"] == "platform-subtitle"
    assert payload["segments"] == [
        {
            "id": "tr_0001",
            "start_ms": 0,
            "end_ms": 1_250,
            "text": "打开设置",
            "confidence": None,
        },
        {
            "id": "tr_0002",
            "start_ms": 1_250,
            "end_ms": 3_000,
            "text": "保存 & 完成",
            "confidence": None,
        },
    ]


def test_parse_srt_accepts_comma_timestamps(tmp_path: Path) -> None:
    from learnnest.subtitles import parse_subtitle_file

    subtitle = tmp_path / "lesson.srt"
    subtitle.write_text(
        "1\n00:00:00,500 --> 00:00:02,000\n第一句\n",
        encoding="utf-8",
    )

    payload = parse_subtitle_file(subtitle)

    assert payload["segments"][0]["start_ms"] == 500
    assert payload["segments"][0]["end_ms"] == 2_000


def test_parse_subtitle_rejects_empty_or_invalid_timelines(tmp_path: Path) -> None:
    from learnnest.subtitles import SubtitleError, parse_subtitle_file

    empty = tmp_path / "empty.vtt"
    empty.write_text("WEBVTT\n", encoding="utf-8")
    invalid = tmp_path / "invalid.srt"
    invalid.write_text(
        "1\n00:00:03,000 --> 00:00:01,000\n倒序\n",
        encoding="utf-8",
    )

    with pytest.raises(SubtitleError, match="no valid cues"):
        parse_subtitle_file(empty)
    with pytest.raises(SubtitleError, match="end precedes start"):
        parse_subtitle_file(invalid)
