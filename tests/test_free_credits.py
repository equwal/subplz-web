"""Free credits for conversions on the server.

The rules under test:

  anonymous  one free credit, while the server takes conversions. A buyer
             whose email came from Stripe is in this group until the email
             is verified.
  verified   an account that signed in with an emailed link gets two free
             credits in all (a free credit that a device used before counts
             toward the two), one more each day, and one more for every two
             books that the server converted for it
  daily      the daily credit does not carry over to the next day
  order      a server job spends the daily credit, then a free credit, then
             a bought one
  refund     a failed job gives its credit back to the same kind
  merge      a second device that signs in adds no free credits
"""

from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from backend import billing, runner
from backend.db import JobStatus
from backend.settings import Settings, settings

from .conftest import (
    account_id, checkout_event, get_account_row, get_job_row, make_job,
    post_webhook,
)

BOOK = {
    "audio_filename": "free.m4b", "audio_bytes": 4321,
    "text_filename": "free.epub", "language": "en",
}
SRT = "1\n00:00:00,000 --> 00:00:01,000\nhello\n\n"


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


def days_later(monkeypatch, days: int) -> None:
    later = billing._today() + timedelta(days=days)
    monkeypatch.setattr(billing, "_today", lambda: later)


def convert_in_browser(client, n: int) -> None:
    job = client.post("/api/local/jobs", json={**BOOK, "audio_bytes": 100 + n}).json()
    r = client.post(f"/api/local/jobs/{job['id']}/finish",
                    json={"srt": SRT, "filename": "free.srt"})
    assert r.status_code == 200


def test_defaults():
    fields = Settings.model_fields
    assert fields["free_credits_anonymous"].default == 1
    assert fields["free_credits_verified"].default == 2
    assert fields["daily_free_credit"].default is True
    assert fields["books_per_bonus_credit"].default == 2


def test_a_new_visitor_has_one_free_credit(client, cloud):
    a = account(client)
    assert a["signed_in"] is False and a["email_verified"] is False
    assert a["credits"] == 1 and a["free_credits"] == 1
    assert a["cloud_allowed"] is True
    assert a["free_credits_with_account"] == 2 and a["daily_free_credit"] is True


def test_the_free_credit_pays_for_one_server_job(client, cloud):
    job_id = make_job(client)
    assert start(client, job_id).status_code == 200
    job = get_job_row(job_id)
    assert job.free_credit_spent == 1 and job.credit_spent == 0

    a = account(client)
    assert a["credits"] == 0 and a["cloud_allowed"] is False
    assert start(client, make_job(client)).status_code == 402


def test_a_verified_email_gets_two_free_credits_in_all_and_one_a_day(client, cloud):
    assert start(client, make_job(client)).status_code == 200  # the anonymous credit
    assert account(client)["free_credits_with_account"] == 1

    sign_in(client, unique("verify"))
    a = account(client)
    assert a["signed_in"] is True and a["email_verified"] is True
    assert a["free_credits"] == 2  # one of the two, and the daily credit
    assert a["free_credits_with_account"] == 0


def test_a_buyer_gets_account_credits_only_after_verifying_the_email(client, cloud):
    email = unique("buyer")
    buy(client, "pack10", email)
    a = account(client)
    assert a["signed_in"] is True and a["email_verified"] is False
    assert a["credits"] == 11 and a["free_credits"] == 1  # 10 bought, the anonymous one
    assert a["free_credits_with_account"] == 2

    sign_in(client, email)  # the same address: this verifies the account
    a = account(client)
    assert a["email_verified"] is True
    assert a["free_credits"] == 3 and a["credits"] == 13


def test_the_daily_credit_does_not_carry_over(client, cloud, monkeypatch):
    sign_in(client, unique("daily"))
    assert account(client)["free_credits"] == 3  # two, and today's

    days_later(monkeypatch, 1)
    assert account(client)["free_credits"] == 3  # not 4: yesterday's credit is gone
    for _ in range(3):
        assert start(client, make_job(client)).status_code == 200
    assert start(client, make_job(client)).status_code == 402

    days_later(monkeypatch, 1)
    assert account(client)["free_credits"] == 1  # the credit of the new day
    job_id = make_job(client)
    assert start(client, job_id).status_code == 200
    assert get_job_row(job_id).daily_credit_on == billing._today()


def test_every_two_books_converted_on_the_server_earn_a_free_credit(client, cloud):
    sign_in(client, unique("bonus"))
    before = account(client)["free_credits"]

    make_job(client, JobStatus.succeeded)
    assert account(client)["free_credits"] == before
    make_job(client, JobStatus.succeeded)
    assert account(client)["free_credits"] == before + 1

    # Books converted in the browser do not count: the server cannot check them.
    convert_in_browser(client, 1)
    convert_in_browser(client, 2)
    assert account(client)["free_credits"] == before + 1


def test_free_credits_are_spent_before_bought_ones(client, cloud):
    buy(client, "single", unique("order"))  # a retired plan: one credit
    assert account(client)["credits"] == 2  # one bought, the anonymous one

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


def test_a_failed_job_gives_the_daily_credit_back(client, cloud):
    sign_in(client, unique("refund"))
    job_id = make_job(client)
    assert start(client, job_id).status_code == 200
    assert get_job_row(job_id).daily_credit_on == billing._today()

    runner.run_job(job_id)  # fails: nothing was staged
    assert get_job_row(job_id).daily_credit_on is None
    assert account(client)["free_credits"] == 3


def test_a_second_device_adds_no_free_credits(client, second_client, cloud):
    email = unique("merge")
    sign_in(client, email)
    assert account(client)["free_credits"] == 3

    # The second device spends its anonymous credit, then signs in as the same person.
    assert start(second_client, make_job(second_client)).status_code == 200
    sign_in(second_client, email)
    assert account(second_client)["free_credits"] == 2
    assert account(client)["free_credits"] == 2

    # A new cookie that signs in again brings no free credit along either.
    client.post("/api/auth/signout")
    assert account(client)["free_credits"] == 1
    sign_in(client, email)
    assert account(client)["free_credits"] == 2


def test_no_free_credits_while_the_server_takes_no_conversions(client):
    # conftest leaves SUBPLZ_WEB_CLOUD_ENABLED off.
    a = account(client)
    assert a["credits"] == 0 and a["free_credits"] == 0
    assert a["free_credits_with_account"] == 0 and a["daily_free_credit"] is False
    assert start(client, make_job(client)).status_code == 402


def test_browser_jobs_leave_the_free_credit_alone(client, cloud):
    r = client.post("/api/local/jobs", json=BOOK)
    assert r.status_code == 200 and r.json()["tier"] == "free"
    assert account(client)["free_credits"] == 1
