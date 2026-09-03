"""Isolated provider entry point for ASR, OCR, and provider health checks."""

from __future__ import annotations

import argparse
import json
import math
import re
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator


_DEFAULT_ASR_MODEL = "large-v3"
_ASR_SEGMENT_SOFT_MS = 4_000
_ASR_SEGMENT_HARD_MS = 8_000
_ASR_BOUNDARY_CHARS = tuple("，,。.!！?？;；:：、")
_HTTP_URL_PATTERN = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    re.IGNORECASE,
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _word_time_ms(word: Any) -> tuple[int, int]:
    return round(float(word.start) * 1_000), round(float(word.end) * 1_000)


def _segment_words(segment: Any) -> list[Any] | None:
    words = list(getattr(segment, "words", None) or [])
    if not words:
        return None

    previous_end_ms = -1
    for word in words:
        try:
            start = float(word.start)
            end = float(word.end)
            text = word.word
        except (AttributeError, TypeError, ValueError):
            return None
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end < start
            or not isinstance(text, str)
            or not text
        ):
            return None
        start_ms, end_ms = _word_time_ms(word)
        if start_ms < previous_end_ms:
            return None
        previous_end_ms = end_ms

    if "".join(word.word for word in words).strip() != segment.text.strip():
        return None
    return words


def _url_internal_cut_indices(words: list[Any]) -> set[int]:
    joined = "".join(word.word for word in words)
    boundaries: list[tuple[int, int]] = []
    offset = 0
    for cut_index, word in enumerate(words[:-1], start=1):
        offset += len(word.word)
        boundaries.append((cut_index, offset))

    forbidden: set[int] = set()
    for match in _HTTP_URL_PATTERN.finditer(joined):
        forbidden.update(
            cut_index
            for cut_index, offset in boundaries
            if match.start() < offset < match.end()
        )
    return forbidden


def _split_timed_words(
    words: list[Any], *, forbidden_cut_indices: set[int] | None = None
) -> list[list[Any]]:
    first_start_ms, _ = _word_time_ms(words[0])
    _, last_end_ms = _word_time_ms(words[-1])
    if last_end_ms - first_start_ms <= _ASR_SEGMENT_HARD_MS:
        return [words]

    chunks: list[list[Any]] = []
    start_index = 0
    forbidden = forbidden_cut_indices or set()
    while start_index < len(words):
        start_ms, _ = _word_time_ms(words[start_index])
        last_within_hard_limit: int | None = None
        cut_index: int | None = None
        for index in range(start_index, len(words)):
            _, end_ms = _word_time_ms(words[index])
            duration_ms = end_ms - start_ms
            boundary = index + 1
            boundary_allowed = boundary not in forbidden
            if duration_ms <= _ASR_SEGMENT_HARD_MS and boundary_allowed:
                last_within_hard_limit = index + 1
            if (
                _ASR_SEGMENT_SOFT_MS <= duration_ms <= _ASR_SEGMENT_HARD_MS
                and boundary_allowed
                and words[index].word.rstrip().endswith(_ASR_BOUNDARY_CHARS)
            ):
                cut_index = index + 1
                break
            if duration_ms > _ASR_SEGMENT_HARD_MS:
                break

        cut_index = cut_index or last_within_hard_limit
        if cut_index is None:
            return [words]
        chunks.append(words[start_index:cut_index])
        start_index = cut_index

    if len(chunks) >= 2:
        previous, tail = chunks[-2:]
        previous_start_ms, _ = _word_time_ms(previous[0])
        _, tail_end_ms = _word_time_ms(tail[-1])
        if tail_end_ms - previous_start_ms <= _ASR_SEGMENT_HARD_MS:
            chunks[-2:] = [previous + tail]
    return chunks


def _asr_segments(provider_segments: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for provider_segment in provider_segments:
        text = provider_segment.text.strip()
        if not text:
            continue
        words = _segment_words(provider_segment)
        if words is None:
            normalized.append(
                {
                    "start_ms": round(provider_segment.start * 1_000),
                    "end_ms": round(provider_segment.end * 1_000),
                    "text": text,
                }
            )
            continue
        normalized.extend(
            {
                "start_ms": _word_time_ms(chunk[0])[0],
                "end_ms": _word_time_ms(chunk[-1])[1],
                "text": "".join(word.word for word in chunk).strip(),
            }
            for chunk in _split_timed_words(
                words,
                forbidden_cut_indices=_url_internal_cut_indices(words),
            )
        )

    return [
        {"id": f"tr_{index:04d}", **segment}
        for index, segment in enumerate(normalized, start=1)
    ]


def run_asr(
    media_path: Path,
    output_path: Path,
    model_name: str,
    *,
    model_path: Path | None = None,
) -> dict[str, Any]:
    """Transcribe a media file inside the CTranslate2-only worker process."""

    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(model_path) if model_path is not None else model_name,
        device="cuda",
        compute_type="int8_float16",
        use_auth_token=False,
        local_files_only=True,
    )
    segments, info = model.transcribe(
        str(media_path), language="zh", word_timestamps=True
    )
    payload = {
        "schema_version": "1.0",
        "provider": "faster-whisper",
        "model": model_name,
        "language": info.language,
        "segments": _asr_segments(list(segments)),
    }
    _write_json(output_path, payload)
    return payload


