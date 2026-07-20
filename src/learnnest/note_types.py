"""Canonical note-type names shared by note generation workflows."""

from typing import Literal, cast

ConcreteNoteType = Literal[
    "concept_explanation", "resource_share", "practical_tutorial"
]
NoteTypeRequest = Literal["auto", "concept", "resource", "practical"]

_SHORT_TO_CONCRETE: dict[str, ConcreteNoteType] = {
    "concept": "concept_explanation",
    "resource": "resource_share",
    "practical": "practical_tutorial",
}


def normalize_note_type(value: str) -> ConcreteNoteType:
    normalized = value.strip().lower()
    if normalized in _SHORT_TO_CONCRETE:
        return _SHORT_TO_CONCRETE[normalized]
    if normalized in _SHORT_TO_CONCRETE.values():
        return cast(ConcreteNoteType, normalized)
    raise ValueError(f"unknown note type: {value}")
