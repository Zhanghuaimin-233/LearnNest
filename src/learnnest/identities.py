"""Layered, stable identities for logical sources and acquired media."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from learnnest.execution_models import SourceIdentities
from learnnest.source_models import SourceItem
from learnnest.sources import parse_source

IdentityKey = tuple[str, str]


def stream_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a media SHA-256 without loading the whole file into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_local_source(path: str | Path) -> str:
    """Normalize one local source using the Windows-compatible path identity rule."""
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))


def source_identities(
    source: SourceItem,
    *,
    content_sha256: str | None = None,
    platform: str | None = None,
    platform_id: str | None = None,
) -> SourceIdentities:
    """Build the stable identities known at the current acquisition boundary."""
    normalized_source = (
        normalize_local_source(source.input)
        if source.input_type == "local_file"
        else parse_source(source.input).input
    )
    return SourceIdentities(
        normalized_source=normalized_source,
        platform=platform,
        platform_id=platform_id,
        content_sha256=content_sha256,
    )


def identity_keys(
    identities: SourceIdentities,
    source_fingerprint: str,
) -> tuple[IdentityKey, ...]:
    """Return identity keys in the required strongest-to-legacy order."""
    keys: list[IdentityKey] = []
    if identities.content_sha256 is not None:
        keys.append(("content_sha256", identities.content_sha256))
    if identities.platform is not None and identities.platform_id is not None:
        keys.append(("platform_id", f"{identities.platform}\0{identities.platform_id}"))
    keys.append(("normalized_source", identities.normalized_source))
    keys.append(("source_fingerprint", source_fingerprint))
    return tuple(keys)
