from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

from learnnest import local_model_worker, local_models
from learnnest.local_models import (
    LocalModelCancelled,
    LocalModelError,
    LocalModelService,
    model_store_root,
    resolve_asr_model,
    resolve_ocr_models,
)


def _wait_for_state(
    service: LocalModelService,
    package_id: str,
    expected: set[str],
) -> dict[str, object]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = service.get(package_id)
        if status["state"] in expected:
            return status
        time.sleep(0.01)
    raise AssertionError(f"model job did not reach {sorted(expected)}")


def _write_component_files(staging: Path, package_id: str) -> None:
    if package_id == "faster-whisper-large-v3":
        component = staging / "model"
        names = (
            "config.json",
            "model.bin",
            "preprocessor_config.json",
            "tokenizer.json",
            "vocabulary.json",
        )
    else:
        for component_name in ("detection", "recognition"):
            component = staging / component_name
            component.mkdir(parents=True, exist_ok=True)
            for name in ("inference.json", "inference.pdiparams", "inference.yml"):
                (component / name).write_bytes(f"{component_name}:{name}".encode())
        return
    component.mkdir(parents=True, exist_ok=True)
    for name in names:
        (component / name).write_bytes(name.encode())


def test_status_query_is_local_and_does_not_start_an_install(tmp_path: Path) -> None:
    calls: list[str] = []

    def installer(*args: object, **kwargs: object) -> None:
        calls.append("install")

    service = LocalModelService(
        tmp_path / "models", installer=installer, discover_external=False
    )

    statuses = service.list()

    assert [item["package_id"] for item in statuses] == [
        "faster-whisper-large-v3",
        "paddleocr-pp-ocrv6-medium",
    ]
    assert {item["state"] for item in statuses} == {"not_installed"}
    assert calls == []


def test_install_validates_then_atomically_publishes_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def installer(spec, staging, progress, cancel) -> None:  # type: ignore[no-untyped-def]
        progress("downloading", 7)
        _write_component_files(staging, spec.package_id)
        progress("downloading", 19)

    service = LocalModelService(
        tmp_path / "models", installer=installer, discover_external=False
    )
    publication_had_clean_staging: list[bool] = []
    atomic_json = local_models._atomic_json

    def checked_atomic_json(path: Path, payload: dict[str, object]) -> None:
        if path.name == "current.json":
            publication_had_clean_staging.append(
                not (service.root / "staging").exists()
            )
        atomic_json(path, payload)

    monkeypatch.setattr(local_models, "_atomic_json", checked_atomic_json)

    accepted = service.install("faster-whisper-large-v3")
    assert accepted["state"] in {"queued", "downloading", "verifying", "ready"}
    ready = _wait_for_state(service, "faster-whisper-large-v3", {"ready"})

    assert ready["install_source"] == "managed"
    assert ready["error"] is None
    assert ready["downloaded_bytes"] == 19
    model_path = resolve_asr_model(service.root)
    assert model_path.is_dir()
    assert (model_path / "model.bin").is_file()
    assert not (service.root / "staging").exists()
    assert publication_had_clean_staging == [True]

    pointer = json.loads(
        (
            service.root / "packages" / "faster-whisper-large-v3" / "current.json"
        ).read_text(encoding="utf-8")
    )
    assert pointer["install_id"]
    assert "path" not in json.dumps(ready)


def test_invalid_download_fails_closed_without_publishing(tmp_path: Path) -> None:
    def installer(spec, staging, progress, cancel) -> None:  # type: ignore[no-untyped-def]
        (staging / "model").mkdir(parents=True)
        (staging / "model" / "config.json").write_text("{}", encoding="utf-8")

    service = LocalModelService(
        tmp_path / "models", installer=installer, discover_external=False
    )
    service.install("faster-whisper-large-v3")

    failed = _wait_for_state(service, "faster-whisper-large-v3", {"failed"})

    assert failed["error"] == "模型文件不完整，请重试下载。"
    assert failed["can_retry"] is True
    assert not (
        service.root / "packages" / "faster-whisper-large-v3" / "current.json"
    ).exists()


def test_failed_install_can_be_explicitly_retried(tmp_path: Path) -> None:
    calls = 0

    def installer(spec, staging, progress, cancel) -> None:  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary upstream failure")
        _write_component_files(staging, spec.package_id)

    service = LocalModelService(
        tmp_path / "models", installer=installer, discover_external=False
    )
    service.install("faster-whisper-large-v3")
    first = _wait_for_state(service, "faster-whisper-large-v3", {"failed"})

    service.install("faster-whisper-large-v3")
    second = _wait_for_state(service, "faster-whisper-large-v3", {"ready"})

    assert first["attempt"] == 1
    assert second["attempt"] == 2
    assert calls == 2


