"""Subscriptions: a plan renews each month until the customer cancels it.

  capped     a number of books a month. The books of one month do not carry
             over to the next. A server job spends the daily free credit
             first (it ends sooner), then the books of the plan, then free
             and bought credits.
  unlimited  no credit is spent while the plan is active.

Stripe tells the server about a plan with customer.subscription.* events. A
new month (a new current_period_end) starts the count again.
"""

from __future__ import annotations

import json
import time

import pytest
import stripe

from backend import runner

from .conftest import (
    FakeStripeObject, account_id, checkout_event, get_account_row, get_job_row,
    make_job, post_webhook,
)

# Its own catalogue, so that these tests do not depend on the plans for sale.
TEST_PLANS = [
    {"id": "pack10", "name": "10 books", "credits": 10, "price_cents": 499},
    {"id": "month10", "name": "10 books a month", "credits": 10, "price_cents": 499,
     "recurring": True},
    {"id": "forever", "name": "Unlimited", "credits": None, "price_cents": 5000,
     "recurring": True},
]


@pytest.fixture(autouse=True)
def catalogue(monkeypatch):
    monkeypatch.setenv("SUBPLZ_WEB_PLANS_JSON", json.dumps(TEST_PLANS))


def subscription_event(kind, account, plan_id, status="active", period_end=None):
    end = period_end or int(time.time()) + 30 * 86400
    return {
        "id": f"evt_{kind}_{time.time_ns()}",
        "type": f"customer.subscription.{kind}",
        "data": {"object": {
            "id": f"sub_{account}", "object": "subscription", "status": status,
            "customer": f"cus_{account}",
            "metadata": {"account_id": account, "plan_id": plan_id},
            "items": {"data": [{"current_period_end": end}]},
        }},
    }


def subscribe(client, plan_id, **kw) -> str:
    acct = account_id(client)
    event = subscription_event("created", acct, plan_id, **kw)
    assert post_webhook(client, event).status_code == 200
    return acct


def start(client, job_id: str):
    return client.post(f"/api/jobs/{job_id}/start", json={"language": "en"})


def account(client) -> dict:
    return client.get("/api/account").json()


def test_a_capped_plan_checks_out_as_a_monthly_subscription(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        stripe.checkout.Session, "create",
        lambda **p: seen.update(p) or FakeStripeObject({"url": "https://stripe.test/sub"}),
    )
    assert client.post("/api/billing/checkout", json={"plan_id": "month10"}).status_code == 200
    assert seen["mode"] == "subscription"
    item = seen["line_items"][0]["price_data"]
    assert item["recurring"] == {"interval": "month"} and item["unit_amount"] == 499
    assert seen["subscription_data"]["metadata"]["plan_id"] == "month10"


def test_paying_for_a_plan_gives_the_books_of_the_month_not_a_pack(client, monkeypatch):
    acct = account_id(client)
    sub = subscription_event("created", acct, "month10")["data"]["object"]
    monkeypatch.setattr(stripe.Subscription, "retrieve", lambda sid: FakeStripeObject(sub))
    event = checkout_event(f"cs_{time.time_ns()}", acct, "month10",
                           email=f"plan-{time.time_ns()}@example.com", subscription=sub["id"])
    assert post_webhook(client, event).status_code == 200

    a = account(client)
    assert a["subscribed"] is True and a["unlimited"] is False
    assert a["plan_credits"] == 10 and a["credits"] == 10
    assert get_account_row(acct).purchased_credits == 0


def test_a_capped_plan_is_spent_and_a_failed_job_gives_it_back(client):
    subscribe(client, "month10")
    job_id = make_job(client)
    assert start(client, job_id).status_code == 200
    job = get_job_row(job_id)
    assert job.plan_credit_spent == 1 and job.credit_spent == 0
    assert account(client)["plan_credits"] == 9

    runner.run_job(job_id)  # fails: nothing was staged
    assert account(client)["plan_credits"] == 10
    assert get_job_row(job_id).plan_credit_spent == 0


def test_the_books_of_a_month_do_not_carry_over(client):
    acct = account_id(client)
    end = int(time.time()) + 30 * 86400
    post_webhook(client, subscription_event("created", acct, "month10", period_end=end))
    for _ in range(10):
        assert start(client, make_job(client)).status_code == 200
    assert start(client, make_job(client)).status_code == 402

    # The same month, told again: nothing changes.
    post_webhook(client, subscription_event("updated", acct, "month10", period_end=end))
    assert account(client)["plan_credits"] == 0

    # A new month starts the count again.
    post_webhook(client, subscription_event("updated", acct, "month10",
                                            period_end=end + 30 * 86400))
    assert account(client)["plan_credits"] == 10


def test_an_unlimited_plan_spends_nothing(client):
    subscribe(client, "forever")
    a = account(client)
    assert a["unlimited"] is True and a["cloud_allowed"] is True
    for _ in range(3):
        job_id = make_job(client)
        assert start(client, job_id).status_code == 200
        job = get_job_row(job_id)
        assert job.plan_credit_spent == 0 and job.credit_spent == 0


def test_a_cancelled_plan_gives_nothing(client):
    acct = subscribe(client, "month10")
    post_webhook(client, subscription_event("deleted", acct, "month10", status="canceled"))
    a = account(client)
    assert a["subscribed"] is False and a["plan_credits"] == 0
    assert start(client, make_job(client)).status_code == 402


def test_a_plan_that_is_not_in_the_catalogue_gives_nothing(client):
    # Retire a sold plan (pricing.RETIRED_PLANS); do not delete it.
    subscribe(client, "gone")
    a = account(client)
    assert a["unlimited"] is False and a["plan_credits"] == 0


def test_a_second_plan_is_refused(client, monkeypatch):
    subscribe(client, "month10")
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda **p: FakeStripeObject({"url": "x"}))
    r = client.post("/api/billing/checkout", json={"plan_id": "forever"})
    assert r.status_code == 400 and "Manage plan" in r.json()["detail"]
