"""Rebuildable SQLite read model projected from Vault fact files."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from learnnest.batch_store import parse_batch_bytes
from learnnest.locks import rebuild_lock
from learnnest.schedule_store import parse_schedule_bytes
from learnnest.task_store import parse_task_bytes

_SCHEMA_VERSION = "1"


class IndexRebuildError(RuntimeError):
    """A strict rebuild failure that leaves the prior database untouched."""


@dataclass(frozen=True)
class IndexBuildResult:
    database_path: Path
    task_count: int
    attempt_count: int
    batch_count: int
    schedule_count: int = 0


@dataclass(frozen=True)
class IndexStatus:
    database_path: Path
    exists: bool
    stale: bool
    reason: str
    task_count: int = 0
    attempt_count: int = 0
    batch_count: int = 0
    schedule_count: int = 0


def rebuild_index(
    output_root: str | Path,
    *,
    lock_timeout: float = 0.0,
    _lock_held: bool = False,
) -> IndexBuildResult:
    root = Path(output_root).resolve()
    if not _lock_held:
        with rebuild_lock(root, timeout=lock_timeout):
            return rebuild_index(
                root,
                lock_timeout=lock_timeout,
                _lock_held=True,
            )
    index_dir = root / ".learnnest"
    index_dir.mkdir(parents=True, exist_ok=True)
    destination = index_dir / "index.sqlite3"
    temporary = index_dir / f".index-{uuid.uuid4().hex}.tmp"
    task_count = 0
    attempt_count = 0
    batch_count = 0
    schedule_count = 0
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA defer_foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.executescript(_schema_sql())
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (_SCHEMA_VERSION,),
            )
            seen_task_ids: set[str] = set()
            for task_path in _task_paths(root):
                try:
                    task, source_sha256 = _read_task_fact(task_path)
                except Exception as error:
                    raise IndexRebuildError(
                        f"{_relative(root, task_path)}: {type(error).__name__}: {error}"
                    ) from error
                if task.task_id in seen_task_ids:
                    raise IndexRebuildError(f"duplicate task_id: {task.task_id}")
                seen_task_ids.add(task.task_id)
                _insert_source_file(
                    connection,
                    root,
                    task_path,
                    "task",
                    source_sha256,
                )
                _insert_task(connection, root, task_path, task)
                task_count += 1
                attempt_count += len(task.attempts)

            seen_schedule_ids: set[str] = set()
            for schedule_path in _schedule_paths(root):
                try:
                    schedule, source_sha256 = _read_schedule_fact(schedule_path)
                except Exception as error:
                    raise IndexRebuildError(
                        f"{_relative(root, schedule_path)}: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                if schedule.schedule_id in seen_schedule_ids:
                    raise IndexRebuildError(
                        f"duplicate schedule_id: {schedule.schedule_id}"
                    )
                seen_schedule_ids.add(schedule.schedule_id)
                _insert_source_file(
                    connection,
                    root,
                    schedule_path,
                    "schedule",
                    source_sha256,
                )
                _insert_schedule(connection, root, schedule_path, schedule)
                schedule_count += 1

            for batch_path in _batch_paths(root):
                try:
                    batch, source_sha256 = _read_batch_fact(batch_path)
                except Exception as error:
                    raise IndexRebuildError(
                        f"{_relative(root, batch_path)}: {type(error).__name__}: {error}"
                    ) from error
                _insert_source_file(
                    connection,
                    root,
                    batch_path,
                    "batch",
                    source_sha256,
                )
                _insert_batch(connection, root, batch_path, batch)
                batch_count += 1

            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_errors:
                raise IndexRebuildError(
                    f"foreign key check failed: {foreign_key_errors[0]}"
                )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise IndexRebuildError(f"integrity check failed: {integrity}")
            source_count = connection.execute(
                "SELECT COUNT(*) FROM source_files"
            ).fetchone()[0]
            if source_count != task_count + batch_count + schedule_count:
                raise IndexRebuildError("source file count does not match projections")
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary, destination)
    except Exception as error:
        _cleanup_temporary_database(temporary)
        if isinstance(error, IndexRebuildError):
            raise
        raise IndexRebuildError(
            f"index rebuild failed: {type(error).__name__}: {error}"
        ) from error
    return IndexBuildResult(
        database_path=destination,
        task_count=task_count,
        attempt_count=attempt_count,
        batch_count=batch_count,
        schedule_count=schedule_count,
    )


def index_status(output_root: str | Path) -> IndexStatus:
    root = Path(output_root).resolve()
    database = root / ".learnnest" / "index.sqlite3"
    if not database.is_file():
        return IndexStatus(database, False, True, "index database is missing")
    try:
        with _connect_readonly(database) as connection:
            rows = connection.execute(
                "SELECT path, sha256 FROM source_files ORDER BY path"
            ).fetchall()
            task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            attempt_count = connection.execute(
                "SELECT COUNT(*) FROM attempts"
            ).fetchone()[0]
            batch_count = connection.execute("SELECT COUNT(*) FROM batches").fetchone()[
                0
            ]
            schedule_count = connection.execute(
                "SELECT COUNT(*) FROM schedules"
            ).fetchone()[0]
    except (OSError, sqlite3.Error) as error:
        return IndexStatus(
            database,
            True,
            True,
            f"index database is unreadable: {type(error).__name__}",
        )
    indexed = {str(row[0]): str(row[1]) for row in rows}
    current_paths = [*_task_paths(root), *_batch_paths(root), *_schedule_paths(root)]
    current = {_relative(root, path): path for path in current_paths}
    if set(indexed) != set(current):
        changed = sorted(set(indexed) ^ set(current))
        return IndexStatus(
            database,
            True,
            True,
            f"fact file set changed: {changed[0]}",
            task_count,
            attempt_count,
            batch_count,
            schedule_count,
        )
    for relative, path in current.items():
        if _sha256(path) != indexed[relative]:
            return IndexStatus(
                database,
                True,
                True,
                f"fact file changed: {relative}",
                task_count,
                attempt_count,
                batch_count,
                schedule_count,
            )
    return IndexStatus(
        database,
        True,
        False,
        "index is current",
        task_count,
        attempt_count,
        batch_count,
        schedule_count,
    )


def query_history(
    output_root: str | Path,
    *,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    database = _require_fresh_database(output_root)
    sql = """
        SELECT tasks.task_id, tasks.task_path, tasks.title,
               attempts.attempt_id, attempts.ordinal,
               attempts.reason, attempts.from_stage,
               attempts.status AS attempt_status,
               attempts.started_at, attempts.finished_at,
               attempts.failed_stage, attempts.failure_code,
               attempts.safe_summary
        FROM tasks
        LEFT JOIN attempts ON attempts.task_id = tasks.task_id
    """
    parameters: tuple[object, ...] = ()
    if task_id is not None:
        sql += " WHERE tasks.task_id = ?"
        parameters = (task_id,)
    sql += " ORDER BY tasks.task_id, attempts.ordinal"
    with _connect_readonly(database) as connection:
        return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def query_history_facts(
    output_root: str | Path,
    *,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read task/attempt history directly from authoritative task facts."""
    root = Path(output_root).resolve()
    rows: list[dict[str, Any]] = []
    for path in _task_paths(root):
        task, _digest = _read_task_fact(path)
        if task_id is not None and task.task_id != task_id:
            continue
        task_path = _relative(root, path.parent)
        if not task.attempts:
            rows.append(
                {
                    "task_id": task.task_id,
                    "task_path": task_path,
                    "title": task.title,
                    "attempt_id": None,
                    "ordinal": None,
                    "reason": None,
                    "from_stage": None,
                    "attempt_status": None,
                    "started_at": None,
                    "finished_at": None,
                    "failed_stage": None,
                    "failure_code": None,
                    "safe_summary": None,
                }
            )
            continue
        for attempt in task.attempts:
            failure = attempt.failure
            rows.append(
                {
                    "task_id": task.task_id,
                    "task_path": task_path,
                    "title": task.title,
                    "attempt_id": attempt.attempt_id,
                    "ordinal": attempt.ordinal,
                    "reason": attempt.reason,
                    "from_stage": attempt.from_stage,
                    "attempt_status": attempt.status,
                    "started_at": attempt.started_at.isoformat(),
                    "finished_at": (
                        attempt.finished_at.isoformat()
                        if attempt.finished_at is not None
                        else None
                    ),
                    "failed_stage": attempt.failed_stage,
                    "failure_code": failure.code if failure is not None else None,
                    "safe_summary": (
                        failure.safe_summary if failure is not None else None
                    ),
                }
            )
    return sorted(
        rows,
        key=lambda row: (str(row["task_id"]), row["ordinal"] or 0),
    )


