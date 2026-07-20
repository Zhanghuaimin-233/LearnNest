from __future__ import annotations

from pathlib import Path


def test_artifact_layout_plan_keeps_delivery_and_audit_roots_separate(
    tmp_path: Path,
) -> None:
    from learnnest.artifact_layout import plan_artifact_layout

    for name in ("视频学习笔记", "视频学习音频", "视频学习素材", "视频学习批次"):
        (tmp_path / name).mkdir()

    moves = plan_artifact_layout(tmp_path)

    assert [
        (move.source.name, move.destination.relative_to(tmp_path)) for move in moves
    ] == [
        ("视频学习笔记", Path("交付物") / "笔记"),
        ("视频学习音频", Path("交付物") / "音频"),
        ("视频学习素材", Path("运行审计") / "任务"),
        ("视频学习批次", Path("运行审计") / "批次"),
    ]


def test_artifact_layout_migration_moves_without_copying_and_keeps_an_alias(
    tmp_path: Path,
) -> None:
    from learnnest.artifact_layout import migrate_artifact_layout

    legacy = tmp_path / "视频学习笔记"
    legacy.mkdir()
    original = legacy / "lesson.md"
    original.write_text("# lesson\n", encoding="utf-8")

    moves = migrate_artifact_layout(tmp_path)

    destination = tmp_path / "交付物" / "笔记" / "lesson.md"
    assert [move.source.name for move in moves] == ["视频学习笔记"]
    assert (tmp_path / "视频学习笔记").is_junction() or (
        tmp_path / "视频学习笔记"
    ).is_symlink()
    assert destination.read_text(encoding="utf-8") == "# lesson\n"
    assert (tmp_path / "视频学习笔记" / "lesson.md").samefile(destination)
