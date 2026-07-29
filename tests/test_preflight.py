from __future__ import annotations

from datetime import date
from pathlib import Path


def _video(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return path


def test_preflight_does_not_create_a_missing_output_root(tmp_path: Path) -> None:
    from learnnest.preflight import preflight_sources
    from learnnest.sources import parse_source

    output_root = tmp_path / "missing-vault"
    source = parse_source(str(_video(tmp_path / "lesson.mp4")))
    calls: list[str] = []

    def find_command(name: str) -> str | None:
        calls.append(name)
        return f"C:/tools/{name}.exe"

    result = preflight_sources(
        [source],
        output_root,
        command_finder=find_command,
        today=date(2026, 7, 11),
    )

    assert result.ok is False
    assert any(issue.code == "output_root_missing" for issue in result.issues)
    assert calls == ["ffmpeg", "ffprobe"]
    assert not output_root.exists()


def test_preflight_checks_ytdlp_only_when_a_url_is_present(tmp_path: Path) -> None:
    from learnnest.preflight import preflight_sources
    from learnnest.sources import parse_source

    calls: list[str] = []
    result = preflight_sources(
        [parse_source("https://example.com/watch?v=1")],
        tmp_path,
        command_finder=lambda name: calls.append(name) or None,
        writable_checker=lambda path: True,
        today=date(2026, 7, 11),
    )

    assert calls == ["ffmpeg", "ffprobe", "yt-dlp"]
    assert {issue.code for issue in result.issues} == {
        "command_missing_ffmpeg",
        "command_missing_ffprobe",
        "command_missing_yt_dlp",
    }


def test_preflight_marks_batch_duplicates_without_creating_files(
    tmp_path: Path,
) -> None:
    from learnnest.preflight import preflight_sources
    from learnnest.sources import parse_source

    source = parse_source("https://example.com/watch?v=1")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    result = preflight_sources(
        [source, source],
        tmp_path,
        command_finder=lambda name: f"C:/tools/{name}.exe",
        writable_checker=lambda path: True,
        today=date(2026, 7, 11),
    )

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert result.items[0].duplicate is False
    assert result.items[1].duplicate is True
    assert result.items[0].task_id == result.items[1].task_id
    assert any(issue.code == "duplicate_in_batch" for issue in result.issues)
    assert before == after


def test_preflight_plans_stable_local_and_url_directories(tmp_path: Path) -> None:
    from learnnest.preflight import preflight_sources
    from learnnest.sources import parse_source

    local = parse_source(str(_video(tmp_path / "课程 A.mp4")))
    url = parse_source("https://example.com/watch?v=1")

    result = preflight_sources(
        [local, url],
        tmp_path,
        command_finder=lambda name: f"C:/tools/{name}.exe",
        writable_checker=lambda path: True,
        today=date(2026, 7, 11),
    )

    assert result.ok is True
    assert result.items[0].planned_directory.startswith("课程 A--")
    assert result.items[1].planned_directory.startswith("url-")
    assert all(item.task_id.startswith("20260711-") for item in result.items)


def test_preflight_limits_long_task_directory_names(tmp_path: Path) -> None:
    from learnnest.preflight import preflight_sources
    from learnnest.source_models import SourceItem

    source = SourceItem(
        input="https://example.com/watch?v=long-title",
        input_type="url",
        title="标题" * 100,
    )
    result = preflight_sources(
        [source],
        tmp_path,
        command_finder=lambda name: f"C:/tools/{name}.exe",
        writable_checker=lambda path: True,
        today=date(2026, 7, 11),
    )

    directory = result.items[0].planned_directory
    assert len(directory) <= 50
    assert directory.endswith(f"--{result.items[0].task_id[-8:]}")


def test_runtime_preflight_reports_missing_credentials_without_echoing_values(
    tmp_path: Path,
) -> None:
    from learnnest.preflight import preflight_runtime

    result = preflight_runtime(
        tmp_path,
        require_douyin=True,
        require_note=True,
        require_podcast=True,
        require_tts=True,
        runtime_environ={},
        command_finder=lambda name: f"C:/tools/{name}.exe",
        writable_checker=lambda path: True,
    )

    assert result.ok is False
    assert {issue.code for issue in result.issues} == {
        "runtime_missing_douyin_cookie",
        "runtime_missing_note_provider",
        "runtime_missing_mimo_api_key",
    }


def test_runtime_preflight_accepts_encrypted_douyin_cookie(
    tmp_path: Path,
) -> None:
    from learnnest.preflight import preflight_runtime

    result = preflight_runtime(
        tmp_path,
        require_douyin=True,
        runtime_environ={},
        douyin_cookie_available=True,
        command_finder=lambda name: f"C:/tools/{name}.exe",
        writable_checker=lambda path: True,
    )

    assert result.ok is True
    assert "runtime_missing_douyin_cookie" not in {
        issue.code for issue in result.issues
    }


def test_runtime_preflight_accepts_a_complete_generic_note_provider(
    tmp_path: Path,
) -> None:
    from learnnest.preflight import preflight_runtime

    result = preflight_runtime(
        tmp_path,
        require_note=True,
        runtime_environ={
            "LEARNNEST_NOTE_API_KEY": "not-printed",
            "LEARNNEST_NOTE_BASE_URL": "https://example.invalid/v1",
            "LEARNNEST_NOTE_MODEL": "example-model",
        },
        command_finder=lambda name: f"C:/tools/{name}.exe",
        writable_checker=lambda path: True,
    )

    assert result.ok is True
