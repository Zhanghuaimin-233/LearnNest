"""Launch ASR and OCR workers without importing their native providers."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

from learnnest.runtime_config import load_runtime_environment


def run_worker(
    args: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one isolated provider worker and return its completed process."""

    command = [sys.executable, "-m", "learnnest.worker", *args]
    asset_environment = _asset_environment()
    merged_environment = {**asset_environment, **(environment or {})}
    options: dict[str, object] = {"capture_output": True, "text": True, "check": False}
    if merged_environment:
        options["env"] = {**os.environ, **merged_environment}
    result = subprocess.run(command, **options)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no worker output"
        raise RuntimeError(f"provider worker failed ({result.returncode}): {detail}")
    return result


def _asset_environment() -> dict[str, str]:
    """Forward only local model-cache configuration to provider workers."""
    cache = load_runtime_environment(os.getcwd()).get("HUGGINGFACE_HUB_CACHE", "")
    return {"HUGGINGFACE_HUB_CACHE": cache} if cache else {}
