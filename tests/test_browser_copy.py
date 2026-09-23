"""During the beta, a job in the browser also sends a copy of its audio and its
book to the server, as the privacy and user data policy says (terms.html,
"Input data kept for debugging"). The server keeps the copy with the
subtitles, in the folder of the job. A job in the browser never spends a
credit, with or without the copy.
"""

from __future__ import annotations

from backend.runner import Paths
from backend.settings import settings

from .conftest import make_job

BOOK = {
    "audio_filename": "copy.m4b", "audio_bytes": 11,
    "text_filename": "copy.epub", "language": "en",
}
SRT = "1\n00:00:00,000 --> 00:00:01,000\nhello\n\n"


def begin(client) -> dict:
    r = client.post("/api/local/jobs", json=BOOK)
    assert r.status_code == 200
    return r.json()


def send_copy(client, job_id: str, files: list[tuple[str, bytes]]):
    return client.post(
        f"/api/local/jobs/{job_id}/files",
        files=[("files", (name, data, "application/octet-stream")) for name, data in files],
    )


def test_the_page_is_told_to_send_a_copy(client):
    assert client.get("/api/account").json()["browser_copy"] is True


def test_a_browser_job_keeps_audio_book_and_subtitles_together(client):
    job = begin(client)
    r = send_copy(client, job["id"], [("copy.m4b", b"audio"), ("copy.epub", b"book")])
    assert r.status_code == 200 and r.json() == {"saved": 2}

    done = client.post(f"/api/local/jobs/{job['id']}/finish",
                       json={"srt": SRT, "filename": "copy.en.srt"})
    assert done.status_code == 200

    root = Paths.for_job(job["id"]).root
    assert (root / "input" / "copy.m4b").read_bytes() == b"audio"
    assert (root / "input" / "copy.epub").read_bytes() == b"book"
    assert (root / "copy.en.srt").read_bytes() == SRT.encode("utf-8")


def test_a_browser_job_and_its_copy_spend_no_credit(client, monkeypatch):
    monkeypatch.setattr(settings, "cloud_enabled", True)  # the free credit exists
    before = client.get("/api/account").json()["credits"]
    job = begin(client)
    assert send_copy(client, job["id"], [("copy.m4b", b"audio")]).status_code == 200
    assert client.get("/api/account").json()["credits"] == before == 1


def test_only_the_owner_can_send_a_copy(client, second_client):
    job = begin(client)
    assert send_copy(second_client, job["id"], [("copy.m4b", b"x")]).status_code == 404


def test_a_file_name_cannot_leave_the_folder_of_the_job(client):
    job = begin(client)
    r = send_copy(client, job["id"], [("../../escape.m4b", b"x"), ("..\\..\\win.epub", b"y")])
    assert r.status_code == 200
    inp = Paths.for_job(job["id"]).inp
    assert (inp / "escape.m4b").read_bytes() == b"x"
    assert (inp / "win.epub").read_bytes() == b"y"
    assert not (inp.parent.parent / "escape.m4b").exists()


def test_no_copy_when_the_beta_setting_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "browser_copy", False)
    assert client.get("/api/account").json()["browser_copy"] is False
    job = begin(client)
    assert send_copy(client, job["id"], [("copy.m4b", b"x")]).status_code == 404


def test_a_server_job_takes_no_copy(client):
    job_id = make_job(client)  # a server job: its files came with the upload
    assert send_copy(client, job_id, [("copy.m4b", b"x")]).status_code == 409
