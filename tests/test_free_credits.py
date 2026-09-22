"""Free credits for conversions on the server.

The rules under test:

  anonymous  one free credit, while the server takes conversions
  account    ten free credits in all: a free credit that a device used before
             sign-in counts toward the ten
  order      a server job spends a free credit first, then a bought one
  refund     a failed job gives its free credit back to the free credits
  merge      a second device that signs in adds no free credits
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest

from backend import runner
from backend.settings import Settings, settings

from .conftest import (
    account_id, checkout_event, get_account_row, get_job_row, make_job,
    post_webhook,
)

BOOK = {
    "audio_filename": "free.m4b", "audio_bytes": 4321,
    "text_filename": "free.epub", "language": "en",
}


@pytest.fixture
def cloud(monkeypatch):
    """The server takes conversions, and sign-in links come back in the response."""
    monkeypatch.setattr(settings, "cloud_enabled", True)
    monkeypatch.setattr(settings, "dev_login_links", True)


def unique(name: str) -> str:
    """An email for one test only: an email joins accounts across tests."""
    return f"{name}-{time.time_ns()}@example.com"


def sign_in(client, email: str) -> None:
    link = client.post("/api/auth/request", json={"email": email}).json()["dev_link"]
    token = parse_qs(urlparse(link).query)["login"][0]
    assert client.post("/api/auth/verify", json={"token": token}).status_code == 200


def buy(client, plan: str, email: str) -> None:
    event = checkout_event(f"cs_{time.time_ns()}", account_id(client), plan, email=email)
    assert post_webhook(client, event).status_code == 200


def start(client, job_id: str):
    return client.post(f"/api/jobs/{job_id}/start", json={"language": "en"})


def account(client) -> dict:
    return client.get("/api/account").json()


def test_defaults_are_one_free_credit_and_ten_with_an_account():
    assert Settings.model_fields["free_credits_anonymous"].default == 1
    assert Settings.model_fields["free_credits_signed_in"].default == 10


def test_a_new_visitor_has_one_free_credit(client, cloud):
    a = account(client)
    assert a["signed_in"] is False
    assert a["credits"] == 1 and a["free_credits"] == 1
    assert a["cloud_allowed"] is True
    assert a["free_credits_with_account"] == 10


def test_the_free_credit_pays_for_one_server_job(client, cloud):
    job_id = make_job(client)
    assert start(client, job_id).status_code == 200
    job = get_job_row(job_id)
    assert job.free_credit_spent == 1 and job.credit_spent == 0

    a = account(client)
    assert a["credits"] == 0 and a["cloud_allowed"] is False
    assert start(client, make_job(client)).status_code == 402


def test_signing_in_raises_the_free_credits_to_ten_in_all(client, cloud):
    assert start(client, make_job(client)).status_code == 200  # the anonymous credit
    assert account(client)["free_credits_with_account"] == 9

    sign_in(client, unique("topup"))
    a = account(client)
    assert a["signed_in"] is True
    assert a["credits"] == 9 and a["free_credits"] == 9
    assert a["free_credits_with_account"] == 0


def test_free_credits_are_spent_before_bought_ones(client, cloud):
    buy(client, "single", unique("order"))
    assert account(client)["credits"] == 11  # 1 bought, 10 free

    job_id = make_job(client)
    assert start(client, job_id).status_code == 200
    assert get_job_row(job_id).free_credit_spent == 1
    row = get_account_row(account_id(client))
    assert row.purchased_credits == 1 and row.free_credits_used == 1


def test_a_failed_job_gives_the_free_credit_back(client, cloud):
    job_id = make_job(client)
    assert start(client, job_id).status_code == 200
    runner.run_job(job_id)  # fails: nothing was staged

    assert account(client)["free_credits"] == 1
    assert get_job_row(job_id).free_credit_spent == 0
    assert get_account_row(account_id(client)).purchased_credits == 0


def test_a_second_device_adds_no_free_credits(client, second_client, cloud):
    email = unique("merge")
    sign_in(client, email)
    assert account(client)["free_credits"] == 10

    # The second device spends its anonymous credit, then signs in as the same person.
    assert start(second_client, make_job(second_client)).status_code == 200
    sign_in(second_client, email)
    assert account(second_client)["free_credits"] == 9
    assert account(client)["free_credits"] == 9

    # A new cookie that signs in again brings no free credit along either.
    client.post("/api/auth/signout")
    assert account(client)["free_credits"] == 1
    sign_in(client, email)
    assert account(client)["free_credits"] == 9


def test_no_free_credits_while_the_server_takes_no_conversions(client):
    # conftest leaves SUBPLZ_WEB_CLOUD_ENABLED off.
    a = account(client)
    assert a["credits"] == 0 and a["free_credits"] == 0
    assert a["free_credits_with_account"] == 0
    assert start(client, make_job(client)).status_code == 402


def test_browser_jobs_leave_the_free_credit_alone(client, cloud):
    r = client.post("/api/local/jobs", json=BOOK)
    assert r.status_code == 200 and r.json()["tier"] == "free"
    assert account(client)["free_credits"] == 1
