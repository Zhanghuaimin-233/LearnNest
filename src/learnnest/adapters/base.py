"""Shared contracts for incremental source discovery."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Protocol

from learnnest.discovery_models import DiscoveredLink
from learnnest.source_models import SourceItem


@dataclass(frozen=True)
class ScanDiscovery:
    """New sources plus the complete candidate cursor after discovery."""

    items: list[SourceItem]
    cursor_after: str
    links: list[DiscoveredLink] = field(default_factory=list)


class SourceAdapter(Protocol):
    """A discoverable source whose scan identity is stable across processes."""

    @property
    def scan_key(self) -> str: ...

    def discover(self, cursor: str | None) -> ScanDiscovery: ...
