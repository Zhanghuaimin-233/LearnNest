"""Explicit, compatibility-preserving artifact layout migration."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LayoutMove:
    source: Path
    destination: Path


_LAYOUT_TARGETS = (
    ("视频学习笔记", Path("交付物") / "笔记"),
    ("视频学习音频", Path("交付物") / "音频"),
    ("视频学习素材", Path("运行审计") / "任务"),
    ("视频学习批次", Path("运行审计") / "批次"),
)


def plan_artifact_layout(output_root: str | Path) -> list[LayoutMove]:
    """Return only existing legacy roots that an explicit migration would move."""
    root = Path(output_root).resolve()
    moves: list[LayoutMove] = []
    for legacy_name, target_relative in _LAYOUT_TARGETS:
        source = root / legacy_name
        destination = root / target_relative
        if source.exists() or _is_directory_alias(source):
            moves.append(LayoutMove(source, destination))
    return moves


def migrate_artifact_layout(output_root: str | Path) -> list[LayoutMove]:
    """Move legacy roots once and leave directory aliases at their old paths.

    Existing task-relative artifact paths and Obsidian links keep resolving through
    the aliases; no delivery or audit file is copied. The operation is explicit
    because it changes the physical layout of a user-owned vault.
    """
    root = Path(output_root).resolve()
    if not root.is_dir():
        raise ValueError(f"output root is not a directory: {root}")
    moves = plan_artifact_layout(root)
    if not moves:
        return []
    for move in moves:
        if _is_directory_alias(move.source):
            raise ValueError(f"legacy artifact root is already an alias: {move.source}")
        if move.destination.exists() or _is_directory_alias(move.destination):
            raise ValueError(
                f"migration destination already exists: {move.destination}"
            )

    moved: list[LayoutMove] = []
    aliases: list[Path] = []
    try:
        for move in moves:
            move.destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(move.source, move.destination)
            moved.append(move)
            _create_directory_alias(move.destination, move.source)
            aliases.append(move.source)
    except OSError as error:
        for alias in reversed(aliases):
            _remove_directory_alias(alias)
        for move in reversed(moved):
            if move.destination.exists() and not move.source.exists():
                os.replace(move.destination, move.source)
        raise RuntimeError(
            "artifact layout migration could not create compatibility aliases; "
            "no files were copied"
        ) from error
    return moves


def _is_directory_alias(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _create_directory_alias(target: Path, alias: Path) -> None:
    if os.name != "nt":
        os.symlink(target, alias, target_is_directory=True)
        return
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (
            result.stderr.decode(errors="replace").strip()
            or result.stdout.decode(errors="replace").strip()
            or "unknown error"
        )
        raise OSError(f"could not create Windows directory junction: {detail}")


def _remove_directory_alias(path: Path) -> None:
    if _is_directory_alias(path):
        path.rmdir()
