from __future__ import annotations


def test_stage_graph_has_stable_order_and_paid_boundary() -> None:
    from learnnest.stages import PAID_STAGES, STAGES, stage_artifacts

    assert STAGES[:3] == ("source", "transcript", "frames")
    assert STAGES[-2:] == ("podcast_script", "tts")
    assert PAID_STAGES == frozenset({"note", "podcast_script", "tts"})
    assert stage_artifacts("content_pack") == ("content_pack.json", "trace.md")
    assert stage_artifacts("publish") == ("note.md",)
    assert stage_artifacts("podcast_script") == ("podcast_script.json", "speech.txt")
    assert stage_artifacts("tts") == ("audio.json", "audio.wav")


def test_downstream_stages_rejects_unknown_stage() -> None:
    import pytest

    from learnnest.stages import downstream_stages

    assert downstream_stages("ocr")[:2] == ("ocr", "evidence")
    with pytest.raises(ValueError, match="unknown stage"):
        downstream_stages("download")
