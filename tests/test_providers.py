from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from learnnest import worker


def install_fake_faster_whisper(
    monkeypatch: pytest.MonkeyPatch, segments: list[SimpleNamespace] | None = None
) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    class FakeWhisperModel:
        def __init__(
            self,
            model_name: str,
            *,
            device: str,
            compute_type: str,
            use_auth_token: bool | None = None,
            local_files_only: bool = False,
        ) -> None:
            calls.append(
                (model_name, device, compute_type, use_auth_token, local_files_only)
            )

        def transcribe(
            self, media_path: str, *, language: str, word_timestamps: bool
        ) -> tuple[object, object]:
            calls.append((media_path, language, word_timestamps, "transcribe"))
            result_segments = (
                segments
                if segments is not None
                else [
                    SimpleNamespace(
                        start=0.0,
                        end=1.25,
                        text="你好",
                        words=[
                            SimpleNamespace(
                                start=0.0,
                                end=1.25,
                                word="你好",
                                probability=0.99,
                            )
                        ],
                    )
                ]
            )
            return iter(result_segments), SimpleNamespace(language="zh")

    module = ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return calls


def install_fake_paddleocr(
    monkeypatch: pytest.MonkeyPatch, items: list[dict[str, object]] | None = None
) -> list[dict[str, object]]:
    constructor_arguments: list[dict[str, object]] = []
    result_items = (
        items if items is not None else [{"text": "设置", "confidence": 0.98}]
    )

    class FakeResult:
        json = {
            "res": {
                "rec_texts": [item["text"] for item in result_items],
                "rec_scores": [item["confidence"] for item in result_items],
            }
        }

    class FakePaddleOCR:
        def __init__(self, **kwargs: object) -> None:
            constructor_arguments.append(kwargs)

        def predict(self, image_path: str) -> list[FakeResult]:
            constructor_arguments.append({"image_path": image_path})
            return [FakeResult()]

    module = ModuleType("paddleocr")
    module.PaddleOCR = FakePaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    return constructor_arguments


