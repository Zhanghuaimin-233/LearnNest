"""Independent consumer for schedule-owned discovered links."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from learnnest.batch import run_batch
from learnnest.adapters.douyin_http import DouyinHttpTransport
from learnnest.adapters.douyin_media import (
    DouyinImageTextDownloader,
    ImageTextDownload,
)
from learnnest.downloader import YtDlpDownloader
from learnnest.discovery_store import (
    claim_pending_discoveries,
    mark_discovery_artifact_result,
    mark_discovery_results,
)
from learnnest.execution import classify_failure
from learnnest.models import TaskProfile, TaskRecord
from learnnest.pipeline import process_source
from learnnest.source_models import SourceItem
from learnnest.util import safe_title
from pydantic import SecretStr


@dataclass(frozen=True)
class DownloadOutcome:
    """Summary of one explicit pending-link consumer run."""

    batch_id: str | None
    claimed_count: int
    completed_count: int
    failed_count: int
    task_ids: tuple[str, ...] = ()


Processor = Callable[[SourceItem, Path, TaskProfile], TaskRecord]
ImageTextProcessor = Callable[[SourceItem, Path], ImageTextDownload]


def run_pending_downloads(
    output_root: str | Path,
    *,
    max_items: int = 10,
    profile: TaskProfile = "evidence",
    retry_failed: bool = False,
    schedule_id: str | None = None,
    selection: Literal["oldest", "latest_observed"] = "oldest",
    processor: Processor = process_source,
    douyin_cookie: SecretStr | None = None,
    image_text_downloader: ImageTextProcessor | None = None,
    now: datetime | None = None,
) -> DownloadOutcome:
    """Lease pending links, run a normal processing batch, and commit outcomes."""
    root = Path(output_root).resolve()
    selected_time = now or datetime.now(UTC)
    claims = claim_pending_discoveries(
        root,
        max_items=max_items,
        now=selected_time,
        retry_failed=retry_failed,
        schedule_id=schedule_id,
        selection=selection,
    )
    if not claims:
        return DownloadOutcome(None, 0, 0, 0)

    video_claims = [claim for claim in claims if claim.link.content_kind == "video"]
    image_claims = [
        claim for claim in claims if claim.link.content_kind == "image_text"
    ]
    batch_id: str | None = None
    completed = 0
    failed = 0
    completed_task_ids: list[str] = []
    if video_claims:
        manifest = run_batch(
            [claim.link.source for claim in video_claims],
            root,
            profile,
            processor=processor,
            now=selected_time,
            kind="manual",
            downloader=(
                YtDlpDownloader(cookie=douyin_cookie)
                if douyin_cookie is not None
                else None
            ),
        )
        batch_id = manifest.batch_id
        updates = [
            (
                claim,
                "downloaded"
                if result.status in {"completed", "skipped_duplicate"}
                else "failed",
                result.task_id,
                result.failure,
            )
            for claim, result in zip(video_claims, manifest.results, strict=True)
        ]
        mark_discovery_results(root, updates, now=selected_time)
        completed += sum(status == "downloaded" for _, status, _, _ in updates)
        failed += sum(status == "failed" for _, status, _, _ in updates)
        completed_task_ids.extend(
            task_id
            for _claim, status, task_id, _failure in updates
            if status == "downloaded" and task_id is not None
        )

    selected_image_downloader = image_text_downloader
    if selected_image_downloader is None and douyin_cookie is not None:
        selected_image_downloader = DouyinImageTextDownloader(
            DouyinHttpTransport(douyin_cookie)
        ).download
    for claim in image_claims:
        try:
            if selected_image_downloader is None:
                raise RuntimeError(
                    "Douyin cookie is missing; set DOUYIN_COOKIE or provide a local .env"
                )
            output_dir = _image_text_output_dir(root, claim.link.source)
            result = selected_image_downloader(claim.link.source, output_dir)
            artifact_path = result.manifest_path.resolve().relative_to(root)
            mark_discovery_artifact_result(
                root,
                claim,
                "downloaded",
                artifact_path.as_posix(),
                None,
                now=selected_time,
            )
            completed += 1
        except Exception as error:
            mark_discovery_artifact_result(
                root,
                claim,
                "failed",
                None,
                classify_failure(error),
                now=selected_time,
            )
            failed += 1
    return DownloadOutcome(
        batch_id=batch_id,
        claimed_count=len(claims),
        completed_count=completed,
        failed_count=failed,
        task_ids=tuple(completed_task_ids),
    )


def _image_text_output_dir(root: Path, source: SourceItem) -> Path:
    title = safe_title(source.title or "douyin-image-text")
    platform_id = source.input.rstrip("/").rsplit("/", 1)[-1]
    return root / "抖音图文素材" / f"{title}--{platform_id}"
