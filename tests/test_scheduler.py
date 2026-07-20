from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from learnnest.locks import LockUnavailable
from learnnest.scheduler import DEFAULT_RESOURCE_SLOTS, ResourceScheduler


def test_scheduler_uses_fixed_v06_default_limits(tmp_path: Path) -> None:
    scheduler = ResourceScheduler(tmp_path)

    assert DEFAULT_RESOURCE_SLOTS == {
        "network": 2,
        "ffmpeg": 2,
        "asr": 1,
        "ocr": 1,
        "llm": 1,
        "tts": 1,
    }
    with scheduler.acquire("asr") as slot:
        assert slot == 0


def test_scheduler_supports_narrow_test_overrides_without_new_config_surface(
    tmp_path: Path,
) -> None:
    scheduler = ResourceScheduler(tmp_path, slots={"asr": 2}, timeout=0)

    with scheduler.acquire("asr") as first:
        with scheduler.acquire("asr") as second:
            assert {first, second} == {0, 1}
            with pytest.raises(LockUnavailable, match="asr"):
                with scheduler.acquire("asr"):
                    pass


def test_scheduler_serializes_same_process_threads_for_single_slot(
    tmp_path: Path,
) -> None:
    scheduler = ResourceScheduler(tmp_path, timeout=2.0)
    guard = threading.Lock()
    active = 0
    maximum = 0

    def worker() -> None:
        nonlocal active, maximum
        with scheduler.acquire("asr"):
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.1)
            with guard:
                active -= 1

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: worker(), range(2)))

    assert maximum == 1
