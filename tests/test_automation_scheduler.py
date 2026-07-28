from datetime import UTC, datetime
from pathlib import Path

from learnnest.automation_scheduler import (
    SchedulerCommandResult,
    install,
    status,
    task_name,
    uninstall,
)


def test_task_scheduler_install_uses_only_absolute_secret_free_arguments(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "learnnest.exe"
    executable.write_bytes(b"stub")
    commands: list[list[str]] = []

    installation = install(
        tmp_path / "vault",
        policy_sha256="a" * 64,
        interval_minutes=15,
        executable=executable,
        runner=lambda command: commands.append(command) or SchedulerCommandResult(0),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert installation.task_name == task_name(tmp_path / "vault")
    assert commands[0][:2] == ["schtasks.exe", "/Create"]
    action = commands[0][commands[0].index("/TR") + 1]
    assert "automation tick" in action
    assert "API_KEY" not in action and "COOKIE" not in action
    assert str((tmp_path / "vault").resolve()) in action


def test_task_scheduler_status_and_uninstall_use_recorded_identity(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "learnnest.exe"
    executable.write_bytes(b"stub")
    commands: list[list[str]] = []
    root = tmp_path / "vault"
    install(
        root,
        policy_sha256="b" * 64,
        interval_minutes=5,
        executable=executable,
        runner=lambda command: SchedulerCommandResult(0),
    )

    installation, exists = status(
        root,
        runner=lambda command: commands.append(command) or SchedulerCommandResult(0),
    )
    removed = uninstall(
        root,
        runner=lambda command: commands.append(command) or SchedulerCommandResult(0),
    )

    assert installation is not None and exists
    assert removed
    assert commands[0] == ["schtasks.exe", "/Query", "/TN", task_name(root)]
    assert commands[1] == ["schtasks.exe", "/Delete", "/TN", task_name(root), "/F"]
