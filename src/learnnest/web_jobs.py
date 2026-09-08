"""Durable, secret-free operation facts for the local WebUI."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal


JobStatus = Literal["queued", "running", "completed", "failed", "interrupted"]
SourceKind = Literal["local_video", "public_url", "douyin_favorite"]


@dataclass(frozen=True)
class WebJobAttempt:
    attempt: int
    status: JobStatus
    started_at: datetime
    completed_at: datetime | None = None


@dataclass(frozen=True)
class WebJob:
    job_id: str
    source_kind: SourceKind
    source_input: str
    created_at: datetime
    status: JobStatus = "queued"
    task_id: str | None = None
    error: str | None = None
    attempts: int = 1
    attempt_history: tuple[WebJobAttempt, ...] = ()

    def public(self) -> dict[str, str | int | None]:
        return {
            "job_id": self.job_id,
            "source_kind": self.source_kind,
            "status": self.status,
            "task_id": self.task_id,
            "error": self.error,
            "attempts": self.attempts,
        }


class WebJobStore:
    """Atomic job facts; task records remain the sole content identity."""

    def __init__(self, output_root: str | Path) -> None:
        self._root = Path(output_root).resolve()
        self._lock = threading.RLock()
        self._recover_interrupted()

    def create(self, source_kind: SourceKind, source_input: str) -> WebJob:
        job = WebJob(
            job_id=uuid.uuid4().hex,
            source_kind=source_kind,
            source_input=source_input,
            created_at=datetime.now(UTC),
        )
        job = replace(
            job,
            attempt_history=(WebJobAttempt(1, "queued", job.created_at),),
        )
        with self._lock:
            self._write(job)
        return job

    def get(self, job_id: str) -> WebJob:
        path = self._path(job_id)
        with self._lock:
            return self._read(path, expected_id=job_id)

    def list(self) -> tuple[WebJob, ...]:
        with self._lock:
            jobs: list[WebJob] = []
            for path in sorted(self._directory().glob("*.json"), reverse=True):
                try:
                    jobs.append(self._read(path, expected_id=path.stem))
                except ValueError:
                    jobs.append(self._invalid_public_job(path.stem))
            return tuple(jobs)

    def find_by_source(
        self, source_kind: SourceKind, source_input: str
    ) -> WebJob | None:
        """Return the most recent recoverable intent for one canonical source.

        A queued, running, or interrupted source job is the durable intent for
        its canonical URL; it is reused instead of creating a duplicate.
        Completed and failed intents are left to the existing retry path so an
        automatic sync never silently restarts user-visible failures.
        """
        with self._lock:
            found: WebJob | None = None
            for path in sorted(self._directory().glob("*.json"), reverse=True):
                try:
                    job = self._read(path, expected_id=path.stem)
                except ValueError:
                    continue
                if (
                    job.source_kind == source_kind
                    and job.source_input == source_input
                    and job.status in {"queued", "running", "interrupted"}
                    and (found is None or job.created_at >= found.created_at)
                ):
                    found = job
            return found

    def start(self, job_id: str) -> WebJob:
        return self._replace_attempt(job_id, "running", error=None)

    def complete(self, job_id: str, task_id: str) -> WebJob:
        return self._replace_attempt(job_id, "completed", task_id=task_id, error=None)

    def fail(self, job_id: str, error: str) -> WebJob:
        return self._replace_attempt(job_id, "failed", error=error)

    def retry(self, job_id: str) -> WebJob:
        with self._lock:
            job = self.get(job_id)
            if job.status not in {"failed", "interrupted"}:
                raise ValueError("source job cannot be retried")
            updated = replace(
                job,
                status="queued",
                error=None,
                attempts=job.attempts + 1,
                attempt_history=(
                    *job.attempt_history,
                    WebJobAttempt(job.attempts + 1, "queued", datetime.now(UTC)),
                ),
            )
            self._write(updated)
            return updated

    def _replace_attempt(
        self, job_id: str, status: JobStatus, **updates: object
    ) -> WebJob:
        with self._lock:
            job = self.get(job_id)
            if not job.attempt_history:
                raise ValueError("web job attempt history is invalid")
            latest = job.attempt_history[-1]
            now = datetime.now(UTC)
            updated = replace(
                job,
                status=status,
                attempt_history=(
                    *job.attempt_history[:-1],
                    replace(
                        latest,
                        status=status,
                        completed_at=(None if status == "running" else now),
                    ),
                ),
                **updates,
            )
            self._write(updated)
            return updated

    def _recover_interrupted(self) -> None:
        directory = self._directory()
        if not directory.is_dir():
            return
        with self._lock:
            for path in directory.glob("*.json"):
                try:
                    job = self._read(path, expected_id=path.stem)
                except ValueError:
                    continue
                if job.status in {"queued", "running"}:
                    self._write(self._interrupted(job))

    def _interrupted(self, job: WebJob) -> WebJob:
        if not job.attempt_history:
            raise ValueError("web job attempt history is invalid")
        return replace(
            job,
            status="interrupted",
            error="处理已中断，请确认后重试。",
            attempt_history=(
                *job.attempt_history[:-1],
                replace(
                    job.attempt_history[-1],
                    status="interrupted",
                    completed_at=datetime.now(UTC),
                ),
            ),
        )

    def _directory(self) -> Path:
        path = self._root / ".learnnest" / "web" / "jobs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _invalid_public_job(self, job_id: str) -> WebJob:
        now = datetime.now(UTC)
        return WebJob(
            job_id="0" * 32,
            source_kind="local_video",
            source_input="",
            created_at=now,
            status="failed",
            error="来源操作记录无法读取。",
            attempt_history=(WebJobAttempt(1, "failed", now, now),),
        )

    def _path(self, job_id: str) -> Path:
        if not job_id or Path(job_id).name != job_id or len(job_id) != 32:
            raise KeyError(job_id)
        return self._directory() / f"{job_id}.json"

    def _read(self, path: Path, *, expected_id: str) -> WebJob:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("job_id") != expected_id:
                raise ValueError("job identity is invalid")
            job = WebJob(
                job_id=str(data["job_id"]),
                source_kind=data["source_kind"],
                source_input=str(data["source_input"]),
                created_at=datetime.fromisoformat(str(data["created_at"])),
                status=data["status"],
                task_id=data.get("task_id"),
                error=data.get("error"),
                attempts=int(data["attempts"]),
                attempt_history=tuple(
                    WebJobAttempt(
                        attempt=int(item["attempt"]),
                        status=item["status"],
                        started_at=datetime.fromisoformat(str(item["started_at"])),
                        completed_at=(
                            None
                            if item.get("completed_at") is None
                            else datetime.fromisoformat(str(item["completed_at"]))
                        ),
                    )
                    for item in data.get(
                        "attempt_history",
                        [
                            {
                                "attempt": data["attempts"],
                                "status": data["status"],
                                "started_at": data["created_at"],
                                "completed_at": None,
                            }
                        ],
                    )
                ),
            )
            if (
                job.source_kind not in {"local_video", "public_url", "douyin_favorite"}
                or job.status
                not in {"queued", "running", "completed", "failed", "interrupted"}
                or job.created_at.tzinfo is None
                or job.attempts < 1
                or len(job.attempt_history) != job.attempts
                or [item.attempt for item in job.attempt_history]
                != list(range(1, job.attempts + 1))
                or any(
                    item.status
                    not in {"queued", "running", "completed", "failed", "interrupted"}
                    or item.started_at.tzinfo is None
                    or (
                        item.completed_at is not None
                        and item.completed_at.tzinfo is None
                    )
                    for item in job.attempt_history
                )
            ):
                raise ValueError("job fact is invalid")
            return job
        except (OSError, UnicodeError, KeyError, TypeError, ValueError) as error:
            raise ValueError("web job is missing or invalid") from error

    def _write(self, job: WebJob) -> None:
        path = self._path(job.job_id)
        payload = {
            "job_id": job.job_id,
            "source_kind": job.source_kind,
            "source_input": job.source_input,
            "created_at": job.created_at.astimezone(UTC).isoformat(),
            "status": job.status,
            "task_id": job.task_id,
            "error": job.error,
            "attempts": job.attempts,
            "attempt_history": [
                {
                    "attempt": item.attempt,
                    "status": item.status,
                    "started_at": item.started_at.astimezone(UTC).isoformat(),
                    "completed_at": (
                        None
                        if item.completed_at is None
                        else item.completed_at.astimezone(UTC).isoformat()
                    ),
                }
                for item in job.attempt_history
            ],
        }
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
