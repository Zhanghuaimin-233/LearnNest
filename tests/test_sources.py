from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError


def _video(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return path


def test_parse_source_normalizes_http_url_without_reordering_query() -> None:
    from learnnest.sources import parse_source

    source = parse_source("HTTPS://Example.COM:443/watch?b=2&a=1#chapter")

    assert source.input_type == "url"
    assert source.input == "https://example.com/watch?b=2&a=1"


def test_parse_source_preserves_ipv6_url_brackets() -> None:
    from learnnest.sources import parse_source

    source = parse_source("HTTP://[2001:4860:4860::8888]:8080/watch")

    assert source.input == "http://[2001:4860:4860::8888]:8080/watch"


def test_parse_source_resolves_local_path_against_explicit_base(tmp_path: Path) -> None:
    from learnnest.sources import parse_source

    video = _video(tmp_path / "inputs" / "lesson.mp4")

    source = parse_source("inputs/lesson.mp4", base_dir=tmp_path)

    assert source.input_type == "local_file"
    assert source.input == str(video.resolve())


def test_source_item_is_strict_and_rejects_unknown_fields() -> None:
    from learnnest.source_models import SourceItem

    with pytest.raises(ValidationError):
        SourceItem.model_validate(
            {
                "schema_version": "1.0",
                "input": "https://example.com/video",
                "input_type": "url",
                "unexpected": True,
            }
        )


def test_parse_txt_tasks_uses_the_task_file_directory(tmp_path: Path) -> None:
    from learnnest.sources import parse_tasks_file

    first = _video(tmp_path / "videos" / "a.mp4")
    tasks = tmp_path / "tasks.txt"
    tasks.write_text(
        "videos/a.mp4\nhttps://example.com/watch?v=1\n\n",
        encoding="utf-8",
    )

    sources = parse_tasks_file(tasks)

    assert [item.input_type for item in sources] == ["local_file", "url"]
    assert sources[0].input == str(first.resolve())


def test_parse_jsonl_tasks_applies_overrides_and_relative_paths(tmp_path: Path) -> None:
    from learnnest.sources import parse_tasks_file

    video = _video(tmp_path / "media" / "lesson.mp4")
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        json.dumps(
            {
                "input": "media/lesson.mp4",
                "title": "课程 A",
                "content_type": "tutorial",
                "tags": ["AI", "工具"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    sources = parse_tasks_file(tasks)

    assert sources[0].input == str(video.resolve())
    assert sources[0].title == "课程 A"
    assert sources[0].content_type == "tutorial"
    assert sources[0].tags == ["AI", "工具"]


def test_jsonl_note_type_is_normalized_and_content_type_is_not_mapped(
    tmp_path: Path,
) -> None:
    from learnnest.sources import parse_tasks_file

    video = _video(tmp_path / "lesson.mp4")
    path = tmp_path / "tasks.jsonl"
    path.write_text(
        json.dumps(
            {
                "input": str(video),
                "note_type": "concept",
                "content_type": "course",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    item = parse_tasks_file(path)[0]

    assert item.note_type == "concept_explanation"
    assert item.content_type == "course"


def test_jsonl_note_type_normalizes_whitespace_and_case(tmp_path: Path) -> None:
    from learnnest.sources import parse_tasks_file

    video = _video(tmp_path / "lesson.mp4")
    path = tmp_path / "tasks.jsonl"
    path.write_text(
        json.dumps({"input": str(video), "note_type": " RESOURCE "}) + "\n",
        encoding="utf-8",
    )

    item = parse_tasks_file(path)[0]

    assert item.note_type == "resource_share"


def test_parse_jsonl_reports_the_invalid_line_number(tmp_path: Path) -> None:
    from learnnest.sources import SourceParseError, parse_tasks_file

    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(
        '{"input":"https://example.com/a"}\nnot-json\n',
        encoding="utf-8",
    )

    with pytest.raises(SourceParseError, match=r"tasks\.jsonl:2"):
        parse_tasks_file(tasks)


def test_scan_directory_is_non_recursive_by_default_and_sorted(tmp_path: Path) -> None:
    from learnnest.sources import scan_input_directory

    second = _video(tmp_path / "b.MKV")
    first = _video(tmp_path / "A.mp4")
    nested = _video(tmp_path / "nested" / "c.webm")
    (tmp_path / "ignore.txt").write_text("not video", encoding="utf-8")

    flat = scan_input_directory(tmp_path)
    recursive = scan_input_directory(tmp_path, recursive=True)

    assert [item.input for item in flat] == [
        str(first.resolve()),
        str(second.resolve()),
    ]
    assert [item.input for item in recursive] == [
        str(first.resolve()),
        str(second.resolve()),
        str(nested.resolve()),
    ]


def test_collect_sources_requires_exactly_one_input_mode(tmp_path: Path) -> None:
    from learnnest.sources import SourceParseError, collect_sources

    video = _video(tmp_path / "lesson.mp4")
    tasks = tmp_path / "tasks.txt"
    tasks.write_text(str(video), encoding="utf-8")

    with pytest.raises(SourceParseError, match="exactly one"):
        collect_sources()
    with pytest.raises(SourceParseError, match="exactly one"):
        collect_sources(input_value=str(video), tasks_path=tasks)

    assert collect_sources(input_value=str(video))[0].input == str(video.resolve())


def test_parse_source_canonicalizes_douyin_video_links() -> None:
    from learnnest.sources import parse_source

    for raw in (
        "https://www.douyin.com/video/123/",
        "https://www.douyin.com/video/123?previous_page=app_code_link#watch",
        "https://douyin.com/video/123",
    ):
        source = parse_source(raw)
        assert source.input_type == "url"
        assert source.input == "https://www.douyin.com/video/123"


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.douyin.com/",
        "https://www.douyin.com/user/MS4wLjABAAAA",
        "https://live.douyin.com/123",
        "https://www.douyin.com/collection/456",
        "https://www.douyin.com/video/abc",
        "https://www.douyin.com/video/123?modal_id=456",
        "http://www.douyin.com/video/123",
        "https://douyin.com.evil.com/video/123",
    ],
)
def test_parse_source_rejects_disallowed_douyin_links(raw: str) -> None:
    from learnnest.sources import SourceParseError, parse_source

    with pytest.raises(SourceParseError, match=r"抖音|视频|数字|多个|https|官方域名"):
        parse_source(raw)


def test_same_douyin_video_id_reduces_to_one_fingerprint() -> None:
    from learnnest.sources import parse_source
    from learnnest.util import url_source_fingerprint

    fingerprints = {
        url_source_fingerprint(parse_source(raw).input)
        for raw in (
            "https://www.douyin.com/video/123/",
            "https://www.douyin.com/video/123?from=search#frag",
            "https://www.douyin.com/?modal_id=123",
            "https://www.douyin.com/video/123?modal_id=123",
            "https://douyin.com/video/123",
        )
    }

    assert fingerprints == {url_source_fingerprint("https://www.douyin.com/video/123")}
    assert len(fingerprints) == 1
