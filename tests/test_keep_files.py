"""The server keeps each file of a job: the audio, the book and the subtitles,
together in the folder of the job. Nothing deletes them automatically. The
operator deletes them by hand."""

from __future__ import annotations

from datetime import timedelta

from backend import main, runner
from backend.db import Job, JobStatus, SessionLocal, utcnow
from backend.runner import Paths
from backend.settings import settings

from .conftest import get_job_row, make_job

SRT = "1\n00:00:00,000 --> 00:00:01,000\nhello\n\n"


def stage_inputs(job_id: str) -> tuple[Paths, list]:
    """Put an audio file and a book where an upload puts them."""
    paths = Paths.for_job(job_id)
    paths.create()
    audio = runner.staged_audio_path(job_id, "book.m4b")
    book = runner.staged_text_path(job_id, "book.epub")
    audio.write_bytes(b"audio")
    book.write_bytes(b"book")
    return paths, [audio, book]


def age(job_id: str, **delta) -> None:
    with SessionLocal() as s:
        job = s.get(Job, job_id)
        job.created_at = utcnow() - timedelta(**delta)
        job.finished_at = utcnow() - timedelta(**delta)
        s.commit()


def test_a_finished_server_job_keeps_audio_book_and_subtitles_together(client, monkeypatch):
    job_id = make_job(client, JobStatus.queued)
    paths, inputs = stage_inputs(job_id)
    produced = paths.out / "source.en.srt"

    def fake_aligner(job_id, request, log_path):
        # Bytes, not text: on Windows write_text writes CRLF line ends.
        produced.write_bytes(SRT.encode("utf-8"))
        return 0

    monkeypatch.setattr(runner, "probe_duration", lambda path: 1.0)
    monkeypatch.setattr(runner, "probe_chapters", lambda path: 0)
    monkeypatch.setattr(runner, "normalize_for_alignment", lambda job_id, source: source)
    monkeypatch.setattr(runner, "_stream_aligner", fake_aligner)
    monkeypatch.setattr(runner.aligner, "locate_output", lambda request: produced)
    monkeypatch.setattr(settings, "render_video", False)

    runner.run_job(job_id)

    assert get_job_row(job_id).status == JobStatus.succeeded
    for path in [*inputs, produced]:
        assert path.exists(), path
        assert paths.root in path.parents  # one folder for the job
    assert client.get(f"/api/jobs/{job_id}/files/srt").text == SRT


def test_housekeeping_deletes_no_file_and_no_job(client):
    failed = make_job(client, JobStatus.failed)
    draft = make_job(client)
    done = make_job(client, JobStatus.succeeded, with_files=True)
    staged = {job_id: stage_inputs(job_id)[1] for job_id in (failed, draft, done)}
    for job_id in staged:
        age(job_id, days=400)

    main.housekeeping()

    for job_id, inputs in staged.items():
        assert get_job_row(job_id) is not None
        assert all(path.exists() for path in inputs), job_id
    assert client.get(f"/api/jobs/{done}/files/srt").status_code == 200


def test_deleting_a_job_keeps_its_files(client):
    job_id = make_job(client)
    _, inputs = stage_inputs(job_id)
    assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    assert get_job_row(job_id) is None
    assert all(path.exists() for path in inputs)


def test_a_refused_upload_keeps_what_arrived(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 10)
    work = settings.data_dir / "work"
    before = set(work.iterdir()) if work.exists() else set()

    r = client.post("/api/uploads", files=[
        ("files", ("book.epub", b"epub", "application/epub+zip")),
        ("files", ("book.m4b", b"x" * 100, "audio/mp4")),
    ])
    assert r.status_code == 413

    new = set(work.iterdir()) - before
    assert len(new) == 1
    kept = {p.suffix for p in new.pop().rglob("*") if p.is_file()}
    assert kept == {".epub", ".m4b"}
