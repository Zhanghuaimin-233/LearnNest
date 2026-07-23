from __future__ import annotations

import pytest
from pydantic import ValidationError

from learnnest.evidence_units import (
    build_evidence_atoms,
    build_evidence_unit,
    canonical_evidence_units_json,
    evidence_units_sha256,
    expand_evidence_ids,
    plan_evidence_shards,
)
from learnnest.models import ContentPack, Evidence


def _content_pack() -> ContentPack:
    return ContentPack(
        task_id="20260723-quality-units",
        source_fingerprint="quality-fingerprint",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="先打开设置页面。",
                artifact_path="content_pack.json",
            ),
            Evidence(
                id="fr_0001",
                kind="frame",
                start_ms=500,
                artifact_path="frames/selected/fr_0001.png",
                related_evidence_ids=["ocr_0001"],
            ),
            Evidence(
                id="ocr_0001",
                kind="ocr",
                start_ms=500,
                end_ms=700,
                text="设置",
                artifact_path="content_pack.json",
                frame_id="fr_0001",
            ),
            Evidence(
                id="tr_0002",
                kind="transcript",
                start_ms=1_000,
                end_ms=2_000,
                text="然后保存修改。",
                artifact_path="content_pack.json",
            ),
        ],
    )


def test_unit_expansion_closes_ocr_to_its_unique_parent_frame() -> None:
    pack = _content_pack()

    expanded = expand_evidence_ids(pack, ["ocr_0001"])
    unit = build_evidence_unit(
        pack,
        unit_id="eu_0001",
        raw_evidence_ids=["ocr_0001"],
        unit_type="concept",
        topic_labels=["设置"],
        outline="页面入口",
        visual_role="required_for_understanding",
        visual_reason="关键界面入口",
    )

    assert [item.id for item in expanded] == ["ocr_0001", "fr_0001"]
    assert unit.evidence_ids == ["ocr_0001", "fr_0001"]
    assert unit.frame_ids == ["fr_0001"]
    assert unit.ocr_ids == ["ocr_0001"]


def test_unit_expansion_rejects_unknown_or_cross_shard_evidence() -> None:
    pack = _content_pack()

    with pytest.raises(ValueError, match="unknown evidence id"):
        expand_evidence_ids(pack, ["tr_9999"])

    with pytest.raises(ValueError, match="outside the shard"):
        build_evidence_unit(
            pack,
            unit_id="eu_0001",
            raw_evidence_ids=["tr_0001"],
            unit_type="background",
            topic_labels=[],
            outline="背景",
            visual_role="none",
            allowed_evidence_ids={"tr_0002"},
        )


def test_shards_keep_ocr_parent_and_cover_atoms_deterministically() -> None:
    atoms = build_evidence_atoms(_content_pack())
    shards = plan_evidence_shards(atoms, max_atoms=2)

    flattened = [atom_id for shard in shards for atom_id in shard.atom_ids]

    assert flattened == [atom.evidence_id for atom in atoms]
    assert any({"fr_0001", "ocr_0001"}.issubset(shard.atom_ids) for shard in shards)
    assert [shard.shard_id for shard in shards] == [
        "shard_0001",
        "shard_0002",
        "shard_0003",
    ]


def test_shards_keep_ocr_parent_when_ocr_timestamp_precedes_frame() -> None:
    pack = _content_pack()
    pack.evidence[2] = pack.evidence[2].model_copy(update={"start_ms": 400})
    atoms = build_evidence_atoms(pack)

    shards = plan_evidence_shards(atoms, max_atoms=2)

    assert [atom_id for shard in shards for atom_id in shard.atom_ids] == [
        atom.evidence_id for atom in atoms
    ]
    assert any({"fr_0001", "ocr_0001"}.issubset(shard.atom_ids) for shard in shards)


def test_unit_serialization_and_sha_are_stable() -> None:
    pack = _content_pack()
    unit = build_evidence_unit(
        pack,
        unit_id="eu_0001",
        raw_evidence_ids=["tr_0001", "fr_0001"],
        unit_type="step",
        topic_labels=["入口"],
        outline="打开设置",
        visual_role="useful",
        visual_reason="界面确认",
    )

    first = canonical_evidence_units_json([unit])
    second = canonical_evidence_units_json([unit.model_copy(deep=True)])

    assert first == second
    assert evidence_units_sha256([unit]) == evidence_units_sha256(
        [unit.model_copy(deep=True)]
    )
    assert '"unit_id":"eu_0001"' in first


def test_unit_model_rejects_reversed_time_range() -> None:
    pack = _content_pack()

    with pytest.raises(ValidationError, match="end_ms"):
        build_evidence_unit(
            pack,
            unit_id="eu_0001",
            raw_evidence_ids=["tr_0001"],
            unit_type="background",
            topic_labels=[],
            outline="背景",
            visual_role="none",
            start_ms=2_000,
            end_ms=1_000,
        )
