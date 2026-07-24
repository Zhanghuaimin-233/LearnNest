from __future__ import annotations

from pathlib import Path


def test_runtime_environment_loads_only_known_local_values(tmp_path: Path) -> None:
    from learnnest.runtime_config import load_runtime_environment

    (tmp_path / ".env").write_text(
        "MIMO_API_KEY=from-file\nUNRELATED_SECRET=must-not-be-loaded\n",
        encoding="utf-8",
    )

    values = load_runtime_environment(tmp_path, environ={})

    assert values == {"MIMO_API_KEY": "from-file"}


def test_runtime_environment_prefers_process_values_and_accepts_raw_cookie(
    tmp_path: Path,
) -> None:
    from learnnest.runtime_config import load_runtime_environment

    (tmp_path / ".env").write_text("sessionid=local-cookie", encoding="utf-8")

    values = load_runtime_environment(
        tmp_path,
        environ={"DOUYIN_COOKIE": "from-process", "MIMO_API_KEY": "process-key"},
    )

    assert values == {
        "DOUYIN_COOKIE": "from-process",
        "MIMO_API_KEY": "process-key",
    }


def test_runtime_environment_allows_coding_plan_secret_reference(
    tmp_path: Path,
) -> None:
    from learnnest.runtime_config import load_runtime_environment

    (tmp_path / ".env").write_text("CODING_PLAN_KEY=local-key\n", encoding="utf-8")

    assert load_runtime_environment(tmp_path, environ={}) == {
        "CODING_PLAN_KEY": "local-key"
    }


def test_runtime_environment_reads_a_raw_cookie_file(tmp_path: Path) -> None:
    from learnnest.runtime_config import load_runtime_environment

    (tmp_path / ".env").write_text("sessionid=local-cookie", encoding="utf-8")

    assert load_runtime_environment(tmp_path, environ={}) == {
        "DOUYIN_COOKIE": "sessionid=local-cookie"
    }
