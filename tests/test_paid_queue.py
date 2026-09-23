"""Burst workers take paid jobs only.

Each server job goes to the paid queue or to the free queue. The web server's
own worker takes both, paid jobs first. A burst worker takes paid jobs only, so
free credits never start a machine that costs money.
"""

from __future__ import annotations

import time
from datetime import date

import fakeredis
import pytest
from rq import Queue

import worker
from backend import api, billing, main
from backend import queue as jobqueue
from backend.db import Job, JobStatus, SessionLocal
from backend.settings import settings

from .conftest import account_id, checkout_event, make_job, post_webhook


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr(settings, "cloud_enabled", True)


@pytest.fixture
def sent(monkeypatch):
    """What the API put on the queue: (job id, paid)."""
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(api.queue, "enqueue", lambda job_id, paid=True: calls.append((job_id, paid)))
    return calls


def start(client, job_id: str):
    return client.post(f"/api/jobs/{job_id}/start", json={"language": "en"})


@pytest.mark.parametrize("fields, paid", [
    ({"credit_spent": 1}, True),  # a bought credit
    ({"plan_credit_spent": 1}, True),  # a book of a monthly plan
    ({}, True),  # an unlimited plan spends no credit
    ({"free_credit_spent": 1}, False),
    ({"daily_credit_on": date(2026, 9, 22)}, False),
    ({"local": 1}, False),  # a browser job never goes to a worker
])
def test_is_paid(fields, paid):
    job = Job(**{"local": 0, "credit_spent": 0, "plan_credit_spent": 0,
                 "free_credit_spent": 0, "daily_credit_on": None, **fields})
    assert billing.is_paid(job) is paid


def test_a_free_credit_job_goes_to_the_free_queue(client, cloud, sent):
    job_id = make_job(client, with_files=True)
    assert start(client, job_id).status_code == 200
    assert sent == [(job_id, False)]


def test_a_bought_credit_job_goes_to_the_paid_queue(client, cloud, sent):
    assert start(client, make_job(client, with_files=True)).status_code == 200  # the free one
    event = checkout_event(f"cs_{time.time_ns()}", account_id(client), "pack10",
                           email=f"paid-{time.time_ns()}@example.com")
    assert post_webhook(client, event).status_code == 200
    job_id = make_job(client, with_files=True)
    assert start(client, job_id).status_code == 200
    assert sent[-1] == (job_id, True)


def test_the_redis_queue_keeps_paid_and_free_jobs_apart(monkeypatch):
    conn = fakeredis.FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", lambda url: conn)
    q = jobqueue.RedisQueue("redis://unused")
    q.enqueue("job_paid", paid=True)
    q.enqueue("job_free", paid=False)
    assert [j.args for j in Queue(jobqueue.PAID_QUEUE, connection=conn).jobs] == [("job_paid",)]
    assert [j.args for j in Queue(jobqueue.FREE_QUEUE, connection=conn).jobs] == [("job_free",)]
    assert q.depth() == 2


def test_a_job_whose_worker_dies_gets_one_more_attempt(monkeypatch):
    # A spot machine can vanish during a job. rq puts such a job back on its
    # queue while the job has retries left: one retry, so two attempts at most.
    conn = fakeredis.FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", lambda url: conn)
    jobqueue.RedisQueue("redis://unused").enqueue("job_1", paid=True)
    assert Queue(jobqueue.PAID_QUEUE, connection=conn).jobs[0].retries_left == 1


def test_the_web_server_worker_takes_paid_jobs_first(monkeypatch):
    monkeypatch.delenv("SUBPLZ_WEB_WORKER_QUEUES", raising=False)
    assert worker.queue_names() == [jobqueue.PAID_QUEUE, jobqueue.FREE_QUEUE]


def test_a_burst_worker_takes_paid_jobs_only(monkeypatch):
    monkeypatch.setenv("SUBPLZ_WEB_WORKER_QUEUES", "paid")
    assert worker.queue_names() == [jobqueue.PAID_QUEUE]


def test_an_unknown_queue_is_refused(monkeypatch):
    monkeypatch.setenv("SUBPLZ_WEB_WORKER_QUEUES", "paid,vip")
    with pytest.raises(ValueError, match="vip"):
        worker.queue_names()


def _running_job(client) -> str:
    job_id = make_job(client, JobStatus.running)
    with SessionLocal() as s:
        s.get(Job, job_id).credit_spent = 1
        s.commit()
    return job_id


def test_a_restart_with_the_redis_queue_leaves_running_jobs_alone(client, sent, monkeypatch):
    # With Redis, a worker on another machine still runs the job. Putting it on
    # the queue again would convert the book twice.
    job_id = _running_job(client)
    monkeypatch.setattr(settings, "queue_backend", "redis")
    main._requeue_interrupted()
    assert sent == []
    with SessionLocal() as s:
        assert s.get(Job, job_id).status == JobStatus.running


def test_a_restart_with_the_memory_queue_runs_interrupted_jobs_again(client, sent, monkeypatch):
    job_id = _running_job(client)
    monkeypatch.setattr(settings, "queue_backend", "memory")
    main._requeue_interrupted()
    assert (job_id, True) in sent
