from __future__ import annotations

from pathlib import Path

import pytest

from learnnest.material_adapters import (
    MaterialAdapterError,
    material_adapters_from_bindings,
)
from learnnest.models import ProviderBindingSnapshot


def _binding(*, capability: str, provider: str, model: str) -> ProviderBindingSnapshot:
    return ProviderBindingSnapshot(
        capability=capability,
        connection_id=f"selected-{capability}",
        provider=provider,
        endpoint=f"local://{capability}",
        model=model,
        adapter_revision="1",
        settings_sha256="a" * 64,
    )


def test_local_material_bindings_run_the_existing_isolated_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import learnnest.material_adapters as material_adapters

    calls: list[list[str]] = []
    monkeypatch.setattr(
        material_adapters.providers,
        "run_worker",
        lambda args: calls.append(list(args)),
    )
    selected = material_adapters_from_bindings(
        {
            "asr": _binding(
                capability="asr", provider="local-asr", model="local-installed"
            ),
            "ocr": _binding(
                capability="ocr", provider="local-ocr", model="local-installed"
            ),
        }
    )

    selected.asr.transcribe(tmp_path / "video.mp4", tmp_path / "transcript.json")
    selected.ocr.recognize(tmp_path / "frame.png", tmp_path / "ocr.json")

    assert calls == [
        [
            "asr",
            str(tmp_path / "video.mp4"),
            str(tmp_path / "transcript.json"),
            "--model",
            "large-v3",
        ],
        ["ocr", str(tmp_path / "frame.png"), str(tmp_path / "ocr.json")],
    ]


@pytest.mark.parametrize(
    ("role", "binding", "message"),
    [
        (
            "asr",
            _binding(capability="asr", provider="cloud-asr", model="remote"),
            "所选 ASR",
        ),
        (
            "ocr",
            _binding(capability="ocr", provider="cloud-ocr", model="remote"),
            "所选 OCR",
        ),
    ],
)
def test_configured_unsupported_material_binding_fails_without_fallback(
    role: str, binding: ProviderBindingSnapshot, message: str
) -> None:
    with pytest.raises(MaterialAdapterError, match=message):
        material_adapters_from_bindings({role: binding})