def test_run_worker_uses_current_interpreter_and_worker_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from learnnest import providers

    recorded: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(args[0], 0, stdout="{}", stderr="")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)

    result = providers.run_worker(["asr", "lesson.mp4", "transcript.json"])

    assert result.stdout == "{}"
    assert recorded["args"] == (
        [
            sys.executable,
            "-m",
            "learnnest.worker",
            "asr",
            "lesson.mp4",
            "transcript.json",
        ],
    )
    options = recorded["kwargs"]
    assert options["capture_output"] is True
    assert options["text"] is True
    assert options["check"] is False
    assert options["env"]["HF_HUB_OFFLINE"] == "1"
    assert options["env"]["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] == "True"


def test_run_worker_forwards_cached_models_in_offline_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from learnnest import providers

    recorded: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(args[0], 0, stdout="{}", stderr="")

    monkeypatch.setattr(
        providers,
        "load_runtime_environment",
        lambda _working_directory: {"HUGGINGFACE_HUB_CACHE": "E:/models"},
    )
    monkeypatch.setattr(providers.subprocess, "run", fake_run)

    providers.run_worker(["asr", "lesson.mp4", "transcript.json"])

    environment = recorded["kwargs"]["env"]
    assert environment["HUGGINGFACE_HUB_CACHE"] == "E:/models"
    assert environment["HF_HUB_OFFLINE"] == "1"


def test_parent_provider_module_does_not_import_asr_or_ocr_libraries() -> None:
    from learnnest import providers

    assert "faster_whisper" not in providers.__dict__
    assert "paddleocr" not in providers.__dict__


def test_run_worker_reports_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from learnnest import providers

    failed = subprocess.CompletedProcess(
        [sys.executable, "-m", "learnnest.worker", "doctor-asr"],
        1,
        stdout="",
        stderr="model unavailable",
    )
    monkeypatch.setattr(providers.subprocess, "run", lambda *args, **kwargs: failed)

    with pytest.raises(RuntimeError, match="model unavailable"):
        providers.run_worker(["doctor-asr"])


def test_local_verification_fixture_is_found_outside_project_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    with worker._verification_fixture("asr-smoke.wav") as fixture:
        assert fixture.is_file()
        assert fixture.name == "asr-smoke.wav"


def test_missing_local_verification_fixture_has_a_specific_error() -> None:
    with pytest.raises(
        FileNotFoundError, match="local verification fixture.*missing.wav"
    ):
        with worker._verification_fixture("missing.wav"):
            pass


def test_asr_worker_writes_transcript_json_from_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = install_fake_faster_whisper(monkeypatch)
    output_path = tmp_path / "transcript.json"

    payload = worker.run_asr(tmp_path / "fixture.wav", output_path, "tiny")

    assert payload["segments"] == [
        {"id": "tr_0001", "start_ms": 0, "end_ms": 1_250, "text": "你好"}
    ]
    assert output_path.read_text(encoding="utf-8")
    assert calls == [
        ("tiny", "cuda", "int8_float16", False, True),
        (str(tmp_path / "fixture.wav"), "zh", True, "transcribe"),
    ]


def test_asr_worker_does_not_use_an_ambient_huggingface_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    constructor_arguments: dict[str, object] = {}

    class AuthCapturingWhisperModel:
        def __init__(
            self,
            model_name: str,
            *,
            device: str,
            compute_type: str,
            use_auth_token: bool | None = None,
            local_files_only: bool = False,
        ) -> None:
            constructor_arguments.update(
                {
                    "model_name": model_name,
                    "device": device,
                    "compute_type": compute_type,
                    "use_auth_token": use_auth_token,
                    "local_files_only": local_files_only,
                }
            )

        def transcribe(
            self, media_path: str, *, language: str, word_timestamps: bool
        ) -> tuple[object, object]:
            del media_path, language, word_timestamps
            return (
                iter(
                    [
                        SimpleNamespace(
                            start=0.0,
                            end=1.0,
                            text="测试",
                            words=None,
                        )
                    ]
                ),
                SimpleNamespace(language="zh"),
            )

    module = ModuleType("faster_whisper")
    module.WhisperModel = AuthCapturingWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)

    worker.run_asr(tmp_path / "fixture.wav", tmp_path / "transcript.json", "large-v3")

    assert constructor_arguments["use_auth_token"] is False
    assert constructor_arguments["local_files_only"] is True


def test_asr_worker_splits_long_word_timestamps_without_losing_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    segments = [
        SimpleNamespace(
            start=0.0,
            end=12.0,
            text="第一段内容，继续说明这个概念，最后补充没有标点的长内容",
            words=[
                SimpleNamespace(start=0.0, end=1.0, word="第一", probability=0.99),
                SimpleNamespace(start=1.0, end=2.0, word="段内容，", probability=0.99),
                SimpleNamespace(start=2.0, end=3.0, word="继续", probability=0.99),
                SimpleNamespace(start=3.0, end=4.2, word="说明", probability=0.99),
                SimpleNamespace(start=4.2, end=5.0, word="这个", probability=0.99),
                SimpleNamespace(start=5.0, end=6.0, word="概念，", probability=0.99),
                SimpleNamespace(start=6.0, end=7.0, word="最后", probability=0.99),
                SimpleNamespace(start=7.0, end=8.0, word="补充", probability=0.99),
                SimpleNamespace(start=8.0, end=9.0, word="没有", probability=0.99),
                SimpleNamespace(start=9.0, end=10.0, word="标点", probability=0.99),
                SimpleNamespace(start=10.0, end=11.0, word="的长", probability=0.99),
                SimpleNamespace(start=11.0, end=12.0, word="内容", probability=0.99),
            ],
        )
    ]
    install_fake_faster_whisper(monkeypatch, segments)

    payload = worker.run_asr(
        tmp_path / "fixture.wav", tmp_path / "transcript.json", "large-v3"
    )

    assert len(payload["segments"]) > 1
    assert "".join(item["text"] for item in payload["segments"]) == segments[0].text
    assert all(
        item["end_ms"] - item["start_ms"] <= 8_000 for item in payload["segments"]
    )
    assert [item["id"] for item in payload["segments"]] == [
        f"tr_{index:04d}" for index in range(1, len(payload["segments"]) + 1)
    ]
    assert all(
        current["start_ms"] >= previous["end_ms"]
        for previous, current in zip(
            payload["segments"], payload["segments"][1:], strict=False
        )
    )


