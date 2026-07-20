from __future__ import annotations

import threading
import time
from pathlib import Path


def test_queue_runner_uses_requested_workers_and_keeps_paid_failures_explicit(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.queue_runner as queue_module
    from learnnest.pipeline import PipelineError

    rows = [
        {"task_id": "task-1"},
        {"task_id": "task-2"},
        {"task_id": "task-paid"},
    ]
    monkeypatch.setattr(
        queue_module, "query_failure_queue", lambda *args, **kwargs: rows
    )
    rebuilds: list[Path] = []
    monkeypatch.setattr(
        queue_module,
        "rebuild_index",
        lambda root: rebuilds.append(Path(root)),
        raising=False,
    )
    monkeypatch.setattr(
        queue_module,
        "find_task_by_id",
        lambda root, task_id: (root / task_id, object()),
    )
    guard = threading.Lock()
    active = 0
    maximum = 0

    def recoverer(task_dir, **kwargs):
        nonlocal active, maximum
        assert kwargs["reason"] == "retry"
        if task_dir.name == "task-paid":
            raise PipelineError(
                "recovery reached a paid stage; explicit command required"
            )
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return object()

    results = queue_module.run_failure_queue(
        tmp_path,
        workers=2,
        max_items=3,
        recoverer=recoverer,
    )

    assert maximum == 2
    assert rebuilds == [tmp_path.resolve(), tmp_path.resolve()]
    assert [item.status for item in results] == ["completed", "completed", "failed"]
    assert "paid stage" in str(results[2].error)


def test_queue_runner_respects_max_items_before_starting_work(
    monkeypatch, tmp_path: Path
) -> None:
    import learnnest.queue_runner as queue_module

    monkeypatch.setattr(
        queue_module,
        "query_failure_queue",
        lambda *args, **kwargs: [{"task_id": f"task-{index}"} for index in range(5)],
    )
    monkeypatch.setattr(
        queue_module,
        "find_task_by_id",
        lambda root, task_id: (root / task_id, object()),
    )
    calls: list[str] = []

    results = queue_module.run_failure_queue(
        tmp_path,
        workers=2,
        max_items=2,
        recoverer=lambda task_dir, **kwargs: calls.append(task_dir.name),
    )

    assert len(results) == 2
    assert sorted(calls) == ["task-0", "task-1"]
