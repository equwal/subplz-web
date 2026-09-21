"""Uploaded books and results do not stay on the server past their time."""

from __future__ import annotations

from datetime import timedelta

from backend import retention
from backend.db import Artifact, Job, JobStatus, SessionLocal, utcnow
from backend.runner import Paths
from backend.storage import storage

from .conftest import get_job_row, make_job


def age(job_id: str, **delta) -> None:
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        job.created_at = utcnow() - timedelta(**delta)
        job.finished_at = utcnow() - timedelta(**delta)
        s.commit()


def stage_inputs(job_id: str):
    paths = Paths.for_job(job_id)
    paths.create()
    (paths.inp / "book.m4b").write_bytes(b"audio")
    return paths


def sweep() -> dict:
    with SessionLocal() as s:
        return retention.sweep(s)


def test_a_failed_job_keeps_its_upload_for_a_day_and_no_longer(client):
    job_id = make_job(client, JobStatus.failed)
    paths = stage_inputs(job_id)

    age(job_id, hours=23)
    sweep()
    assert (paths.inp / "book.m4b").exists()        # still there for another try

    age(job_id, hours=25)
    assert sweep()["inputs"] == 1
    assert not paths.root.exists()
    assert get_job_row(job_id).status == JobStatus.failed   # the list still shows it


def test_an_upload_that_was_never_started_goes_away_with_its_job(client):
    job_id = make_job(client)                       # a draft
    paths = stage_inputs(job_id)
    age(job_id, hours=25)
    assert sweep()["drafts"] == 1
    assert not paths.root.exists() and get_job_row(job_id) is None


def test_results_are_deleted_after_a_week_and_the_job_says_so(client):
    fresh = make_job(client, JobStatus.succeeded, with_files=True)
    old = make_job(client, JobStatus.succeeded, with_files=True)
    age(fresh, days=6)
    age(old, days=8)

    assert sweep()["results"] == 1
    assert client.get(f"/api/jobs/{fresh}/files/srt").status_code == 200
    assert client.get(f"/api/jobs/{old}/files/srt").status_code == 404
    job = client.get(f"/api/jobs/{old}").json()
    assert job["artifacts"] == [] and "deleted after 7 days" in job["stage"]
    with SessionLocal() as s:
        assert s.query(Artifact).filter(Artifact.job_id == old).count() == 0
    assert not storage.exists(f"{old}/book.en.srt")

    assert sweep() == {"inputs": 0, "drafts": 0, "results": 0}      # and once is enough


def test_running_jobs_are_left_alone(client):
    job_id = make_job(client, JobStatus.running)
    paths = stage_inputs(job_id)
    age(job_id, days=30)
    sweep()
    assert (paths.inp / "book.m4b").exists()