def test_asr_worker_does_not_accept_punctuation_beyond_hard_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    segments = [
        SimpleNamespace(
            start=0.0,
            end=8.2,
            text="前半后半。",
            words=[
                SimpleNamespace(start=0.0, end=4.0, word="前半", probability=0.99),
                SimpleNamespace(start=4.0, end=8.2, word="后半。", probability=0.99),
            ],
        )
    ]
    install_fake_faster_whisper(monkeypatch, segments)

    payload = worker.run_asr(
        tmp_path / "fixture.wav", tmp_path / "transcript.json", "large-v3"
    )

    assert [item["text"] for item in payload["segments"]] == ["前半", "后半。"]
    assert all(
        item["end_ms"] - item["start_ms"] <= 8_000 for item in payload["segments"]
    )


def test_asr_worker_does_not_split_inside_http_locator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    url = "https://example.com/path?query=one"
    segments = [
        SimpleNamespace(
            start=0.0,
            end=11.0,
            text=f"查看{url}再继续说明",
            words=[
                SimpleNamespace(start=0.0, end=1.0, word="查看", probability=0.99),
                SimpleNamespace(start=1.0, end=3.0, word="https", probability=0.99),
                SimpleNamespace(
                    start=3.0,
                    end=6.0,
                    word="://example.com/path?",
                    probability=0.99,
                ),
                SimpleNamespace(start=6.0, end=9.0, word="query=one", probability=0.99),
                SimpleNamespace(start=9.0, end=10.0, word="再继续", probability=0.99),
                SimpleNamespace(start=10.0, end=11.0, word="说明", probability=0.99),
            ],
        )
    ]
    install_fake_faster_whisper(monkeypatch, segments)

    payload = worker.run_asr(
        tmp_path / "fixture.wav", tmp_path / "transcript.json", "large-v3"
    )

    assert any(url in item["text"] for item in payload["segments"])
    assert "".join(item["text"] for item in payload["segments"]) == segments[0].text
    assert all(
        item["end_ms"] - item["start_ms"] <= 8_000 for item in payload["segments"]
    )


def test_asr_worker_falls_back_to_atomic_segment_when_words_do_not_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    segments = [
        SimpleNamespace(
            start=1.0,
            end=11.0,
            text="必须保留的原文",
            words=[
                SimpleNamespace(
                    start=1.0,
                    end=2.0,
                    word="不匹配",
                    probability=0.1,
                )
            ],
        )
    ]
    install_fake_faster_whisper(monkeypatch, segments)

    payload = worker.run_asr(
        tmp_path / "fixture.wav", tmp_path / "transcript.json", "large-v3"
    )

    assert payload["segments"] == [
        {
            "id": "tr_0001",
            "start_ms": 1_000,
            "end_ms": 11_000,
            "text": "必须保留的原文",
        }
    ]


def test_ocr_worker_writes_json_and_disables_mkldnn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    constructor_arguments = install_fake_paddleocr(monkeypatch)
    output_path = tmp_path / "ocr.json"

    payload = worker.run_ocr(tmp_path / "fixture.png", output_path)

    assert payload["items"] == [{"text": "设置", "confidence": 0.98}]
    output_json = output_path.read_text(encoding="utf-8")
    assert "设置" in output_json
    assert json.loads(output_json)["items"] == payload["items"]
    assert constructor_arguments[0]["enable_mkldnn"] is False


