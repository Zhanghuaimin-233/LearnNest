"""Resource scheduling policy over cross-process Vault slots."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

from learnnest.locks import (
    DEFAULT_RESOURCE_SLOTS,
    resource_slot,
)


class ResourceScheduler:
    """Map heavy operations to fixed cross-process resource limits."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        slots: Mapping[str, int] | None = None,
        timeout: float = 30.0,
    ) -> None:
        overrides = dict(slots or {})
        unknown = set(overrides) - set(DEFAULT_RESOURCE_SLOTS)
        if unknown:
            raise ValueError(f"unknown resource: {sorted(unknown)[0]}")
        self._output_root = Path(output_root).resolve()
        self._slots = {**DEFAULT_RESOURCE_SLOTS, **overrides}
        self._timeout = timeout

    def acquire(self, resource: str) -> Iterator[int]:
        """Return a context manager for one configured resource slot."""
        if resource not in self._slots:
            raise ValueError(f"unknown resource: {resource}")
        return resource_slot(
            self._output_root,
            resource,
            timeout=self._timeout,
            slots=self._slots[resource],
        )
