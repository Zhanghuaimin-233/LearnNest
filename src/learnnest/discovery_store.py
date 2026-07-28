"""Atomic persistence and leasing for schedule-owned discovered links."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from learnnest.discovery_models import (
    DiscoveryManifest,
    DiscoveryRecord,
    DiscoveredLink,
    discovery_id,
)
from learnnest.execution_models import FailureInfo
from learnnest.locks import discovery_lock
from learnnest.publication import atomic_replace_bytes

_LEASE_DURATION = timedelta(minutes=30)


@dataclass(frozen=True)
class DiscoveryClaim:
    """One link exclusively leased to a download consumer."""

    schedule_id: str
    discovery_id: str
    link: DiscoveredLink
    task_id: str | None = None


def discovery_path(output_root: str | Path, schedule_id: str) -> Path:
    root = Path(output_root).resolve()
    return root / "视频学习批次" / "schedules" / "discovery" / f"{schedule_id}.json"


def load_discovery_manifest(
    output_root: str | Path,
    schedule_id: str,
) -> DiscoveryManifest | None:
    path = discovery_path(output_root, schedule_id)
    if not path.is_file():
        return None
    return DiscoveryManifest.model_validate_json(path.read_text(encoding="utf-8"))


def persist_discovery_links(
    output_root: str | Path,
    schedule_id: str,
    links: Iterable[DiscoveredLink],
    *,
    now: datetime,
) -> DiscoveryManifest:
    """Merge observed links into one schedule-owned atomic ledger."""
    _require_aware(now)
    root = Path(output_root).resolve()
    observed_links = list(links)
    with discovery_lock(root, schedule_id, timeout=0):
        current = load_discovery_manifest(root, schedule_id)
        records = (
            {record.discovery_id: record for record in current.records}
            if current
            else {}
        )
        for position, link in enumerate(observed_links):
            key = discovery_id(link)
            existing = records.get(key)
            if existing is None:
                records[key] = DiscoveryRecord(
                    schema_version="1.1",
                    discovery_id=key,
                    link=link,
                    first_seen_at=now,
                    updated_at=now,
                    observed_position=position,
                    last_observed_at=now,
                )
                continue
            records[key] = existing.model_copy(
                update={
                    "schema_version": "1.1",
                    "link": _merge_link(existing.link, link),
                    "updated_at": now,
                    "observed_position": position,
                    "last_observed_at": now,
                }
            )
        manifest = DiscoveryManifest(
            schema_version="1.1",
            schedule_id=schedule_id,
            updated_at=now,
            latest_observation_at=now,
            records=list(records.values()),
        )
        _write_manifest(root, manifest)
        return manifest


def claim_pending_discoveries(
    output_root: str | Path,
    *,
    max_items: int,
    now: datetime,
    retry_failed: bool = False,
    max_attempts: int = 4,
    schedule_id: str | None = None,
    selection: Literal["oldest", "latest_observed"] = "oldest",
) -> list[DiscoveryClaim]:
    """Lease pending links, reclaiming only expired running leases.

    The default consumes a schedule snapshot from its oldest item to newest.
    ``latest_observed`` is a one-shot test selector: it only considers the
    most recent source observation recorded for the specified schedule.
    """
    if max_items < 1:
        raise ValueError("max_items must be positive")
    if not 1 <= max_attempts <= 4:
        raise ValueError("max_attempts must be between 1 and 4")
    if selection not in {"oldest", "latest_observed"}:
        raise ValueError(f"unsupported discovery selection: {selection}")
    if selection == "latest_observed" and schedule_id is None:
        raise ValueError("latest_observed selection requires schedule_id")
    _require_aware(now)
    root = Path(output_root).resolve()
    claims: list[DiscoveryClaim] = []
    paths = _manifest_paths(root)
    if schedule_id is not None:
        paths = [path for path in paths if path.stem == schedule_id]
        if not paths:
            raise FileNotFoundError(f"discovery manifest is missing: {schedule_id}")
    for path in paths:
        current_schedule_id = path.stem
        with discovery_lock(root, current_schedule_id, timeout=0):
            manifest = load_discovery_manifest(root, current_schedule_id)
            if manifest is None:
                continue
            records = list(manifest.records)
            changed = False
            for index in _ordered_record_indices(manifest, selection):
                if len(claims) >= max_items:
                    break
                record = records[index]
                eligible = record.status == "pending"
                if record.status == "running":
                    eligible = (
                        record.lease_until is not None and record.lease_until <= now
                    )
                if retry_failed and record.status == "failed":
                    eligible = (
                        record.failure is not None
                        and record.failure.disposition == "retryable"
                    )
                if not eligible:
                    continue
                next_attempt_count = record.attempt_count + 1
                if next_attempt_count > max_attempts:
                    continue
                claimed = record.model_copy(
                    update={
                        "status": "running",
                        "lease_until": now + _LEASE_DURATION,
                        "attempt_count": next_attempt_count,
                        "updated_at": now,
                        "failure": None,
                    }
                )
                records[index] = claimed
                claims.append(
                    DiscoveryClaim(
                        current_schedule_id,
                        record.discovery_id,
                        record.link,
                        record.task_id,
                    )
                )
                changed = True
            if changed:
                _write_manifest(
                    root,
                    manifest.model_copy(update={"updated_at": now, "records": records}),
                )
        if len(claims) >= max_items:
            break
    return claims


def _ordered_record_indices(
    manifest: DiscoveryManifest,
    selection: Literal["oldest", "latest_observed"],
) -> list[int]:
    indexed_records = list(enumerate(manifest.records))
    if selection == "latest_observed":
        observed_at = manifest.latest_observation_at
        if observed_at is None:
            raise ValueError(
                "latest_observed selection requires a fresh source observation"
            )
        candidates = [
            (index, record)
            for index, record in indexed_records
            if record.last_observed_at == observed_at
            and record.observed_position is not None
        ]
        if not candidates:
            raise ValueError(
                "latest_observed selection found no ordered source observation"
            )
        return [
            index
            for index, _record in sorted(
                candidates,
                key=lambda item: (item[1].observed_position, item[0]),
            )
        ]
    return [
        index
        for index, _record in sorted(
            indexed_records,
            key=lambda item: (
                item[1].first_seen_at,
                -(item[1].observed_position or 0),
                item[0],
            ),
        )
    ]


def mark_discovery_results(
    output_root: str | Path,
    updates: Iterable[tuple[DiscoveryClaim, str, str | None, FailureInfo | None]],
    *,
    now: datetime,
) -> None:
    """Commit consumer outcomes without touching the schedule cursor."""
    _require_aware(now)
    grouped: dict[
        str, list[tuple[DiscoveryClaim, str, str | None, FailureInfo | None]]
    ] = {}
    for update in updates:
        grouped.setdefault(update[0].schedule_id, []).append(update)
    root = Path(output_root).resolve()
    for schedule_id, schedule_updates in grouped.items():
        with discovery_lock(root, schedule_id, timeout=0):
            manifest = load_discovery_manifest(root, schedule_id)
            if manifest is None:
                raise FileNotFoundError(f"discovery manifest is missing: {schedule_id}")
            by_id = {record.discovery_id: record for record in manifest.records}
            for claim, status, task_id, failure in schedule_updates:
                if status not in {"downloaded", "failed"}:
                    raise ValueError(f"unsupported discovery result status: {status}")
                current = by_id.get(claim.discovery_id)
                if current is None:
                    raise ValueError(
                        f"discovery record is missing: {claim.discovery_id}"
                    )
                by_id[claim.discovery_id] = current.model_copy(
                    update={
                        "status": status,
                        "task_id": task_id,
                        "artifact_path": None,
                        "failure": failure,
                        "lease_until": None,
                        "updated_at": now,
                    }
                )
            _write_manifest(
                root,
                manifest.model_copy(
                    update={
                        "updated_at": now,
                        "records": [
                            by_id[record.discovery_id] for record in manifest.records
                        ],
                    }
                ),
            )


def mark_discovery_artifact_result(
    output_root: str | Path,
    claim: DiscoveryClaim,
    status: str,
    artifact_path: str | None,
    failure: FailureInfo | None,
    *,
    now: datetime,
) -> None:
    """Commit a non-TaskRecord download such as an image-text asset bundle."""
    _require_aware(now)
    if status not in {"downloaded", "failed"}:
        raise ValueError(f"unsupported discovery result status: {status}")
    if status == "downloaded" and not artifact_path:
        raise ValueError("downloaded artifact results require artifact_path")
    if status == "failed" and artifact_path is not None:
        raise ValueError("failed artifact results must not hold artifact_path")
    root = Path(output_root).resolve()
    with discovery_lock(root, claim.schedule_id, timeout=0):
        manifest = load_discovery_manifest(root, claim.schedule_id)
        if manifest is None:
            raise FileNotFoundError(
                f"discovery manifest is missing: {claim.schedule_id}"
            )
        by_id = {record.discovery_id: record for record in manifest.records}
        current = by_id.get(claim.discovery_id)
        if current is None:
            raise ValueError(f"discovery record is missing: {claim.discovery_id}")
        by_id[claim.discovery_id] = current.model_copy(
            update={
                "status": status,
                "task_id": None,
                "artifact_path": artifact_path,
                "failure": failure,
                "lease_until": None,
                "updated_at": now,
            }
        )
        _write_manifest(
            root,
            manifest.model_copy(
                update={
                    "updated_at": now,
                    "records": [
                        by_id[record.discovery_id] for record in manifest.records
                    ],
                }
            ),
        )


def _manifest_paths(root: Path) -> list[Path]:
    directory = root / "视频学习批次" / "schedules" / "discovery"
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def _write_manifest(root: Path, manifest: DiscoveryManifest) -> Path:
    destination = discovery_path(root, manifest.schedule_id)
    atomic_replace_bytes(
        destination,
        manifest.model_dump_json(indent=2, ensure_ascii=False).encode("utf-8"),
    )
    return destination


def _merge_link(previous: DiscoveredLink, current: DiscoveredLink) -> DiscoveredLink:
    folder_ids = sorted(set(previous.folder_ids) | set(current.folder_ids))
    folder_names = dict(previous.folder_names)
    folder_names.update(current.folder_names)
    source = current.source if current.source.title else previous.source
    content_kind = (
        "image_text"
        if {
            previous.content_kind,
            current.content_kind,
        }
        == {"image_text", "video"}
        or current.content_kind == "image_text"
        else current.content_kind
    )
    if source.content_type != content_kind:
        source = source.model_copy(update={"content_type": content_kind})
    return DiscoveredLink(
        platform_id=previous.platform_id,
        source=source,
        content_kind=content_kind,
        folder_ids=folder_ids,
        folder_names=folder_names,
    )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("discovery time must be timezone-aware")
