from __future__ import annotations

from pathlib import Path

import pytest


def test_provider_path_budget_rejects_the_podcast_path_before_bundle_creation() -> None:
    from learnnest.path_budget import ArtifactPathBudgetError, assert_stage_path_budget

    long_root = Path("C:/") / ("vault" * 40)

    with pytest.raises(ArtifactPathBudgetError, match="podcast_script"):
        assert_stage_path_budget(
            long_root / "视频学习素材" / "lesson--a1b2c3d4",
            long_root,
            stage="podcast_script",
            task_title="lesson",
            task_id="20260720-a1b2c3d4",
        )


def test_provider_path_budget_allows_a_path_saved_by_short_hidden_bundle_staging() -> (
    None
):
    from learnnest.path_budget import assert_stage_path_budget

    root = Path("C:/") / ("v" * 139)

    assert_stage_path_budget(
        root / "视频学习素材" / "lesson--a1b2c3d4",
        root,
        stage="podcast_script",
        task_title="lesson",
        task_id="20260720-a1b2c3d4",
    )
