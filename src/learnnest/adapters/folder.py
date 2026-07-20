"""Deterministic incremental discovery for local video folders."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from learnnest.adapters.base import ScanDiscovery
from learnnest.source_models import SourceItem
from learnnest.sources import VIDEO_EXTENSIONS

_CURSOR_VERSION = "folder-v1"


class FolderAdapter:
    """Discover supported local videos using a full directory snapshot cursor."""

    def __init__(self, path: str | Path, *, recursive: bool = False) -> None:
        self.path = Path(path).resolve()
        self.recursive = recursive

    @property
    def root_id(self) -> str:
        normalized = self.path.as_posix().casefold()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @property
    def scan_key(self) -> str:
        identity = f"{self.root_id}\0{int(self.recursive)}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return f"folder-{digest}"

    def discover(self, cursor: str | None) -> ScanDiscovery:
        previous = _parse_cursor(cursor, self.root_id, self.recursive)
        snapshot = self._snapshot()
        current = {relative: signature for _, relative, signature in snapshot}
        new_sources = [
            source
            for source, relative, signature in snapshot
            if previous.get(relative) != signature
        ]
        return ScanDiscovery(
            items=new_sources,
            cursor_after=_render_cursor(
                current,
                root_id=self.root_id,
                recursive=self.recursive,
            ),
        )

    def _snapshot(self) -> list[tuple[SourceItem, str, dict[str, int]]]:
        if not self.path.is_dir():
            raise ValueError(f"scan folder is not a directory: {self.path}")
        iterator = self.path.rglob("*") if self.recursive else self.path.iterdir()
        videos = sorted(
            (
                candidate.resolve()
                for candidate in iterator
                if candidate.is_file() and candidate.suffix.lower() in VIDEO_EXTENSIONS
            ),
            key=lambda candidate: candidate.as_posix().casefold(),
        )
        snapshot: list[tuple[SourceItem, str, dict[str, int]]] = []
        for video in videos:
            stat = video.stat()
            snapshot.append(
                (
                    SourceItem(input=str(video), input_type="local_file"),
                    video.relative_to(self.path).as_posix().casefold(),
                    {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
                )
            )
        return snapshot


def _parse_cursor(
    cursor: str | None,
    root_id: str,
    recursive: bool,
) -> dict[str, dict[str, int]]:
    if cursor is None:
        return {}
    try:
        payload = json.loads(cursor)
    except json.JSONDecodeError as error:
        raise ValueError("folder scan cursor is not valid JSON") from error
    if not isinstance(payload, dict) or payload.get("version") != _CURSOR_VERSION:
        raise ValueError("unsupported folder scan cursor version")
    if payload.get("root_id") != root_id:
        raise ValueError("folder scan cursor root does not match adapter root")
    if payload.get("recursive") is not recursive:
        raise ValueError("folder scan cursor recursive mode does not match adapter")
    entries = payload.get("entries")
    if not isinstance(entries, dict) or any(
        not _valid_entry(path, signature) for path, signature in entries.items()
    ):
        raise ValueError("folder scan cursor entries contain an invalid signature")
    return entries


def _valid_entry(path: object, signature: object) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and isinstance(signature, dict)
        and set(signature) == {"size", "mtime_ns"}
        and type(signature["size"]) is int
        and signature["size"] >= 0
        and type(signature["mtime_ns"]) is int
        and signature["mtime_ns"] >= 0
    )


def _render_cursor(
    entries: dict[str, dict[str, int]],
    *,
    root_id: str,
    recursive: bool,
) -> str:
    return json.dumps(
        {
            "version": _CURSOR_VERSION,
            "root_id": root_id,
            "recursive": recursive,
            "entries": entries,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