def test_workers_receive_only_explicit_local_model_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asr_calls = install_fake_faster_whisper(monkeypatch)
    ocr_arguments = install_fake_paddleocr(monkeypatch)
    asr_model = tmp_path / "asr"
    detection = tmp_path / "det"
    recognition = tmp_path / "rec"

    worker.run_asr(
        tmp_path / "fixture.wav",
        tmp_path / "transcript.json",
        "large-v3",
        model_path=asr_model,
    )
    worker.run_ocr(
        tmp_path / "fixture.png",
        tmp_path / "ocr.json",
        detection_model_path=detection,
        recognition_model_path=recognition,
    )

    assert asr_calls[0] == (
        str(asr_model),
        "cuda",
        "int8_float16",
        False,
        True,
    )
    assert ocr_arguments[0]["text_detection_model_dir"] == str(detection)
    assert ocr_arguments[0]["text_recognition_model_dir"] == str(recognition)


def test_worker_stdout_json_uses_ascii_wire_format_for_cp936_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = {
        "provider": "paddleocr",
        "items": [{"text": "✓ 中文", "confidence": 0.98}],
    }
    arguments = SimpleNamespace(
        command="ocr",
        image_path=tmp_path / "fixture.png",
        output_path=tmp_path / "ocr.json",
    )
    stdout_bytes = io.BytesIO()
    cp936_stdout = io.TextIOWrapper(stdout_bytes, encoding="cp936", errors="strict")
    monkeypatch.setattr(
        worker, "build_parser", lambda: SimpleNamespace(parse_args=lambda: arguments)
    )
    monkeypatch.setattr(
        worker,
        "run_ocr",
        lambda image_path, output_path, **kwargs: payload,
    )
    monkeypatch.setattr(
        "learnnest.local_models.resolve_ocr_models",
        lambda: (Path("det-model"), Path("rec-model")),
    )
    monkeypatch.setattr(sys, "stdout", cp936_stdout)

    worker.main()
    cp936_stdout.flush()

    assert json.loads(stdout_bytes.getvalue().decode("cp936")) == payload


def test_doctor_asr_runs_the_provider_against_a_local_fixture_outside_project_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = install_fake_faster_whisper(monkeypatch)
    model_path = tmp_path / "large-v3"
    monkeypatch.setattr("learnnest.local_models.resolve_asr_model", lambda: model_path)
    monkeypatch.chdir(tmp_path)

    assert worker.doctor_asr() == {
        "provider": "faster-whisper",
        "ok": True,
        "segments": 1,
    }
    assert calls[0] == (
        str(model_path),
        "cuda",
        "int8_float16",
        False,
        True,
    )
    assert Path(calls[1][0]).name == "asr-smoke.wav"


def test_doctor_ocr_runs_the_provider_against_a_local_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    constructor_arguments = install_fake_paddleocr(monkeypatch)
    detection = tmp_path / "det"
    recognition = tmp_path / "rec"
    monkeypatch.setattr(
        "learnnest.local_models.resolve_ocr_models",
        lambda: (detection, recognition),
    )
    monkeypatch.chdir(tmp_path)

    assert worker.doctor_ocr() == {"provider": "paddleocr", "ok": True, "items": 1}
    assert Path(str(constructor_arguments[1]["image_path"])).name == "ocr-smoke.png"


@pytest.mark.parametrize(
    ("doctor", "installer"),
    [
        (worker.doctor_asr, install_fake_faster_whisper),
        (worker.doctor_ocr, install_fake_paddleocr),
    ],
)
def test_doctor_rejects_empty_provider_output(
    monkeypatch: pytest.MonkeyPatch,
    doctor: object,
    installer: object,
) -> None:
    if doctor is worker.doctor_asr:
        installer(monkeypatch, [])
        monkeypatch.setattr(
            "learnnest.local_models.resolve_asr_model", lambda: Path("asr-model")
        )
        expected = "ASR verification produced no transcript segments"
    else:
        installer(monkeypatch, [])
        monkeypatch.setattr(
            "learnnest.local_models.resolve_ocr_models",
            lambda: (Path("det-model"), Path("rec-model")),
        )
        expected = "OCR verification produced no recognized text"

    with pytest.raises(RuntimeError, match=expected):
        doctor()
