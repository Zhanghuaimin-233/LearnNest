from __future__ import annotations

import os
from pathlib import Path

import pytest

from learnnest.util import format_timestamp, safe_title, source_fingerprint


def test_source_fingerprint_is_stable_for_unchanged_file(tmp_path: Path) -> None:
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video-bytes")

    assert source_fingerprint(source) == source_fingerprint(source.resolve())


def test_source_fingerprint_changes_when_file_metadata_changes(tmp_path: Path) -> None:
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"video-bytes")
    original = source_fingerprint(source)
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    assert source_fingerprint(source) != original


def test_url_source_fingerprint_preserves_query_order_in_identity() -> None:
    from learnnest.util import url_source_fingerprint

    assert url_source_fingerprint("https://example.com/watch?b=2&a=1") == (
        url_source_fingerprint("https://example.com/watch?b=2&a=1")
    )
    assert url_source_fingerprint("https://example.com/watch?b=2&a=1") != (
        url_source_fingerprint("https://example.com/watch?a=1&b=2")
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (" lesson: one? ", "lesson one"),
        ("CON", "CON_"),
        ("CON.txt", "CON_.txt"),
        ("NUL.md", "NUL_.md"),
        ("COM1.ext", "COM1_.ext"),
        ("LPT1.ext", "LPT1_.ext"),
        ("...", "untitled"),
        ('a/b\\c* d|e"f<g>h', "a b c d e f g h"),
    ],
)
def test_safe_title_removes_windows_unsafe_filename_characters(
    name: str, expected: str
) -> None:
    assert safe_title(name) == expected


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (0, "00:00:00"),
        (62_999, "00:01:02"),
        (3_723_004, "01:02:03"),
    ],
)
def test_format_timestamp_renders_whole_milliseconds_as_hh_mm_ss(
    milliseconds: int, expected: str
) -> None:
    assert format_timestamp(milliseconds) == expected


def test_format_timestamp_rejects_negative_milliseconds() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        format_timestamp(-1)
