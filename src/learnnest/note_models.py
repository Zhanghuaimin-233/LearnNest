"""Versioned data contract for generated structured notes."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)

from learnnest.note_templates import SemanticBlockKind
from learnnest.reader_templates import ReaderSlot


def _validate_plain_text(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("text must be a single paragraph")
    if "[[" in value or "]]" in value:
        raise ValueError("text must not contain Obsidian markup")
    return value


NonEmptyString = Annotated[str, Field(min_length=1)]
PlainText = Annotated[
    str,
    Field(min_length=1),
    AfterValidator(_validate_plain_text),
]


class _NoteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoteStatement(_NoteModel):
    """A factual statement backed by at least one evidence item."""

    text: PlainText
    evidence_ids: list[NonEmptyString] = Field(min_length=1)


class NoteStep(_NoteModel):
    """An ordered operation backed by at least one evidence item."""

    order: int
    text: PlainText
    evidence_ids: list[NonEmptyString] = Field(min_length=1)


class AiSupplement(_NoteModel):
    """Clearly separated model-generated context without evidence claims."""

    text: PlainText


_READER_URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)
_READER_MARKUP_PATTERN = re.compile(
    r"<!--.*?-->|</?[A-Za-z][^<>]*>", re.IGNORECASE | re.DOTALL
)
_READER_FORBIDDEN_MARKDOWN_PATTERN = re.compile(
    r"(?m)^\s{0,3}#{1,6}\s|"
    r"!\[\[|!\[[^\]]*\]\(|"
    r"\[[^\]]+\]\([^)]+\)|"
    r"\[\^[^\]]+\]"
)


def _validate_reader_markdown(value: str) -> str:
    if _READER_URL_PATTERN.search(value):
        raise ValueError("ReaderDraft Markdown must not contain URLs")
    if _READER_MARKUP_PATTERN.search(value):
        raise ValueError("ReaderDraft Markdown must not contain HTML")
    if _READER_FORBIDDEN_MARKDOWN_PATTERN.search(value):
        raise ValueError(
            "ReaderDraft Markdown must not contain headings, links, images, or citations"
        )
    return value


ReaderMarkdown = Annotated[
    str,
    Field(min_length=1),
    AfterValidator(_validate_reader_markdown),
]


class ReaderDraftItem(_NoteModel):
    """Provider-only reader content; it cannot carry program-owned fields."""

    markdown: ReaderMarkdown
    evidence_unit_ids: list[NonEmptyString] = Field(default_factory=list, max_length=32)
    visual_unit_id: NonEmptyString | None = None
    ai_supplement: bool = False

    @model_validator(mode="after")
    def source_and_ai_are_separate(self) -> ReaderDraftItem:
        if self.ai_supplement and (
            self.evidence_unit_ids or self.visual_unit_id is not None
        ):
            raise ValueError(
                "ai_supplement items must not cite evidence or select a visual"
            )
        if len(self.evidence_unit_ids) != len(set(self.evidence_unit_ids)):
            raise ValueError("evidence_unit_ids must be unique")
        if (
            self.visual_unit_id is not None
            and self.visual_unit_id not in self.evidence_unit_ids
        ):
            raise ValueError("visual_unit_id must be one of evidence_unit_ids")
        return self


class ReaderDraftSection(_NoteModel):
    """Provider-facing slot contents before program section IDs are injected."""

    slot: ReaderSlot
    items: list[ReaderDraftItem] = Field(default_factory=list)


class ReaderDraft(_NoteModel):
    """The quality-first Writer response without task or rendering metadata."""

    schema_version: Literal["2.0"] = "2.0"
    title: PlainText
    sections: list[ReaderDraftSection] = Field(default_factory=list)
    ai_supplements: list[AiSupplement] = Field(default_factory=list)

    @model_validator(mode="after")
    def visual_budget_is_bounded(self) -> ReaderDraft:
        visual_unit_ids = [
            item.visual_unit_id
            for section in self.sections
            for item in section.items
            if item.visual_unit_id is not None
        ]
        if len(visual_unit_ids) > 3:
            raise ValueError("ReaderDraft may select at most three visuals")
        if len(visual_unit_ids) != len(set(visual_unit_ids)):
            raise ValueError("ReaderDraft visual selections must be unique")
        return self


class QualityNoteLocator(_NoteModel):
    """A program-derived URL locator, never supplied by the Writer."""

    url: PlainText
    evidence_ids: list[NonEmptyString] = Field(min_length=1)


class QualityNoteItem(_NoteModel):
    """Program-owned rendered item with expanded raw evidence IDs."""

    order: int = Field(ge=1)
    markdown: ReaderMarkdown
    evidence_unit_ids: list[NonEmptyString] = Field(default_factory=list)
    citation_evidence_ids: list[NonEmptyString] = Field(default_factory=list)
    evidence_ids: list[NonEmptyString] = Field(default_factory=list)
    visual_unit_id: NonEmptyString | None = None
    visual_evidence_id: NonEmptyString | None = None
    ai_supplement: bool = False
    derived_from_cited_note: bool = False
    locator: QualityNoteLocator | None = None

    @model_validator(mode="after")
    def item_source_contract(self) -> QualityNoteItem:
        if self.ai_supplement and (
            self.evidence_unit_ids
            or self.citation_evidence_ids
            or self.evidence_ids
            or self.visual_unit_id is not None
            or self.visual_evidence_id is not None
            or self.derived_from_cited_note
        ):
            raise ValueError("ai_supplement items cannot contain source evidence")
        if self.derived_from_cited_note and (
            self.evidence_unit_ids
            or self.citation_evidence_ids
            or self.evidence_ids
            or self.visual_unit_id is not None
            or self.visual_evidence_id is not None
            or self.locator is not None
        ):
            raise ValueError("derived reader aids cannot contain direct source fields")
        if self.derived_from_cited_note:
            return self
        if not self.ai_supplement and not self.evidence_unit_ids:
            raise ValueError("source item requires evidence_unit_ids")
        if not self.ai_supplement and not self.citation_evidence_ids:
            raise ValueError("source item requires citation_evidence_ids")
        if not self.ai_supplement and not self.evidence_ids:
            raise ValueError("source item requires expanded evidence_ids")
        if set(self.citation_evidence_ids) - set(self.evidence_ids):
            raise ValueError("citation evidence must be included in full evidence")
        if (self.visual_unit_id is None) != (self.visual_evidence_id is None):
            raise ValueError("visual unit and evidence must be selected together")
        if (
            self.visual_unit_id is not None
            and self.visual_unit_id not in self.evidence_unit_ids
        ):
            raise ValueError("visual_unit_id must be one of evidence_unit_ids")
        if (
            self.visual_evidence_id is not None
            and self.visual_evidence_id not in self.evidence_ids
        ):
            raise ValueError("visual evidence must be included in full evidence")
        return self


class QualityNoteSection(_NoteModel):
    """Renderer-owned section with stable ID, purpose, and contiguous order."""

    order: int = Field(ge=1)
    section_id: NonEmptyString
    slot: ReaderSlot
    heading: PlainText
    purpose: PlainText
    required: bool
    items: list[QualityNoteItem] = Field(default_factory=list)


class QualityNoteEnvelope(_NoteModel):
    """Formal program-owned quality-first note bundle envelope."""

    schema_version: Literal["2.0"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    content_pack_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    template_id: NonEmptyString
    template_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    title: PlainText
    sections: list[QualityNoteSection] = Field(min_length=7)
    ai_supplements: list[AiSupplement] = Field(default_factory=list)

    @model_validator(mode="after")
    def sections_are_ordered(self) -> QualityNoteEnvelope:
        orders = [section.order for section in self.sections]
        if orders != list(range(1, len(self.sections) + 1)):
            raise ValueError("quality note section order must be contiguous")
        section_ids = [section.section_id for section in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("quality note section IDs must be unique")
        return self


class QualityReviewIssue(_NoteModel):
    """Provider-only advisory issue; the local quality report remains authoritative."""

    code: NonEmptyString
    severity: Literal["low", "medium", "high"]
    message: PlainText
    section_id: NonEmptyString | None = None


class QualityReviewerResponse(_NoteModel):
    """Strict response from the optional Reviewer role."""

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["passed", "flagged"]
    issues: list[QualityReviewIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def flagged_response_has_an_issue(self) -> QualityReviewerResponse:
        if self.status == "flagged" and not self.issues:
            raise ValueError("flagged quality review requires issues")
        return self


class GeneratedNote(_NoteModel):
    """The complete version 2.0 structured note returned by a provider."""

    schema_version: Literal["2.0"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    title: PlainText
    audience: NoteStatement
    summary: NoteStatement
    key_points: list[NoteStatement] = Field(min_length=1)
    steps: list[NoteStep]
    cautions: list[NoteStatement]
    ai_supplements: list[AiSupplement]

    @model_validator(mode="after")
    def step_orders_must_be_contiguous_from_one(self) -> GeneratedNote:
        orders = [step.order for step in self.steps]
        if orders != list(range(1, len(self.steps) + 1)):
            raise ValueError("step order must start at 1 and be unique and contiguous")
        return self


class ResourceLocator(_NoteModel):
    url: PlainText
    evidence_ids: list[NonEmptyString] = Field(min_length=1)


class SemanticBlockItem(_NoteModel):
    """One template-owned V4 item; all visible factual text stays evidenced."""

    order: int | None = Field(default=None, ge=1)
    title: NoteStatement | None = None
    content: NoteStatement
    locator: ResourceLocator | None = None


class SemanticBlock(_NoteModel):
    """A V4 semantic block selected by the persisted template snapshot."""

    block_id: NonEmptyString
    semantic_block: SemanticBlockKind
    items: list[SemanticBlockItem]


class GeneratedNoteV4(_NoteModel):
    """Template-driven GeneratedNote 4.0 contract for all new note creation."""

    schema_version: Literal["4.0"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    template_id: NonEmptyString
    template_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    title: NoteStatement
    blocks: list[SemanticBlock] = Field(min_length=1)
    ai_supplements: list[AiSupplement] = Field(default_factory=list)

    @model_validator(mode="after")
    def block_ids_must_be_unique(self) -> GeneratedNoteV4:
        block_ids = [block.block_id for block in self.blocks]
        if len(set(block_ids)) != len(block_ids):
            raise ValueError("semantic block IDs must be unique")
        return self


class _GeneratedNoteV3Base(_NoteModel):
    schema_version: Literal["3.0"]
    task_id: NonEmptyString
    source_fingerprint: NonEmptyString
    classification_evidence_ids: list[NonEmptyString] = Field(max_length=12)
    title: PlainText
    summary: NoteStatement
    ai_supplements: list[AiSupplement] = Field(default_factory=list)


class ConceptItem(_NoteModel):
    title: PlainText
    explanation: NoteStatement


class PracticalStep(_NoteModel):
    order: int = Field(ge=1)
    title: PlainText
    action: NoteStatement
    expected_result: NoteStatement | None = None


class ConceptExplanationNote(_GeneratedNoteV3Base):
    note_type: Literal["concept_explanation"]
    concepts: list[ConceptItem] = Field(min_length=1)
    background: NoteStatement | None = None
    relationships: list[NoteStatement] = Field(default_factory=list)
    misconceptions: list[NoteStatement] = Field(default_factory=list)
    review: NoteStatement | None = None


class ResourceItem(_NoteModel):
    name: NoteStatement
    value: NoteStatement
    suitable_for: NoteStatement | None = None
    access_or_usage: NoteStatement | None = None
    locator: ResourceLocator | None = None
    limitations: list[NoteStatement] = Field(default_factory=list)


class ResourceShareNote(_GeneratedNoteV3Base):
    note_type: Literal["resource_share"]
    resources: list[ResourceItem] = Field(min_length=1)
    reminders: list[NoteStatement] = Field(default_factory=list)


class TroubleshootingItem(_NoteModel):
    symptom: NoteStatement
    resolution: NoteStatement


class PracticalTutorialNote(_GeneratedNoteV3Base):
    note_type: Literal["practical_tutorial"]
    goal: NoteStatement
    prerequisites: list[NoteStatement] = Field(default_factory=list)
    steps: list[PracticalStep] = Field(min_length=1)
    troubleshooting: list[TroubleshootingItem] = Field(default_factory=list)
    completion_checks: list[NoteStatement] = Field(default_factory=list)
    cautions: list[NoteStatement] = Field(default_factory=list)

    @model_validator(mode="after")
    def step_orders_must_be_contiguous_from_one(self) -> PracticalTutorialNote:
        orders = [step.order for step in self.steps]
        if orders != list(range(1, len(self.steps) + 1)):
            raise ValueError("step order must start at 1 and be contiguous")
        return self


GeneratedNoteV3 = Annotated[
    ConceptExplanationNote | ResourceShareNote | PracticalTutorialNote,
    Field(discriminator="note_type"),
]
AnyGeneratedNote = GeneratedNote | GeneratedNoteV3 | GeneratedNoteV4
GENERATED_NOTE_ADAPTER = TypeAdapter(AnyGeneratedNote)
GENERATED_NOTE_V3_ADAPTER = TypeAdapter(GeneratedNoteV3)


def generated_note_v3_json_schema() -> dict[str, object]:
    return GENERATED_NOTE_V3_ADAPTER.json_schema()


def generated_note_v4_json_schema() -> dict[str, object]:
    return TypeAdapter(GeneratedNoteV4).json_schema()


def reader_draft_json_schema() -> dict[str, object]:
    return TypeAdapter(ReaderDraft).json_schema()


def quality_reviewer_json_schema() -> dict[str, object]:
    return TypeAdapter(QualityReviewerResponse).json_schema()
