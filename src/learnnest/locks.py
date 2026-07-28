"""Cross-process ownership locks for one local Vault."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

DEFAULT_RESOURCE_SLOTS = {
    "network": 2,
    "ffmpeg": 2,
    "asr": 1,
    "ocr": 1,
    "llm": 1,
    "tts": 1,
}
_LOCAL_RESOURCE_GUARD = threading.Lock()
_LOCAL_RESOURCE_SLOTS: dict[tuple[str, str, int], threading.BoundedSemaphore] = {}


class LockUnavailable(RuntimeError):
    """One OS-backed ownership lock could not be acquired before its timeout."""


def _lock_root(output_root: str | Path) -> Path:
    root = Path(output_root).resolve() / ".learnnest" / "locks"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def _named_lock(
    output_root: str | Path,
    category: str,
    key: str,
    *,
    timeout: float,
) -> Iterator[None]:
    directory = _lock_root(output_root) / category
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_safe_key(key)}.lock"
    lock = FileLock(path)
    try:
        lock.acquire(timeout=timeout)
    except Timeout as error:
        lock.release(force=True)
        raise LockUnavailable(f"{category} lock unavailable: {key}") from error
    try:
        yield
    finally:
        lock.release()


def task_lock(
    output_root: str | Path, task_id: str, *, timeout: float = 0.0
) -> Iterator[None]:
    """Return an exclusive task execution lock context manager."""
    return _named_lock(output_root, "tasks", task_id, timeout=timeout)


def identity_lock(
    output_root: str | Path, identity_key: str, *, timeout: float = 0.0
) -> Iterator[None]:
    """Return an exclusive source-admission lock context manager."""
    return _named_lock(output_root, "identities", identity_key, timeout=timeout)


def batch_lock(
    output_root: str | Path, batch_id: str, *, timeout: float = 0.0
) -> Iterator[None]:
    """Return an exclusive batch runner lock context manager."""
    return _named_lock(output_root, "batches", batch_id, timeout=timeout)


def schedule_lock(
    output_root: str | Path, schedule_id: str, *, timeout: float = 0.0
) -> Iterator[None]:
    """Return an exclusive schedule cursor/active-batch lock context manager."""
    return _named_lock(output_root, "schedules", schedule_id, timeout=timeout)


def discovery_lock(
    output_root: str | Path, schedule_id: str, *, timeout: float = 0.0
) -> Iterator[None]:
    """Return an exclusive lock for one schedule-owned discovery ledger."""
    return _named_lock(output_root, "discoveries", schedule_id, timeout=timeout)


def rebuild_lock(output_root: str | Path, *, timeout: float = 0.0) -> Iterator[None]:
    """Return the Vault-wide SQLite rebuild lock context manager."""
    return _named_lock(output_root, "index", "rebuild", timeout=timeout)


def automation_lock(output_root: str | Path, *, timeout: float = 0.0) -> Iterator[None]:
    """Return the Vault-wide ownership lock for a single automation tick."""
    return _named_lock(output_root, "automation", "tick", timeout=timeout)


@contextmanager
def resource_slot(
    output_root: str | Path,
    resource: str,
    *,
    timeout: float = 0.0,
    slots: int | None = None,
) -> Iterator[int]:
    """Acquire any one OS-backed slot for a bounded shared resource."""
    if resource not in DEFAULT_RESOURCE_SLOTS:
        raise ValueError(f"unknown resource: {resource}")
    selected_slots = slots if slots is not None else DEFAULT_RESOURCE_SLOTS[resource]
    if selected_slots < 1:
        raise ValueError("resource slots must be positive")
    directory = _lock_root(output_root) / "resources" / resource
    directory.mkdir(parents=True, exist_ok=True)
    locks = [FileLock(directory / f"{index}.lock") for index in range(selected_slots)]
    deadline = time.monotonic() + max(timeout, 0.0)
    local = _local_resource_slots(Path(output_root).resolve(), resource, selected_slots)
    if not local.acquire(timeout=max(timeout, 0.0)):
        raise LockUnavailable(f"resource lock unavailable: {resource}")
    try:
        while True:
            for index, lock in enumerate(locks):
                try:
                    lock.acquire(timeout=0)
                except Timeout:
                    lock.release(force=True)
                    continue
                try:
                    yield index
                finally:
                    lock.release()
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LockUnavailable(f"resource lock unavailable: {resource}")
            time.sleep(min(0.02, remaining))
    finally:
        local.release()


def _local_resource_slots(
    output_root: Path,
    resource: str,
    slots: int,
) -> threading.BoundedSemaphore:
    key = (str(output_root), resource, slots)
    with _LOCAL_RESOURCE_GUARD:
        semaphore = _LOCAL_RESOURCE_SLOTS.get(key)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(slots)
            _LOCAL_RESOURCE_SLOTS[key] = semaphore
        return semaphore
