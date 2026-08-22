"""ASR and OCR execution adapters consumed by the material pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from learnnest import providers
from learnnest.models import ProviderBindingSnapshot


class MaterialAdapterError(ValueError):
    """A frozen material binding cannot be executed by this build."""


class AsrAdapter(Protocol):
    """One ASR implementation that writes the shared transcript contract."""

    def transcribe(self, media_path: Path, output_path: Path) -> None: ...


class OcrAdapter(Protocol):
    """One OCR implementation that writes the shared frame OCR contract."""

    def recognize(self, image_path: Path, output_path: Path) -> None: ...


@dataclass(frozen=True)
class LocalFasterWhisperAdapter:
    """Run the existing isolated faster-whisper worker."""

    model: str = "large-v3"

    def transcribe(self, media_path: Path, output_path: Path) -> None:
        providers.run_worker(
            ["asr", str(media_path), str(output_path), "--model", self.model]
        )


@dataclass(frozen=True)
class LocalPaddleOcrAdapter:
    """Run the existing isolated PaddleOCR worker."""

    def recognize(self, image_path: Path, output_path: Path) -> None:
        providers.run_worker(["ocr", str(image_path), str(output_path)])


@dataclass(frozen=True)
class MaterialAdapters:
    """The two material capabilities frozen for one task execution."""

    asr: AsrAdapter
    ocr: OcrAdapter


def material_adapters_from_bindings(
    bindings: Mapping[str, ProviderBindingSnapshot],
) -> MaterialAdapters:
    """Resolve task-frozen ASR/OCR bindings into executable adapters.

    Missing bindings preserve the historical local-first behavior. A configured
    but unsupported binding fails before either worker runs; it never silently
    falls back to a different provider.
    """

    asr_binding = bindings.get("asr")
    if asr_binding is not None and (
        asr_binding.capability != "asr"
        or asr_binding.provider != "local-asr"
        or asr_binding.model != "local-installed"
    ):
        raise MaterialAdapterError("当前版本不能执行所选 ASR；请改用本地 ASR。")

    ocr_binding = bindings.get("ocr")
    if ocr_binding is not None and (
        ocr_binding.capability != "ocr"
        or ocr_binding.provider != "local-ocr"
        or ocr_binding.model != "local-installed"
    ):
        raise MaterialAdapterError("当前版本不能执行所选 OCR；请改用本地 OCR。")

    return MaterialAdapters(
        asr=LocalFasterWhisperAdapter(),
        ocr=LocalPaddleOcrAdapter(),
    )
