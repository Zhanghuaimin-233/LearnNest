"""Deterministic source projections used by the quality-first note workflow."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence, Set

from learnnest.evidence_unit_models import (
    EvidenceAtom,
    EvidenceUnit,
    EvidenceUnitShard,
    EvidenceUnitType,
    VisualRole,
)
from learnnest.models import ContentPack, Evidence

_KIND_ORDER = {"transcript": 0, "frame": 1, "ocr": 2}


def build_evidence_atoms(content_pack: ContentPack) -> list[EvidenceAtom]:
    """Project source evidence without rewriting any source field."""
    atoms = [
        _atom_from_evidence(item)
        for item in content_pack.evidence
        if item.kind != "ai_supplement"
    ]
    atoms.sort(
        key=lambda item: (
            item.start_ms if item.start_ms is not None else 0,
            _KIND_ORDER[item.kind],
            item.evidence_id,
        )
    )
    return atoms


def expand_evidence_ids(
    content_pack: ContentPack,
    raw_evidence_ids: Sequence[str],
    *,
    allowed_evidence_ids: Set[str] | None = None,
) -> list[Evidence]:
    """Resolve IDs and add each OCR item's unique parent frame exactly once."""
    evidence_by_id = content_pack.evidence_by_id()
    expanded: list[str] = []

    def append(evidence_id: str) -> None:
        if evidence_id in expanded:
            return
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            raise ValueError(f"unknown evidence id: {evidence_id}")
        if evidence.kind == "ai_supplement":
            raise ValueError("AI supplement evidence cannot be organized")
        if allowed_evidence_ids is not None and evidence_id not in allowed_evidence_ids:
            raise ValueError(f"evidence id is outside the shard: {evidence_id}")
        expanded.append(evidence_id)

    for evidence_id in raw_evidence_ids:
        append(evidence_id)
        evidence = evidence_by_id[evidence_id]
        if evidence.kind == "ocr" and evidence.frame_id is not None:
            append(evidence.frame_id)
    return [evidence_by_id[evidence_id] for evidence_id in expanded]


def build_evidence_unit(
    content_pack: ContentPack,
    *,
    unit_id: str,
    shard_id: str = "shard_0001",
    raw_evidence_ids: Sequence[str],
    unit_type: EvidenceUnitType,
    topic_labels: Sequence[str],
    outline: str,
    visual_role: VisualRole,
    visual_reason: str | None = None,
    allowed_evidence_ids: Set[str] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> EvidenceUnit:
    """Build a source-bound unit and fail closed on incomplete closure."""
    expanded = expand_evidence_ids(
        content_pack,
        raw_evidence_ids,
        allowed_evidence_ids=allowed_evidence_ids,
    )
    atoms = [_atom_from_evidence(item) for item in expanded]
    starts = [item.start_ms for item in atoms if item.start_ms is not None]
    ends = [
        item.end_ms if item.end_ms is not None else item.start_ms
        for item in atoms
        if item.start_ms is not None
    ]
    if not starts or not ends:
        raise ValueError("evidence unit has no time-bounded source evidence")
    typed_ids = {
        "transcript_ids": [
            item.evidence_id for item in atoms if item.kind == "transcript"
        ],
        "frame_ids": [item.evidence_id for item in atoms if item.kind == "frame"],
        "ocr_ids": [item.evidence_id for item in atoms if item.kind == "ocr"],
    }
    return EvidenceUnit(
        unit_id=unit_id,
        shard_id=shard_id,
        unit_type=unit_type,
        start_ms=min(starts) if start_ms is None else start_ms,
        end_ms=max(ends) if end_ms is None else end_ms,
        topic_labels=_unique_nonempty(topic_labels),
        outline=outline,
        raw_evidence_ids=_unique_nonempty(raw_evidence_ids),
        evidence_ids=[item.evidence_id for item in atoms],
        evidence=atoms,
        visual_role=visual_role,
        visual_reason=visual_reason,
        **typed_ids,
    )


def plan_evidence_shards(
    atoms: Sequence[EvidenceAtom], *, max_atoms: int = 80
) -> list[EvidenceUnitShard]:
    """Split deterministic atom groups without separating OCR from its frame."""
    if max_atoms < 1:
        raise ValueError("max_atoms must be at least 1")
    ordered = list(atoms)
    by_id = {atom.evidence_id: atom for atom in ordered}
    order_index = {atom.evidence_id: index for index, atom in enumerate(ordered)}
    by_frame: dict[str, list[EvidenceAtom]] = {}
    for atom in ordered:
        if atom.kind == "ocr" and atom.frame_id is not None:
            by_frame.setdefault(atom.frame_id, []).append(atom)

    groups: list[list[EvidenceAtom]] = []
    consumed: set[str] = set()
    for atom in ordered:
        if atom.evidence_id in consumed:
            continue
        group = [atom]
        frame_id = atom.evidence_id if atom.kind == "frame" else atom.frame_id
        parent = by_id.get(frame_id) if frame_id is not None else None
        if parent is not None and parent.kind == "frame":
            group = [parent, *by_frame.get(parent.evidence_id, [])]
            group.sort(key=lambda item: order_index[item.evidence_id])
        consumed.update(item.evidence_id for item in group)
        groups.append(group)

    shards: list[list[EvidenceAtom]] = []
    current: list[EvidenceAtom] = []
    for group in groups:
        if current and len(current) + len(group) > max_atoms:
            shards.append(current)
            current = []
        current.extend(group)
    if current:
        shards.append(current)

    result: list[EvidenceUnitShard] = []
    for index, shard_atoms in enumerate(shards, start=1):
        starts = [item.start_ms for item in shard_atoms if item.start_ms is not None]
        ends = [
            item.end_ms if item.end_ms is not None else item.start_ms
            for item in shard_atoms
            if item.start_ms is not None
        ]
        result.append(
            EvidenceUnitShard(
                shard_id=f"shard_{index:04d}",
                atom_ids=[item.evidence_id for item in shard_atoms],
                start_ms=min(starts),
                end_ms=max(ends),
            )
        )
    return result


def canonical_shard_json(
    shard: EvidenceUnitShard, atoms: Iterable[EvidenceAtom]
) -> str:
    """Return the exact provider input for one shard."""
    atoms_by_id = {atom.evidence_id: atom for atom in atoms}
    selected = [atoms_by_id[atom_id] for atom_id in shard.atom_ids]
    payload = {
        "schema_version": "1.0",
        "shard": shard.model_dump(mode="json"),
        "atoms": [atom.model_dump(mode="json") for atom in selected],
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def canonical_evidence_units_json(units: Sequence[EvidenceUnit]) -> str:
    payload = {
        "schema_version": "1.0",
        "units": [unit.model_dump(mode="json") for unit in units],
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def evidence_units_sha256(units: Sequence[EvidenceUnit]) -> str:
    return hashlib.sha256(
        canonical_evidence_units_json(units).encode("utf-8")
    ).hexdigest()


def _unique_nonempty(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _atom_from_evidence(item: Evidence) -> EvidenceAtom:
    payload = item.model_dump(mode="python")
    payload["evidence_id"] = payload.pop("id")
    return EvidenceAtom.model_validate(payload)