def query_failure_queue(
    output_root: str | Path,
    *,
    now: datetime | None = None,
    max_stage_attempts: int = 4,
) -> list[dict[str, Any]]:
    from learnnest.execution import MAX_STAGE_ATTEMPTS, stage_attempt_count

    if not 1 <= max_stage_attempts <= MAX_STAGE_ATTEMPTS:
        raise ValueError(
            f"max_stage_attempts must be between 1 and {MAX_STAGE_ATTEMPTS}"
        )
    root = Path(output_root).resolve()
    database = _require_fresh_database(root)
    selected = (now or datetime.now(UTC)).isoformat()
    with _connect_readonly(database) as connection:
        rows = connection.execute(
            """
            SELECT * FROM failure_queue
            WHERE next_retry_at IS NULL OR next_retry_at <= ?
            ORDER BY next_retry_at, task_id, ordinal
            """,
            (selected,),
        ).fetchall()
    tasks = {
        task.task_id: task
        for task, _digest in (_read_task_fact(path) for path in _task_paths(root))
    }
    result: list[dict[str, Any]] = []
    for raw_row in rows:
        row = dict(raw_row)
        task = tasks.get(str(row["task_id"]))
        failed_stage = row.get("failed_stage")
        if (
            task is not None
            and failed_stage is not None
            and stage_attempt_count(task, failed_stage) >= max_stage_attempts
        ):
            continue
        result.append(row)
    return result


