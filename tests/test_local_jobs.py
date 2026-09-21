"""Jobs that run in the visitor's browser: the server only keeps the books."""

from __future__ import annotations

import time
from datetime import timedelta

from backend import api
from backend.db import Job, JobStatus, SessionLocal, utcnow

from .conftest import account_id, checkout_event, get_job_row, post_webhook

BOOK = {
    "audio_filename": "book.m4b", "audio_bytes": 123456, "audio_duration_seconds": 3600.0,
    "text_filename": "book.epub", "language": "ja",
}
SRT = "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n\n"


def begin(client, **over):
    return client.post("/api/local/jobs", json={**BOOK, **over})


def buy(client, plan="single", email="local@example.com"):
    sid = f"cs_{time.time_ns()}"
    assert post_webhook(client, checkout_event(sid, account_id(client), plan, email=email)).status_code == 200


def test_free_job_runs_finishes_and_keeps_its_subtitles(client):
    r = begin(client)
    assert r.status_code == 200
    job = r.json()
    assert job["local"] is True and job["status"] == "running" and job["tier"] == "free"

    done = client.post(f"/api/local/jobs/{job['id']}/finish",
                       json={"srt": SRT, "filename": "book.ja.srt", "metadata": {"cues": 1}})
    assert done.status_code == 200 and done.json()["status"] == "succeeded"
    kinds = {a["kind"] for a in done.json()["artifacts"]}
    assert kinds == {"srt", "metadata"}

    got = client.get(f"/api/jobs/{job['id']}/files/srt")
    assert got.status_code == 200 and got.content.decode("utf-8") == SRT
    assert job["id"] in {j["id"] for j in client.get("/api/jobs").json()}


def test_free_window_applies_to_browser_jobs_too(client):
    assert begin(client).status_code == 200
    second = begin(client, audio_filename="another.m4b", audio_bytes=999)
    assert second.status_code == 402


def test_reopening_the_same_book_does_not_charge_twice(client):
    first = begin(client).json()
    again = begin(client)          # closed tab, same files
    assert again.status_code == 200 and again.json()["id"] == first["id"]
    with SessionLocal() as s:
        assert s.query(Job).filter(Job.account_id == account_id(client)).count() == 1


def test_youtube_tier_costs_a_credit_and_a_failure_returns_it(client):
    assert begin(client, tier="youtube").status_code == 402

    buy(client)
    r = begin(client, tier="youtube")
    assert r.status_code == 200 and r.json()["tier"] == "youtube"
    assert client.get("/api/account").json()["credits"] == 0

    failed = client.post(f"/api/local/jobs/{r.json()['id']}/fail", json={"error": "GPU lost"})
    assert failed.status_code == 200 and failed.json()["status"] == "failed"
    assert client.get("/api/account").json()["credits"] == 1
    # And the free book was never touched.
    assert client.get("/api/account").json()["free_remaining"] == 1


def test_upgrading_a_running_free_job_to_youtube(client):
    job = begin(client).json()
    assert begin(client, tier="youtube").status_code == 402     # no credit yet
    buy(client, email="upgrade@example.com")
    up = begin(client, tier="youtube")
    assert up.status_code == 200 and up.json()["id"] == job["id"] and up.json()["tier"] == "youtube"
    assert client.get("/api/account").json()["credits"] == 0


def test_finish_is_once_only_and_owner_only(client, second_client):
    job = begin(client).json()
    body = {"srt": SRT, "filename": "x.srt"}
    assert second_client.post(f"/api/local/jobs/{job['id']}/finish", json=body).status_code == 404
    assert client.post(f"/api/local/jobs/{job['id']}/finish", json=body).status_code == 200
    assert client.post(f"/api/local/jobs/{job['id']}/finish", json=body).status_code == 409
    assert client.post(f"/api/local/jobs/{job['id']}/fail", json={}).status_code == 409


def test_filename_cannot_escape_the_job_directory(client):
    job = begin(client).json()
    r = client.post(f"/api/local/jobs/{job['id']}/finish",
                    json={"srt": SRT, "filename": "../../../etc/passwd"})
    assert r.status_code == 200
    srt = next(a for a in r.json()["artifacts"] if a["kind"] == "srt")
    assert srt["filename"] == "passwd"


def test_abandoned_jobs_release_what_they_held(client):
    job = begin(client).json()
    assert begin(client, audio_filename="b.m4b", audio_bytes=2).status_code == 402
    with SessionLocal() as s:
        row = s.get(Job, job["id"])
        row.created_at = utcnow() - timedelta(hours=72)
        s.commit()
        assert api.expire_stale_local_jobs(s) >= 1
    assert get_job_row(job["id"]).status == JobStatus.canceled
    assert begin(client, audio_filename="b.m4b", audio_bytes=2).status_code == 200


def test_restart_does_not_queue_browser_jobs(client, monkeypatch):
    from backend import main

    job = begin(client).json()
    queued = []
    monkeypatch.setattr(main.queue, "enqueue", queued.append)
    main._requeue_interrupted()
    assert job["id"] not in queued
    assert get_job_row(job["id"]).status == JobStatus.running