def _ocr_items(result: Any) -> list[dict[str, Any]]:
    """Extract recognized text and scores from PaddleOCR's result object."""

    json_result = result.json
    if callable(json_result):
        json_result = json_result()
    if isinstance(json_result, str):
        json_result = json.loads(json_result)
    result_data = json_result.get("res", json_result)
    texts = result_data.get("rec_texts", [])
    scores = result_data.get("rec_scores", [])
    return [
        {"text": text, "confidence": score}
        for text, score in zip(texts, scores, strict=True)
        if text.strip()
    ]


def run_ocr(
    image_path: Path,
    output_path: Path,
    *,
    detection_model_path: Path | None = None,
    recognition_model_path: Path | None = None,
) -> dict[str, Any]:
    """Recognize one selected frame inside the Paddle-only worker process."""

    from paddleocr import PaddleOCR

    model_arguments: dict[str, object] = {}
    if detection_model_path is not None and recognition_model_path is not None:
        model_arguments = {
            "text_detection_model_name": "PP-OCRv6_medium_det",
            "text_detection_model_dir": str(detection_model_path),
            "text_recognition_model_name": "PP-OCRv6_medium_rec",
            "text_recognition_model_dir": str(recognition_model_path),
        }
    ocr = PaddleOCR(
        lang="ch",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,
        **model_arguments,
    )
    payload = {
        "schema_version": "1.0",
        "provider": "paddleocr",
        "items": [
            item
            for result in ocr.predict(str(image_path))
            for item in _ocr_items(result)
        ],
    }
    _write_json(output_path, payload)
    return payload


@contextmanager
def _verification_fixture(filename: str) -> Iterator[Path]:
    fixture = files("learnnest").joinpath("fixtures", filename)
    if not fixture.is_file():
        raise FileNotFoundError(
            "local verification fixture is missing: "
            f"{filename}; see src/learnnest/fixtures/README.md"
        )
    with as_file(fixture) as path:
        yield path


def doctor_asr() -> dict[str, Any]:
    """Verify ASR using a rights-cleared local audio fixture."""

    with (
        _verification_fixture("asr-smoke.wav") as fixture,
        TemporaryDirectory(prefix="learnnest-doctor-") as temporary_directory,
    ):
        temporary_output = Path(temporary_directory) / "transcript.json"
        from learnnest.local_models import resolve_asr_model

        payload = run_asr(
            fixture,
            temporary_output,
            model_name=_DEFAULT_ASR_MODEL,
            model_path=resolve_asr_model(),
        )
        if not payload["segments"]:
            raise RuntimeError("ASR verification produced no transcript segments")
        return {
            "provider": "faster-whisper",
            "ok": True,
            "segments": len(payload["segments"]),
        }


def doctor_ocr() -> dict[str, Any]:
    """Verify OCR using a rights-cleared local image fixture."""

    with (
        _verification_fixture("ocr-smoke.png") as fixture,
        TemporaryDirectory(prefix="learnnest-doctor-") as temporary_directory,
    ):
        temporary_output = Path(temporary_directory) / "ocr.json"
        from learnnest.local_models import resolve_ocr_models

        detection, recognition = resolve_ocr_models()
        payload = run_ocr(
            fixture,
            temporary_output,
            detection_model_path=detection,
            recognition_model_path=recognition,
        )
        if not payload["items"]:
            raise RuntimeError("OCR verification produced no recognized text")
        return {"provider": "paddleocr", "ok": True, "items": len(payload["items"])}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="learnnest.worker")
    commands = parser.add_subparsers(dest="command", required=True)

    asr = commands.add_parser("asr")
    asr.add_argument("media_path", type=Path)
    asr.add_argument("output_path", type=Path)
    asr.add_argument("--model", default=_DEFAULT_ASR_MODEL)

    ocr = commands.add_parser("ocr")
    ocr.add_argument("image_path", type=Path)
    ocr.add_argument("output_path", type=Path)

    commands.add_parser("doctor-asr")
    commands.add_parser("doctor-ocr")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "asr":
        from learnnest.local_models import resolve_asr_model

        payload = run_asr(
            args.media_path,
            args.output_path,
            args.model,
            model_path=resolve_asr_model(),
        )
    elif args.command == "ocr":
        from learnnest.local_models import resolve_ocr_models

        detection, recognition = resolve_ocr_models()
        payload = run_ocr(
            args.image_path,
            args.output_path,
            detection_model_path=detection,
            recognition_model_path=recognition,
        )
    elif args.command == "doctor-asr":
        payload = doctor_asr()
    else:
        payload = doctor_ocr()
    print(json.dumps(payload, ensure_ascii=True))


if __name__ == "__main__":
    main()
