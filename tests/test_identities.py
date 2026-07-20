from __future__ import annotations

import hashlib
import os
from pathlib import Path

from learnnest.execution_models import SourceIdentities
from learnnest.identities import (
    identity_keys,
    normalize_local_source,
    source_identities,
    stream_sha256,
)
from learnnest.source_models import SourceItem


def test_stream_sha256_uses_file_bytes_not_name_size_or_title(tmp_path: Path) -> None:
    first = tmp_path / "first" / "lesson.mp4"
    second = tmp_path / "second" / "lesson.mp4"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"abc123")
    second.write_bytes(b"xyz789")

    assert first.stat().st_size == second.stat().st_size
    assert stream_sha256(first) == hashlib.sha256(b"abc123").hexdigest()
    assert stream_sha256(first) != stream_sha256(second)


def test_normalize_local_source_uses_normcase_of_resolved_path(tmp_path: Path) -> None:
    source = tmp_path / "Lesson.MP4"
    source.write_bytes(b"video")

    assert normalize_local_source(source) == os.path.normcase(str(source.resolve()))


def test_url_identity_reuses_existing_normalization_without_dropping_query() -> None:
    identities = source_identities(
        SourceItem(
            input="HTTPS://EXAMPLE.COM:443/watch?b=2&a=1#fragment",
            input_type="url",
        )
    )

    assert identities.normalized_source == "https://example.com/watch?b=2&a=1"


def test_identity_priority_is_content_then_platform_then_source_then_legacy() -> None:
    identities = SourceIdentities(
        normalized_source="https://example.com/watch?v=1",
        platform="yt-dlp",
        platform_id="video-1",
        content_sha256="a" * 64,
    )

    assert identity_keys(identities, "legacy-fingerprint") == (
        ("content_sha256", "a" * 64),
        ("platform_id", "yt-dlp\0video-1"),
        ("normalized_source", "https://example.com/watch?v=1"),
        ("source_fingerprint", "legacy-fingerprint"),
    )