def _insert_task(
    connection: sqlite3.Connection,
    root: Path,
    task_path: Path,
    task: Any,
) -> None:
    connection.execute(
        """
        INSERT INTO tasks(
            task_id, task_path, schema_version, title, source_input,
            source_type, source_fingerprint, profile, error_summary,
            duplicate_of_task_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.task_id,
            _relative(root, task_path),
            task.schema_version,
            task.title,
            task.source_input,
            task.source_type,
            task.source_fingerprint,
            task.profile,
            task.error_summary,
            task.duplicate_of_task_id,
        ),
    )
    identities = task.identities
    if identities is None:
        raise IndexRebuildError(f"task has no identities: {task.task_id}")
    connection.execute(
        "INSERT INTO source_identities VALUES (?, ?, ?, ?, ?)",
        (
            task.task_id,
            identities.normalized_source,
            identities.platform,
            identities.platform_id,
            identities.content_sha256,
        ),
    )
    connection.executemany(
        "INSERT INTO task_stages VALUES (?, ?, ?)",
        [(task.task_id, stage, status.value) for stage, status in task.stages.items()],
    )
    connection.executemany(
        "INSERT INTO task_artifacts VALUES (?, ?, ?, ?)",
        [
            (task.task_id, stage, position, path)
            for stage, paths in task.artifacts.items()
            for position, path in enumerate(paths, start=1)
        ],
    )
    for attempt in task.attempts:
        failure = attempt.failure
        connection.execute(
            """
            INSERT INTO attempts VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                attempt.attempt_id,
                task.task_id,
                attempt.ordinal,
                attempt.reason,
                attempt.from_stage,
                attempt.status,
                attempt.started_at.isoformat(),
                attempt.finished_at.isoformat() if attempt.finished_at else None,
                attempt.failed_stage,
                attempt.batch_id,
                failure.code if failure else None,
                failure.category if failure else None,
                failure.disposition if failure else None,
                failure.safe_summary if failure else None,
                attempt.next_retry_at.isoformat() if attempt.next_retry_at else None,
            ),
        )


