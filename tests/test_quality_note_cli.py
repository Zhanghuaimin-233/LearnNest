from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from learnnest.cli import app


def test_quality_note_cli_exposes_explicit_plan_and_execution_commands() -> None:
    result = CliRunner().invoke(app, ["quality-note", "--help"])

    assert result.exit_code == 0
    for command in ("plan", "organize", "generate", "review", "status", "recover"):
        assert command in result.output


def test_assisted_note_cli_exposes_isolated_markdown_commands() -> None:
    result = CliRunner().invoke(app, ["assisted-note", "--help"])

    assert result.exit_code == 0
    for command in ("plan", "generate", "review", "status", "recover"):
        assert command in result.output


def test_quality_note_plan_is_dry_run_with_no_provider_configuration(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app,
        [
            "quality-note",
            "plan",
            "missing-task",
            "--output-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert "no provider connection is configured" in result.output
    assert "API key" not in result.output
