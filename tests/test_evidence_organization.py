from __future__ import annotations

import json

import pytest

from learnnest.evidence_organization import (
    canonical_writer_organization_json,
    organize_evidence_units,
)
from learnnest.evidence_unit_models import EvidenceUnitOrganization
from learnnest.evidence_units import build_evidence_atoms, build_evidence_unit
from learnnest.models import ContentPack, Evidence


def _content_pack() -> ContentPack:
    return ContentPack(
        task_id="20260723-quality-organization",
        source_fingerprint="organization-fingerprint",
        evidence=[
            Evidence(
                id="tr_0001",
                kind="transcript",
                start_ms=0,
                end_ms=1_000,
                text="先打开设置。",
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
                text="再保存修改。",
                artifact_path="content_pack.json",
            ),
        ],
    )


class FakeOrganizer:
    name = "fake-organizer"
    model = "fake-1"

    def __init__(
        self, *, invalid: bool = False, raw_ids: list[str] | None = None
    ) -> None:
        self.calls: list[str] = []
        self.invalid = invalid
        self.raw_ids = raw_ids

    def organize(self, shard_json: str) -> str:
        self.calls.append(shard_json)
        payload = json.loads(shard_json)
        atom_ids = [atom["evidence_id"] for atom in payload["atoms"]]
        raw_ids = (
            self.raw_ids
            if self.raw_ids is not None
            else (["tr_9999"] if self.invalid else atom_ids)
        )
        visual_anchor = next(
            (evidence_id for evidence_id in raw_ids if evidence_id.startswith("fr_")),
            None,
        )
        return json.dumps(
            {
                "schema_version": "2.0",
                "units": [
                    {
                        "raw_evidence_ids": raw_ids,
                        "unit_type": "concept",
                        "topic_labels": ["设置"],
                        "outline": "程序生成的组织纲要",
                        "reader_relevance": "core",
                        "citation_anchor_ids": raw_ids[:3],
                        "visual_role": "useful" if visual_anchor else "none",
                        "visual_reason": "帮助定位画面" if visual_anchor else None,
                        "visual_anchor_id": visual_anchor,
                    }
                ],
            },
            ensure_ascii=False,
        )


def test_organizer_calls_once_per_shard_and_program_owns_unit_ids() -> None:
    pack = _content_pack()
    organizer = FakeOrganizer()

    organization = organize_evidence_units(
        pack,
        organizer,
        content_pack_sha256="a" * 64,
        max_atoms=2,
    )

    assert len(organizer.calls) == 3
    assert [unit.unit_id for unit in organization.units] == [
        "eu_0001",
        "eu_0002",
        "eu_0003",
    ]
    assert organization.units[1].frame_ids == ["fr_0001"]
    assert organization.units[1].ocr_ids == ["ocr_0001"]
    assert organization.shard_ids == ["shard_0001", "shard_0002", "shard_0003"]


def test_organizer_rejects_unknown_ids_without_retrying() -> None:
    organizer = FakeOrganizer(invalid=True)

    with pytest.raises(ValueError, match="unknown evidence id"):
        organize_evidence_units(
            _content_pack(),
            organizer,
            content_pack_sha256="b" * 64,
        )

    assert len(organizer.calls) == 1


def test_organizer_normalizes_unique_zero_padded_id_aliases() -> None:
    organizer = FakeOrganizer(raw_ids=["tr_001", "fr_001", "ocr_001", "tr_002"])

    organization = organize_evidence_units(
        _content_pack(),
        organizer,
        content_pack_sha256="d" * 64,
        max_atoms=10,
    )

    assert organization.units[0].raw_evidence_ids == [
        "tr_0001",
        "fr_0001",
        "ocr_0001",
        "tr_0002",
    ]
    assert organization.normalizations == [
        "normalized organizer evidence ID tr_001 -> tr_0001",
        "normalized organizer evidence ID fr_001 -> fr_0001",
        "normalized organizer evidence ID ocr_001 -> ocr_0001",
        "normalized organizer evidence ID tr_002 -> tr_0002",
    ]


def test_organizer_does_not_guess_reordered_id_digits() -> None:
    organizer = FakeOrganizer(raw_ids=["tr_0100"])

    with pytest.raises(ValueError, match="unknown evidence id: tr_0100"):
        organize_evidence_units(
            _content_pack(),
            organizer,
            content_pack_sha256="e" * 64,
            max_atoms=10,
        )


def test_organizer_input_contains_only_deterministic_source_atoms() -> None:
    pack = _content_pack()
    organizer = FakeOrganizer()

    organize_evidence_units(
        pack,
        organizer,
        content_pack_sha256="c" * 64,
        max_atoms=10,
    )

    payload = json.loads(organizer.calls[0])
    assert payload["schema_version"] == "2.0"
    assert payload["shard"]["shard_id"] == "shard_0001"
    assert [atom["evidence_id"] for atom in payload["atoms"]] == [
        atom.evidence_id for atom in build_evidence_atoms(pack)
    ]
    assert "task_id" not in payload


def test_writer_input_excludes_background_and_full_evidence_closure() -> None:
    pack = _content_pack()
    core = build_evidence_unit(
        pack,
        unit_id="eu_0001",
        shard_id="shard_0001",
        raw_evidence_ids=["tr_0001", "ocr_0001"],
        unit_type="concept",
        topic_labels=["设置"],
        outline="打开设置",
        reader_relevance="core",
        citation_anchor_ids=["tr_0001"],
        visual_role="none",
    )
    background = build_evidence_unit(
        pack,
        unit_id="eu_0002",
        shard_id="shard_0001",
        raw_evidence_ids=["tr_0002"],
        unit_type="background",
        topic_labels=["背景"],
        outline="补充背景",
        reader_relevance="background",
        citation_anchor_ids=["tr_0002"],
        visual_role="none",
    )
    organization = EvidenceUnitOrganization(
        task_id=pack.task_id,
        source_fingerprint=pack.source_fingerprint,
        content_pack_sha256="a" * 64,
        units=[core, background],
        shard_ids=["shard_0001"],
    )

    payload = json.loads(canonical_writer_organization_json(organization))

    assert [unit["unit_id"] for unit in payload["units"]] == ["eu_0001"]
    assert payload["units"][0]["citation_anchor_ids"] == ["tr_0001"]
    assert "evidence_ids" not in payload["units"][0]
    assert "ocr_0001" not in json.dumps(payload, ensure_ascii=False)
