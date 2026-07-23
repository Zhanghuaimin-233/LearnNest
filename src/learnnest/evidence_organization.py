"""Organizer boundary and source-contract normalization for quality notes."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Protocol

from pydantic import ValidationError, TypeAdapter

from learnnest.evidence_unit_models import (
    EvidenceUnitOrganization,
    EvidenceUnitOrganizationResponse,
)
from learnnest.evidence_units import (
    build_evidence_atoms,
    build_evidence_unit,
    canonical_shard_json,
    plan_evidence_shards,
)
from learnnest.models import ContentPack


class EvidenceUnitOrganizer(Protocol):
    """The one-call-per-shard semantic organization boundary."""

    name: str
    model: str

    def organize(self, shard_json: str) -> str: ...


_EVIDENCE_ID_PATTERN = re.compile(r"^(tr|fr|ocr)_([0-9]+)$")


def _normalize_zero_padded_id_alias(
    evidence_id: str,
    shard_atom_ids: list[str],
    normalizations: list[str],
) -> str:
    """Repair only one unambiguous numeric ID alias within the current shard."""
    if evidence_id in shard_atom_ids:
        return evidence_id
    match = _EVIDENCE_ID_PATTERN.fullmatch(evidence_id)
    if match is None:
        return evidence_id
    prefix, numeric_suffix = match.groups()
    numeric_value = int(numeric_suffix)
    candidates = [
        candidate
        for candidate in shard_atom_ids
        if candidate.startswith(f"{prefix}_")
        and int(candidate.split("_", 1)[1]) == numeric_value
    ]
    if len(candidates) != 1:
        return evidence_id
    normalized = candidates[0]
    normalizations.append(
        f"normalized organizer evidence ID {evidence_id} -> {normalized}"
    )
    return normalized


def organize_evidence_units(
    content_pack: ContentPack,
    organizer: EvidenceUnitOrganizer,
    *,
    content_pack_sha256: str,
    max_atoms: int = 80,
) -> EvidenceUnitOrganization:
    """Organize every deterministic shard exactly once and fail closed on output."""
    if len(content_pack_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in content_pack_sha256
    ):
        raise ValueError("content_pack_sha256 must be a lowercase SHA-256")
    atoms = build_evidence_atoms(content_pack)
    if not atoms:
        raise ValueError("content pack has no source evidence atoms")
    shards = plan_evidence_shards(atoms, max_atoms=max_atoms)
    responses: list[EvidenceUnitOrganizationResponse] = []
    for shard in shards:
        payload = canonical_shard_json(shard, atoms)
        try:
            raw_response = organizer.organize(payload)
            responses.append(
                EvidenceUnitOrganizationResponse.model_validate_json(raw_response)
            )
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
            raise ValueError("organizer response failed schema validation") from error
    return build_organization_from_responses(
        content_pack,
        list(zip(shards, responses, strict=True)),
        content_pack_sha256=content_pack_sha256,
    )


def build_organization_from_responses(
    content_pack: ContentPack,
    shard_responses: list[tuple[object, EvidenceUnitOrganizationResponse]],
    *,
    content_pack_sha256: str,
) -> EvidenceUnitOrganization:
    """Normalize already-persisted provider responses without another call."""
    from learnnest.evidence_unit_models import EvidenceUnitShard

    if len(content_pack_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in content_pack_sha256
    ):
        raise ValueError("content_pack_sha256 must be a lowercase SHA-256")
    known_evidence_ids = set(content_pack.evidence_by_id())
    units = []
    normalizations: list[str] = []

    for raw_shard, response in shard_responses:
        if not isinstance(raw_shard, EvidenceUnitShard):
            raise ValueError("invalid persisted evidence unit shard")
        shard = raw_shard

        shard_units = []
        for proposal in response.units:
            trimmed_ids = [item.strip() for item in proposal.raw_evidence_ids]
            if trimmed_ids != proposal.raw_evidence_ids:
                normalizations.append("deduplicated or trimmed organizer raw IDs")
            raw_ids: list[str] = []
            for evidence_id in trimmed_ids:
                normalized = _normalize_zero_padded_id_alias(
                    evidence_id,
                    shard.atom_ids,
                    normalizations,
                )
                if normalized in raw_ids:
                    normalizations.append("deduplicated or trimmed organizer raw IDs")
                    continue
                raw_ids.append(normalized)
            unknown = next(
                (
                    evidence_id
                    for evidence_id in raw_ids
                    if evidence_id not in known_evidence_ids
                ),
                None,
            )
            if unknown is not None:
                raise ValueError(f"unknown evidence id: {unknown}")
            if any(evidence_id not in shard.atom_ids for evidence_id in raw_ids):
                outside = next(
                    evidence_id
                    for evidence_id in raw_ids
                    if evidence_id not in shard.atom_ids
                )
                raise ValueError(f"evidence id is outside the shard: {outside}")
            unit = build_evidence_unit(
                content_pack,
                unit_id=f"eu_{len(units) + len(shard_units) + 1:04d}",
                shard_id=shard.shard_id,
                raw_evidence_ids=raw_ids,
                unit_type=proposal.unit_type,
                topic_labels=proposal.topic_labels,
                outline=proposal.outline,
                visual_role=proposal.visual_role,
                visual_reason=proposal.visual_reason,
                allowed_evidence_ids=set(shard.atom_ids),
            )
            shard_units.append(unit)

        covered = {
            evidence_id for unit in shard_units for evidence_id in unit.evidence_ids
        }
        missing = [
            evidence_id for evidence_id in shard.atom_ids if evidence_id not in covered
        ]
        if missing:
            raise ValueError(
                "organizer omitted source evidence IDs: " + ", ".join(missing)
            )
        units.extend(shard_units)

    if not units:
        raise ValueError("organizer returned no evidence units")
    if not units:
        raise ValueError("organizer returned no evidence units")
    return EvidenceUnitOrganization(
        task_id=content_pack.task_id,
        source_fingerprint=content_pack.source_fingerprint,
        content_pack_sha256=content_pack_sha256,
        units=units,
        shard_ids=[shard.shard_id for shard, _response in shard_responses],
        normalizations=normalizations,
    )


def organization_response_json_schema() -> dict[str, object]:
    """Return the provider-only Organizer response schema."""
    return TypeAdapter(EvidenceUnitOrganizationResponse).json_schema()


def canonical_organization_json(organization: EvidenceUnitOrganization) -> str:
    return json.dumps(
        organization.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def organization_sha256(organization: EvidenceUnitOrganization) -> str:
    return hashlib.sha256(
        canonical_organization_json(organization).encode("utf-8")
    ).hexdigest()
