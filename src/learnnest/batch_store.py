"""Atomic storage and compatibility loading for batch manifests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from learnnest.batch_models import BatchManifest
from learnnest.publication import atomic_replace_bytes


def write_batch_atomic(batch_dir: str | Path, manifest: BatchManifest) -> Path:
    directory = Path(batch_dir)
    validated = BatchManifest.model_validate(manifest.model_dump(mode="python"))
    payload = (
        json.dumps(
            validated.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    destination = directory / "batch.json"
    atomic_replace_bytes(destination, payload)
    return destination


def load_batch(path: str | Path) -> BatchManifest:
    batch_path = Path(path)
    if batch_path.is_dir():
        batch_path /= "batch.json"
    return parse_batch_bytes(batch_path.read_bytes())


def parse_batch_bytes(data: bytes) -> BatchManifest:
    """Parse one batch fact snapshot already read from disk."""
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("batch.json root must be an object")
    if payload.get("schema_version", "1.0") == "1.0":
        payload = _migrate_batch_1_to_2(payload)
    payload = _migrate_early_2_items(payload)
    return BatchManifest.model_validate(payload)


def _migrate_batch_1_to_2(payload: dict[str, object]) -> dict[str, object]:
    migrated = dict(payload)
    batch_id = str(migrated.get("batch_id", ""))
    timestamp = _timestamp_from_batch_id(batch_id)
    raw_results = migrated.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("legacy batch results must be a list")
    results: list[dict[str, object]] = []
    for position, raw in enumerate(raw_results, start=1):
        if not isinstance(raw, dict):
            raise ValueError("legacy batch item must be an object")
        item = dict(raw)
        item["position"] = position
        item.setdefault("attempt_id", None)
        item.setdefault("failure", None)
        results.append(item)
    has_failure = any(item.get("status") == "failed" for item in results)
    migrated.update(
        {
            "schema_version": "2.0",
            "kind": "manual",
            "status": "partial" if has_failure else "completed",
            "created_at": timestamp.isoformat(),
            "updated_at": timestamp.isoformat(),
            "finished_at": timestamp.isoformat(),
            "schedule_id": None,
            "cursor_before": None,
            "cursor_after": None,
            "results": results,
        }
    )
    return migrated


def _timestamp_from_batch_id(batch_id: str) -> datetime:
    try:
        parsed = datetime.strptime(batch_id[:16], "%Y%m%dT%H%M%SZ")
    except ValueError as error:
        raise ValueError("legacy batch_id has no UTC timestamp") from error
    return parsed.replace(tzinfo=UTC)


def _migrate_early_2_items(payload: dict[str, object]) -> dict[str, object]:
    migrated = dict(payload)
    raw_results = migrated.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("batch results must be a list")
    results: list[dict[str, object]] = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise ValueError("batch item must be an object")
        item = dict(raw)
        if "source" not in item:
            item["source"] = {
                "input": item.get("input"),
                "input_type": item.get("input_type"),
                "title": item.pop("title", None),
                "content_type": item.pop("content_type", None),
                "tags": item.pop("tags", []),
            }
        results.append(item)
    migrated["results"] = results
    return migrated
