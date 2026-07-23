from __future__ import annotations

import pytest

from learnnest.evidence_units import build_evidence_unit
from learnnest.models import ContentPack, Evidence, TaskRecord
from learnnest.note_models import ReaderDraft
from learnnest.note_quality import analyze_quality
from learnnest.quality_note import build_quality_note, render_quality_note
from learnnest.reader_templates import (
    builtin_reader_template,
    reader_template_snapshot_sha256,
)
from learnnest.evidence_unit_models import EvidenceUnitOrganization


def _task() -> TaskRecord:
    return TaskRecord(
        task_id="20260723-quality-render",
        source_path="C:/videos/quality.mp4",
        source_fingerprint="quality-render-fingerprint",
        title="质量渲染",
    )


def _pack() -> ContentPack:
    return ContentPack(
        task_id="20260723-quality-render",
        source_fingerprint="quality-render-fingerprint",
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


def _organization(pack: ContentPack) -> EvidenceUnitOrganization:
    first = build_evidence_unit(
        pack,
        unit_id="eu_0001",
        shard_id="shard_0001",
        raw_evidence_ids=["tr_0001", "ocr_0001"],
        unit_type="step",
        topic_labels=["设置"],
        outline="打开设置",
        visual_role="required_for_understanding",
        visual_reason="界面入口",
    )
    second = build_evidence_unit(
        pack,
        unit_id="eu_0002",
        shard_id="shard_0001",
        raw_evidence_ids=["tr_0002"],
        unit_type="step",
        topic_labels=["保存"],
        outline="保存修改",
        visual_role="none",
    )
    return EvidenceUnitOrganization(
        task_id=pack.task_id,
        source_fingerprint=pack.source_fingerprint,
        content_pack_sha256="a" * 64,
        units=[first, second],
        shard_ids=["shard_0001"],
    )


def _draft() -> ReaderDraft:
    return ReaderDraft.model_validate(
        {
            "title": "设置与保存",
            "sections": [
                {
                    "slot": "summary",
                    "items": [
                        {
                            "text": "先定位设置入口，再保存修改。",
                            "evidence_unit_ids": ["eu_0001", "eu_0002"],
                        }
                    ],
                },
                {
                    "slot": "why_learn",
                    "items": [
                        {
                            "text": "这能让修改流程可复现。",
                            "evidence_unit_ids": ["eu_0001"],
                        }
                    ],
                },
                {
                    "slot": "narrative",
                    "items": [
                        {
                            "text": "内容先展示入口，再说明保存。",
                            "evidence_unit_ids": ["eu_0001", "eu_0002"],
                        }
                    ],
                },
                {
                    "slot": "core",
                    "items": [
                        {
                            "text": "设置入口和保存动作是两个连续环节。",
                            "evidence_unit_ids": ["eu_0001", "eu_0002"],
                        }
                    ],
                },
                {
                    "slot": "practice",
                    "items": [
                        {
                            "text": "按顺序打开设置并保存。",
                            "evidence_unit_ids": ["eu_0001", "eu_0002"],
                        }
                    ],
                },
                {"slot": "review", "items": []},
            ],
            "ai_supplements": [{"text": "入口名称可能随版本变化。"}],
        }
    )


def test_program_injects_template_identity_and_expands_units_to_source_evidence() -> (
    None
):
    pack = _pack()
    template = builtin_reader_template("tutorial")

    note = build_quality_note(
        _task(),
        pack,
        _organization(pack),
        _draft(),
        template=template,
        content_pack_sha256="a" * 64,
    )

    assert note.task_id == _task().task_id
    assert note.template_sha256 == reader_template_snapshot_sha256(template)
    assert [section.order for section in note.sections] == list(
        range(1, len(note.sections) + 1)
    )
    summary = next(section for section in note.sections if section.slot == "summary")
    assert summary.items[0].evidence_ids == [
        "tr_0001",
        "ocr_0001",
        "fr_0001",
        "tr_0002",
    ]
    assert summary.items[0].ai_supplement is False


def test_quality_renderer_keeps_trace_and_first_use_visual_embed() -> None:
    pack = _pack()
    template = builtin_reader_template("concept")
    note = build_quality_note(
        _task(),
        pack,
        _organization(pack),
        _draft(),
        template=template,
        content_pack_sha256="a" * 64,
    )

    markdown = render_quality_note(
        _task(), pack, note, asset_prefix="视频学习素材/quality--render"
    )

    assert "# 设置与保存" in markdown
    assert "<!-- evidence: tr_0001, ocr_0001, fr_0001, tr_0002 -->" in markdown
    assert "![[视频学习素材/quality--render/frames/selected/fr_0001.png]]" in markdown
    assert "## 来源与追溯" in markdown
    assert "AI 补充，不属于视频事实" in markdown


def test_unknown_unit_is_a_source_failure_not_a_quality_warning() -> None:
    pack = _pack()
    draft = _draft().model_copy(deep=True)
    draft.sections[0].items[0].evidence_unit_ids = ["eu_9999"]

    with pytest.raises(ValueError, match="unknown evidence unit"):
        build_quality_note(
            _task(),
            pack,
            _organization(pack),
            draft,
            template=builtin_reader_template("concept"),
            content_pack_sha256="a" * 64,
        )


def test_quality_report_flags_missing_required_visual_without_deleting_candidate() -> (
    None
):
    pack = _pack()
    template = builtin_reader_template("concept")
    draft = _draft().model_copy(deep=True)
    for section in draft.sections:
        for item in section.items:
            item.evidence_unit_ids = ["eu_0002"]
    note = build_quality_note(
        _task(),
        pack,
        _organization(pack),
        draft,
        template=template,
        content_pack_sha256="a" * 64,
    )

    report = analyze_quality(note, _organization(pack), pack)

    assert report.status == "flagged"
    assert any(issue.code == "required_visual_missing" for issue in report.issues)
    assert note.task_id == pack.task_id
