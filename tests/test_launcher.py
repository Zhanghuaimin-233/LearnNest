from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import learnnest.cli as cli
from learnnest.launcher import (
    LauncherCancelled,
    default_model_root,
    load_launcher_config,
    resolve_output_root,
    save_launcher_model_root,
    save_launcher_output_root,
)


def test_first_launcher_selection_persists_only_an_absolute_output_root(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "学习资料"
    config_path = tmp_path / "local-app-data" / "LearnNest" / "launcher.json"

    root = resolve_output_root(config_path, select_directory=lambda: selected)

    assert root == selected.resolve()
    assert load_launcher_config(config_path).output_root == str(selected.resolve())
    raw = config_path.read_text(encoding="utf-8")
    assert "secret" not in raw.lower()
    assert not list(config_path.parent.glob("*.tmp"))


def test_default_model_root_is_program_owned_model_directory(tmp_path: Path) -> None:
    assert (
        default_model_root(tmp_path / "LearnNest") == tmp_path / "LearnNest" / "model"
    )


def test_launcher_model_root_is_editable_and_survives_output_root_changes(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "学习资料"
    next_output_root = tmp_path / "新学习资料"
    model_root = tmp_path / "large-models"
    config_path = tmp_path / "local-app-data" / "LearnNest" / "launcher.json"
    resolve_output_root(config_path, select_directory=lambda: output_root)

    saved = save_launcher_model_root(
        model_root,
        output_root=output_root,
        config_path=config_path,
    )
    save_launcher_output_root(next_output_root, config_path)
    config = load_launcher_config(config_path)

    assert saved == model_root.resolve()
    assert model_root.is_dir()
    assert config.output_root == str(next_output_root.resolve())
    assert config.model_root == str(model_root.resolve())


def test_first_launcher_cancel_exits_without_writing_configuration(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "local-app-data" / "LearnNest" / "launcher.json"

    with pytest.raises(LauncherCancelled):
        resolve_output_root(config_path, select_directory=lambda: None)

    assert not config_path.exists()


def test_invalid_launcher_config_can_be_reselected_without_deleting_on_cancel(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "local-app-data" / "LearnNest" / "launcher.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{not-json", encoding="utf-8")
    original = config_path.read_bytes()

    with pytest.raises(LauncherCancelled):
        resolve_output_root(config_path, select_directory=lambda: None)
    assert config_path.exists()
    assert config_path.read_bytes() == original
    selected = tmp_path / "new-root"
    root = resolve_output_root(config_path, select_directory=lambda: selected)

    assert root == selected.resolve()
    assert load_launcher_config(config_path).output_root == str(selected.resolve())


def test_web_launch_uses_saved_launcher_root_without_cli_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_launch(*, port: int) -> None:
        captured["port"] = port

    monkeypatch.setattr(cli, "launch_web_app", fake_launch)

    result = CliRunner().invoke(cli.app, ["web", "launch", "--port", "8123"])

    assert result.exit_code == 0, result.output
    assert captured == {"port": 8123}