def _insert_batch(
    connection: sqlite3.Connection,
    root: Path,
    batch_path: Path,
    batch: Any,
) -> None:
    connection.execute(
        """
        INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch.batch_id,
            _relative(root, batch_path),
            batch.schema_version,
            batch.kind,
            batch.status,
            batch.profile,
            batch.created_at.isoformat(),
            batch.updated_at.isoformat(),
            batch.finished_at.isoformat() if batch.finished_at else None,
            batch.schedule_id,
            batch.cursor_before,
            batch.cursor_after,
        ),
    )
    for item in batch.results:
        failure = item.failure
        connection.execute(
            "INSERT INTO batch_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch.batch_id,
                item.position,
                item.input,
                item.input_type,
                item.source_fingerprint,
                item.status,
                item.task_id,
                item.attempt_id,
                item.error,
                failure.code if failure else None,
                failure.disposition if failure else None,
            ),
        )


def _insert_source_file(
    connection: sqlite3.Connection,
    root: Path,
    path: Path,
    kind: str,
    source_sha256: str,
) -> None:
    connection.execute(
        "INSERT INTO source_files(path, kind, sha256) VALUES (?, ?, ?)",
        (_relative(root, path), kind, source_sha256),
    )


def _insert_schedule(
    connection: sqlite3.Connection,
    root: Path,
    schedule_path: Path,
    schedule: Any,
) -> None:
    connection.execute(
        "INSERT INTO schedules VALUES (?, ?, ?, ?)",
        (
            schedule.schedule_id,
            _relative(root, schedule_path),
            schedule.status,
            schedule.cursor,
        ),
    )


def _read_task_fact(path: Path) -> tuple[Any, str]:
    data = path.read_bytes()
    task = parse_task_bytes(data, base_dir=path.parent)
    return task, hashlib.sha256(data).hexdigest()


def _read_batch_fact(path: Path) -> tuple[Any, str]:
    data = path.read_bytes()
    batch = parse_batch_bytes(data)
    return batch, hashlib.sha256(data).hexdigest()


def _read_schedule_fact(path: Path) -> tuple[Any, str]:
    data = path.read_bytes()
    schedule = parse_schedule_bytes(data)
    return schedule, hashlib.sha256(data).hexdigest()


def _task_paths(root: Path) -> list[Path]:
    directory = root / "视频学习素材"
    return sorted(directory.glob("*/task.json")) if directory.is_dir() else []


def _batch_paths(root: Path) -> list[Path]:
    directory = root / "视频学习批次"
    return sorted(directory.glob("*/batch.json")) if directory.is_dir() else []


def _schedule_paths(root: Path) -> list[Path]:
    directory = root / "视频学习批次" / "schedules"
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def _relative(root: Path, path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise IndexRebuildError(f"fact file is outside Vault: {path}")
    return resolved.relative_to(root).as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_sql() -> str:
    return (Path(__file__).with_name("sql") / "schema_v1.sql").read_text(
        encoding="utf-8"
    )


@contextmanager
def _connect_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        yield connection
    finally:
        connection.close()


def _require_database(output_root: str | Path) -> Path:
    database = Path(output_root).resolve() / ".learnnest" / "index.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("index database is missing; run index rebuild")
    return database


def _require_fresh_database(output_root: str | Path) -> Path:
    status = index_status(output_root)
    if not status.exists:
        raise FileNotFoundError("index database is missing; run index rebuild")
    if status.stale:
        raise IndexRebuildError(
            f"index projection is stale ({status.reason}); run index rebuild"
        )
    return status.database_path


def _cleanup_temporary_database(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass
