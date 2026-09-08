from __future__ import annotations

import json

import pytest

from learnnest.web_jobs import WebJobStore


def test_web_jobs_survive_restart_and_running_jobs_become_interrupted(tmp_path) -> None:
    jobs = WebJobStore(tmp_path)
    running = jobs.create("public_url", "https://example.test/watch?private=value")
    jobs.start(running.job_id)

    restored = WebJobStore(tmp_path).get(running.job_id)

    assert restored.status == "interrupted"
    assert restored.error == "处理已中断，请确认后重试。"
    assert "source_input" not in restored.public()


def test_web_job_retry_keeps_identity_and_appends_attempt(tmp_path) -> None:
    jobs = WebJobStore(tmp_path)
    job = jobs.create("local_video", "C:/private/lesson.mp4")
    jobs.fail(job.job_id, "处理暂时无法继续。")

    retried = jobs.retry(job.job_id)

    assert retried.job_id == job.job_id
    assert retried.status == "queued"
    assert retried.attempts == 2
    assert [item.status for item in retried.attempt_history] == ["failed", "queued"]
    with pytest.raises(ValueError):
        jobs.retry(job.job_id)


def test_web_job_rejects_tampered_identity_and_malformed_fact(tmp_path) -> None:
    jobs = WebJobStore(tmp_path)
    job = jobs.create("douyin_favorite", "https://www.douyin.com/video/123")
    path = tmp_path / ".learnnest" / "web" / "jobs" / f"{job.job_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["job_id"] = "f" * 32
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        jobs.get(job.job_id)
    with pytest.raises(KeyError):
        jobs.get("../" + job.job_id)
    public = jobs.list()[0].public()
    assert public["status"] == "failed"
    assert public["error"] == "来源操作记录无法读取。"
    assert "douyin" not in str(public)


def test_web_jobs_find_by_source_reuses_only_recoverable_intents(tmp_path) -> None:
    jobs = WebJobStore(tmp_path)
    url_one = "https://www.douyin.com/video/1234567890123456789"
    url_two = "https://www.douyin.com/video/2234567890123456789"

    assert jobs.find_by_source("douyin_favorite", url_one) is None

    created = jobs.create("douyin_favorite", url_one)
    assert jobs.find_by_source("douyin_favorite", url_one).job_id == created.job_id
    running = jobs.start(created.job_id)
    assert jobs.find_by_source("douyin_favorite", url_one).job_id == running.job_id
    interrupted = WebJobStore(tmp_path).get(created.job_id)
    assert interrupted.status == "interrupted"
    assert jobs.find_by_source("douyin_favorite", url_one).job_id == interrupted.job_id

    completed = jobs.create("douyin_favorite", url_two)
    jobs.complete(completed.job_id, "20260908-task-2")
    assert jobs.find_by_source("douyin_favorite", url_two) is None

    jobs.create("public_url", url_one)
    assert jobs.find_by_source("douyin_favorite", url_one).job_id == interrupted.job_id