def test_cancel_stops_current_install_and_keeps_no_partial_model(
    tmp_path: Path,
) -> None:
    started = threading.Event()

    def installer(spec, staging, progress, cancel) -> None:  # type: ignore[no-untyped-def]
        started.set()
        while not cancel.is_set():
            time.sleep(0.01)
        raise LocalModelCancelled

    service = LocalModelService(
        tmp_path / "models", installer=installer, discover_external=False
    )
    service.install("paddleocr-pp-ocrv6-medium")
    assert started.wait(timeout=1)

    service.cancel("paddleocr-pp-ocrv6-medium")
    cancelled = _wait_for_state(service, "paddleocr-pp-ocrv6-medium", {"interrupted"})

    assert cancelled["can_retry"] is True
    assert not (service.root / "staging").exists()


def test_second_process_does_not_interrupt_or_duplicate_active_install(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    finish = threading.Event()
    second_calls = 0

    def first_installer(spec, staging, progress, cancel) -> None:  # type: ignore[no-untyped-def]
        started.set()
        assert finish.wait(timeout=2)
        _write_component_files(staging, spec.package_id)

    def second_installer(spec, staging, progress, cancel) -> None:  # type: ignore[no-untyped-def]
        nonlocal second_calls
        second_calls += 1

    root = tmp_path / "models"
    first = LocalModelService(root, installer=first_installer, discover_external=False)
    first.install("faster-whisper-large-v3")
    assert started.wait(timeout=1)

    second = LocalModelService(
        root, installer=second_installer, discover_external=False
    )
    with pytest.raises(LocalModelError, match="正在下载"):
        second.install("faster-whisper-large-v3")

    assert second.get("faster-whisper-large-v3")["state"] == "downloading"
    assert second_calls == 0
    finish.set()
    _wait_for_state(first, "faster-whisper-large-v3", {"ready"})


def test_ocr_resolution_requires_both_components(tmp_path: Path) -> None:
    def installer(spec, staging, progress, cancel) -> None:  # type: ignore[no-untyped-def]
        _write_component_files(staging, spec.package_id)

    service = LocalModelService(
        tmp_path / "models", installer=installer, discover_external=False
    )
    service.install("paddleocr-pp-ocrv6-medium")
    _wait_for_state(service, "paddleocr-pp-ocrv6-medium", {"ready"})

    detection, recognition = resolve_ocr_models(service.root)

    assert detection.name == "detection"
    assert recognition.name == "recognition"


def test_external_ocr_files_require_pinned_revision_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    external = tmp_path / "paddlex"
    monkeypatch.setenv("PADDLE_PDX_CACHE_HOME", str(external))
    component_revisions = {
        "PP-OCRv6_medium_det": "8e0f56fb2ef86b461d99cfc7ac5c137738985f61",
        "PP-OCRv6_medium_rec": "e5a92bcbc5cc1b494628e458d267778f0704fd7c",
    }
    for name in component_revisions:
        component = external / "official_models" / name
        component.mkdir(parents=True)
        for filename in ("inference.json", "inference.pdiparams", "inference.yml"):
            (component / filename).write_bytes(b"model")
    service = LocalModelService(tmp_path / "managed")

    assert service.get("paddleocr-pp-ocrv6-medium")["state"] == "not_installed"

    for name, revision in component_revisions.items():
        tree = external / "official_models" / name / ".cache" / "huggingface" / "trees"
        tree.mkdir(parents=True)
        (tree / f"{revision}.json").write_text("{}", encoding="utf-8")

    status = service.get("paddleocr-pp-ocrv6-medium")
    assert status["state"] == "external_ready"
    assert status["install_source"] == "external_cache"


def test_model_store_root_defaults_to_program_model_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_app_data = tmp_path / "local-app-data"
    program_directory = tmp_path / "LearnNest"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(
        "learnnest.launcher.application_directory", lambda: program_directory
    )

    assert model_store_root() == program_directory / "model"
    service = LocalModelService(discover_external=False)
    assert service.root == program_directory / "model"
    assert service.root.is_dir()


def test_model_store_root_uses_launcher_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_app_data = tmp_path / "local-app-data"
    config_path = local_app_data / "LearnNest" / "launcher.json"
    configured = tmp_path / "shared-models"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "output_root": str(tmp_path / "vault"),
                "model_root": str(configured),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    assert model_store_root() == configured.resolve()


def test_download_worker_uses_only_pinned_official_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []

    def snapshot_download(**kwargs: object) -> None:
        calls.append(kwargs)
        destination = Path(str(kwargs["local_dir"]))
        destination.mkdir(parents=True)
        for name in kwargs["allow_patterns"]:  # type: ignore[union-attr]
            (destination / str(name)).write_bytes(b"model")

    module = ModuleType("huggingface_hub")
    module.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    local_model_worker.install("paddleocr-pp-ocrv6-medium", tmp_path)

    assert [(call["repo_id"], call["revision"]) for call in calls] == [
        (
            "PaddlePaddle/PP-OCRv6_medium_det",
            "8e0f56fb2ef86b461d99cfc7ac5c137738985f61",
        ),
        (
            "PaddlePaddle/PP-OCRv6_medium_rec",
            "e5a92bcbc5cc1b494628e458d267778f0704fd7c",
        ),
    ]
    assert all(call["max_workers"] == 1 for call in calls)
