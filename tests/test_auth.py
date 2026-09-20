"""Email sign-in links."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from backend import mailer
from backend.db import JobStatus, LoginToken, SessionLocal, utcnow
from backend.settings import settings

from .conftest import account_id, get_job_row, make_job


@pytest.fixture
def outbox(monkeypatch):
    """Pretend SMTP is configured, and catch what would have been sent."""
    sent = []
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(mailer, "send_login_link",
                        lambda to, url: sent.append((to, url)) or True)
    return sent


def token_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["login"][0]


def sign_in(client, outbox, email):
    assert client.post("/api/auth/request", json={"email": email}).status_code == 200
    return client.post("/api/auth/verify", json={"token": token_of(outbox[-1][1])})


def test_link_signs_the_browser_in(client, outbox):
    r = client.post("/api/auth/request", json={"email": "  New@Example.com "})
    assert r.status_code == 200
    assert r.json() == {"sent": True, "email": "new@example.com"}  # no link leaked
    to, url = outbox[-1]
    assert to == "new@example.com"
    assert url.startswith("https://example.test/?login=")

    r = client.post("/api/auth/verify", json={"token": token_of(url)})
    assert r.status_code == 200
    assert r.json()["signed_in"] is True and r.json()["email"] == "new@example.com"
    assert client.get("/api/account").json()["signed_in"] is True


def test_link_works_once(client, outbox):
    assert sign_in(client, outbox, "once@example.com").status_code == 200
    again = client.post("/api/auth/verify", json={"token": token_of(outbox[-1][1])})
    assert again.status_code == 400


def test_expired_link_is_refused(client, outbox):
    client.post("/api/auth/request", json={"email": "late@example.com"})
    with SessionLocal() as s:
        row = s.query(LoginToken).filter(LoginToken.email == "late@example.com").one()
        row.expires_at = utcnow() - timedelta(minutes=1)
        s.commit()
    r = client.post("/api/auth/verify", json={"token": token_of(outbox[-1][1])})
    assert r.status_code == 400
    assert client.get("/api/account").json()["signed_in"] is False


def test_garbage_token_is_refused(client):
    assert client.post("/api/auth/verify", json={"token": "nope"}).status_code == 400


def test_bad_email_is_refused(client, outbox):
    for bad in ["", "no-at-sign", "a@b", "two words@x.com"]:
        assert client.post("/api/auth/request", json={"email": bad}).status_code == 400
    assert outbox == []


def test_second_device_lands_in_the_same_account_and_keeps_its_work(
    client, second_client, outbox
):
    assert sign_in(client, outbox, "both@example.com").status_code == 200
    original = account_id(client)

    job_id = make_job(second_client, JobStatus.succeeded)
    assert sign_in(second_client, outbox, "both@example.com").status_code == 200

    assert account_id(second_client) == original
    assert get_job_row(job_id).account_id == original


def test_sign_out_forgets_the_browser_but_not_the_account(client, outbox):
    assert sign_in(client, outbox, "bye@example.com").status_code == 200
    original = account_id(client)
    assert client.post("/api/auth/signout").status_code == 200

    fresh = client.get("/api/account").json()
    assert fresh["signed_in"] is False and fresh["id"] != original

    assert sign_in(client, outbox, "bye@example.com").status_code == 200
    assert account_id(client) == original


def test_signing_in_as_someone_else_switches_rather_than_merges(client, outbox):
    assert sign_in(client, outbox, "first@example.com").status_code == 200
    first = account_id(client)
    job_id = make_job(client, JobStatus.succeeded)

    assert sign_in(client, outbox, "second@example.com").status_code == 200
    assert account_id(client) != first
    assert get_job_row(job_id).account_id == first  # stayed with its owner


def test_requests_are_rate_limited(client, outbox):
    codes = [
        client.post("/api/auth/request", json={"email": "flood@example.com"}).status_code
        for _ in range(7)
    ]
    assert codes[:5] == [200] * 5 and set(codes[5:]) == {429}


@pytest.mark.parametrize("billing", [True, False])
def test_server_without_smtp_refuses_instead_of_leaking_the_link(
    client, monkeypatch, billing
):
    # No mail server: showing the link instead would let anyone sign in as
    # anyone. Billing being off must not soften this - a public server can
    # perfectly well have billing off.
    monkeypatch.setattr(settings, "billing_enabled", billing)
    r = client.post("/api/auth/request", json={"email": "x@example.com"})
    assert r.status_code == 503 and "dev_link" not in r.text
    assert client.get("/api/account").json()["email_sign_in_available"] is False


def test_dev_mode_hands_the_link_back_only_when_asked_to(client, monkeypatch):
    monkeypatch.setattr(settings, "dev_login_links", True)
    r = client.post("/api/auth/request", json={"email": "dev@example.com"})
    assert r.status_code == 200 and r.json()["sent"] is False
    link = r.json()["dev_link"]
    assert client.post("/api/auth/verify",
                       json={"token": token_of(link)}).json()["signed_in"] is True
