"""Tiers, credits, Stripe fulfilment. The rules under test:

  free   a job in the visitor's browser: no price, no limit, each output
  cloud  a job on this server: one credit (or a recurring plan, if one is sold)
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict

import pytest
import stripe

from backend import pricing, runner
from backend.db import JobStatus

from .conftest import (
    FakeStripeObject, account_id, checkout_event, checkout_object,
    get_account_row, get_job_row, make_job, post_webhook,
)


def start(client, job_id):
    """Start a job on the server: a cloud job."""
    return client.post(f"/api/jobs/{job_id}/start", json={"language": "en"})


@pytest.fixture
def monthly_plan(monkeypatch):
    """The default catalogue sells no recurring plan. An operator can add one."""
    plans = [asdict(p) for p in pricing.DEFAULT_PLANS]
    plans.append({"id": "monthly", "name": "Monthly", "credits": None, "price_cents": 1500, "recurring": True})
    monkeypatch.setenv("SUBPLZ_WEB_PLANS_JSON", json.dumps(plans))


def buy(client, plan="pack5", session_id=None, **kw):
    session_id = session_id or f"cs_{time.time_ns()}"
    r = post_webhook(client, checkout_event(session_id, account_id(client), plan, **kw))
    assert r.status_code == 200, r.text
    return session_id


# --- the cloud tier ----------------------------------------------------------

def test_new_visitor_is_anonymous_and_has_no_credits(client):
    a = client.get("/api/account").json()
    assert a["signed_in"] is False and a["email"] is None
    assert a["credits"] == 0 and a["subscribed"] is False
    assert a["cloud_allowed"] is False
    assert "free in your browser" in a["free_tier_summary"]


def test_nothing_is_for_sale_until_the_cloud_is_connected(client, monkeypatch):
    """Billing can be on while fast conversion is not yet on offer. The page
    reads this flag, and shows no way to buy credits that would buy nothing."""
    from backend.settings import settings
    assert client.get("/api/account").json()["cloud_available"] is False
    monkeypatch.setattr(settings, "cloud_enabled", True)
    assert client.get("/api/account").json()["cloud_available"] is True


def test_a_server_job_needs_a_credit(client):
    r = start(client, make_job(client))
    assert r.status_code == 402
    assert "one credit" in r.json()["detail"] and "free" in r.json()["detail"]


def test_each_output_of_a_job_can_be_downloaded(client):
    job_id = make_job(client, JobStatus.succeeded, with_files=True)
    arts = client.get(f"/api/jobs/{job_id}").json()["artifacts"]
    assert {a["kind"] for a in arts} >= {"srt", "video", "video_embedded"}
    assert all("locked" not in a for a in arts)
    for kind in ("srt", "video_embedded", "video"):
        assert client.get(f"/api/jobs/{job_id}/files/{kind}").status_code == 200


# --- credits -----------------------------------------------------------------

def test_purchase_credits_the_account_and_signs_it_in(client):
    buy(client, "pack5", email="Reader@Example.com")
    a = client.get("/api/account").json()
    assert a["credits"] == 5
    assert a["signed_in"] is True and a["email"] == "reader@example.com"
    assert a["cloud_allowed"] is True


def test_webhook_redelivery_does_not_credit_twice(client):
    sid = buy(client, "pack5", email="twice@example.com")
    buy(client, "pack5", session_id=sid, email="twice@example.com")
    assert client.get("/api/account").json()["credits"] == 5


def test_unpaid_checkout_credits_nothing(client):
    buy(client, "pack5", email="pending@example.com", payment_status="unpaid")
    assert client.get("/api/account").json()["credits"] == 0


def test_webhook_rejects_a_bad_signature(client):
    event = checkout_event("cs_forged", account_id(client), "pack20")
    assert post_webhook(client, event, secret="whsec_wrong").status_code == 400
    assert client.get("/api/account").json()["credits"] == 0


def test_a_server_job_spends_a_credit(client):
    buy(client, "single", email="cloud@example.com")
    job_id = make_job(client, with_files=True)
    assert start(client, job_id).status_code == 200
    assert client.get("/api/account").json()["credits"] == 0
    assert client.get(f"/api/jobs/{job_id}").json()["tier"] == "cloud"
    assert get_job_row(job_id).credit_spent == 1

    # And with the credit gone, the next one is refused again.
    assert start(client, make_job(client)).status_code == 402


def test_failed_server_job_returns_the_credit(client):
    buy(client, "single", email="refund@example.com")
    job_id = make_job(client)
    assert start(client, job_id).status_code == 200
    assert client.get("/api/account").json()["credits"] == 0

    runner.run_job(job_id)  # fails: nothing was staged

    assert client.get("/api/account").json()["credits"] == 1
    assert get_job_row(job_id).credit_spent == 0


def test_cancelled_server_job_returns_the_credit(client):
    buy(client, "single", email="cancel@example.com")
    job_id = make_job(client)
    assert start(client, job_id).status_code == 200
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    assert client.get("/api/account").json()["credits"] == 1


def test_cannot_touch_someone_elses_job(client, second_client):
    job_id = make_job(client, JobStatus.succeeded, with_files=True)
    assert second_client.get(f"/api/jobs/{job_id}/files/srt").status_code == 404
    assert second_client.post(f"/api/jobs/{job_id}/cancel").status_code == 404


# --- checkout ----------------------------------------------------------------

def test_checkout_sends_stripe_the_right_order(client, monkeypatch):
    seen = {}

    def create(**params):
        seen.update(params)
        return FakeStripeObject({"id": "cs_new", "url": "https://stripe.test/pay"})

    monkeypatch.setattr(stripe.checkout.Session, "create", create)
    r = client.post("/api/billing/checkout", json={"plan_id": "pack10"})
    assert r.status_code == 200 and r.json()["url"] == "https://stripe.test/pay"

    assert seen["mode"] == "payment"
    assert seen["client_reference_id"] == account_id(client)
    assert seen["metadata"]["plan_id"] == "pack10"
    item = seen["line_items"][0]["price_data"]
    assert item["unit_amount"] == 499 and item["currency"] == "usd"
    assert "recurring" not in item
    assert seen["success_url"].startswith(
        "https://example.test/api/billing/return?session_id={CHECKOUT_SESSION_ID}")
    assert seen["customer_creation"] == "always"


def test_the_packs_and_the_monthly_plans_for_sale(client):
    plans = client.get("/api/pricing").json()["plans"]
    row = lambda p: (p["id"], p["credits"], p["price_cents"])  # noqa: E731
    assert [row(p) for p in plans if not p["recurring"]] == [
        ("pack10", 10, 499), ("pack100", 100, 3999), ("pack500", 500, 17499)]
    assert [row(p) for p in plans if p["recurring"]] == [
        ("month10", 10, 499), ("month30", 30, 999), ("unlimited", None, 5000)]


def test_bigger_packs_cost_less_a_book(client):
    # A bigger pack must cost less a book, or nobody buys it. It must also cost
    # more in all, or the smaller packs are pointless.
    plans = client.get("/api/pricing").json()["plans"]
    packs = sorted((p for p in plans if not p["recurring"]), key=lambda p: p["credits"])
    assert [p["credits"] for p in packs] == [10, 100, 500]
    per_book = [p["price_cents"] / p["credits"] for p in packs]
    assert all(small > big for small, big in zip(per_book, per_book[1:]))
    totals = [p["price_cents"] for p in packs]
    assert all(small < big for small, big in zip(totals, totals[1:]))


@pytest.mark.parametrize("plan_id", ["pack100", "pack500"])
def test_a_big_pack_checks_out_at_its_price_and_credits_its_books(client, monkeypatch, plan_id):
    plan = pricing.get(plan_id)
    assert plan is not None
    seen = {}

    def create(**params):
        seen.update(params)
        return FakeStripeObject({"id": "cs_new", "url": "https://stripe.test/pay"})

    monkeypatch.setattr(stripe.checkout.Session, "create", create)
    assert client.post("/api/billing/checkout", json={"plan_id": plan_id}).status_code == 200
    assert seen["metadata"]["plan_id"] == plan_id
    assert seen["line_items"][0]["price_data"]["unit_amount"] == plan.price_cents

    buy(client, plan_id, email=f"{plan_id}-{time.time_ns()}@example.com")
    assert client.get("/api/account").json()["credits"] == plan.credits


def test_a_retired_plan_is_not_sold_but_a_late_payment_still_credits(client):
    # A checkout that started before the price change can finish after it.
    # It must credit what the buyer saw on the payment page.
    assert client.post("/api/billing/checkout",
                       json={"plan_id": "single"}).status_code == 404
    buy(client, "single", email=f"late-{time.time_ns()}@example.com")
    assert client.get("/api/account").json()["credits"] == 1


def test_subscription_checkout_is_recurring(client, monkeypatch, monthly_plan):
    seen = {}
    monkeypatch.setattr(
        stripe.checkout.Session, "create",
        lambda **p: seen.update(p) or FakeStripeObject({"url": "https://stripe.test/sub"}),
    )
    assert client.post("/api/billing/checkout",
                       json={"plan_id": "monthly"}).status_code == 200
    assert seen["mode"] == "subscription"
    assert seen["line_items"][0]["price_data"]["recurring"] == {"interval": "month"}
    assert seen["subscription_data"]["metadata"]["account_id"] == account_id(client)
    assert "customer_creation" not in seen  # not allowed in subscription mode


def test_unknown_plan_is_refused(client):
    assert client.post("/api/billing/checkout",
                       json={"plan_id": "nope"}).status_code == 404


def test_return_trip_fulfils_and_the_webhook_then_adds_nothing(client, monkeypatch):
    acct = account_id(client)
    paid = checkout_object("cs_return", acct, "pack20", email="return@example.com")
    monkeypatch.setattr(stripe.checkout.Session, "retrieve",
                        lambda sid: FakeStripeObject(paid))

    r = client.get("/api/billing/return?session_id=cs_return", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?checkout=paid"
    assert client.get("/api/account").json()["credits"] == 20

    assert post_webhook(client, checkout_event(
        "cs_return", acct, "pack20", email="return@example.com")).status_code == 200
    assert client.get("/api/account").json()["credits"] == 20


def test_return_trip_does_not_trust_an_unpaid_session(client, monkeypatch):
    unpaid = checkout_object("cs_unpaid", account_id(client), "pack20",
                             payment_status="unpaid")
    monkeypatch.setattr(stripe.checkout.Session, "retrieve",
                        lambda sid: FakeStripeObject(unpaid))
    r = client.get("/api/billing/return?session_id=cs_unpaid", follow_redirects=False)
    assert r.headers["location"] == "/?checkout=pending"
    assert client.get("/api/account").json()["credits"] == 0


# --- subscription ------------------------------------------------------------

def subscription_event(kind, account, status, ends_in=30 * 86400, sub_id="sub_1",
                       plan_id="monthly"):
    return {
        "id": f"evt_{kind}_{time.time_ns()}",
        "type": f"customer.subscription.{kind}",
        "data": {"object": {
            "id": sub_id, "object": "subscription", "status": status,
            "customer": "cus_sub", "metadata": {"account_id": account, "plan_id": plan_id},
            # Where newer API versions put it; _period_end reads both places.
            "items": {"data": [{"current_period_end": int(time.time()) + ends_in}]},
        }},
    }


def test_subscription_lifts_every_limit_then_lapses(client, monthly_plan):
    acct = account_id(client)
    assert post_webhook(client, subscription_event("created", acct, "active")).status_code == 200

    a = client.get("/api/account").json()
    assert a["subscribed"] is True and a["cloud_allowed"] is True
    assert a["subscription_ends"]

    # Server jobs start, and no credits are spent.
    for _ in range(3):
        job_id = make_job(client, with_files=True)
        assert start(client, job_id).status_code == 200
    assert get_job_row(job_id).credit_spent == 0

    post_webhook(client, subscription_event("deleted", acct, "canceled"))
    a = client.get("/api/account").json()
    assert a["subscribed"] is False
    assert start(client, make_job(client)).status_code == 402


def test_subscription_past_its_paid_period_does_not_count(client, monthly_plan):
    acct = account_id(client)
    post_webhook(client, subscription_event("updated", acct, "active", ends_in=-3 * 86400))
    assert client.get("/api/account").json()["subscribed"] is False


def test_cannot_subscribe_twice(client, monkeypatch, monthly_plan):
    post_webhook(client, subscription_event("created", account_id(client), "active"))
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda **p: FakeStripeObject({"url": "x"}))
    r = client.post("/api/billing/checkout", json={"plan_id": "monthly"})
    assert r.status_code == 400 and "already" in r.json()["detail"]


# --- paying with an email that already has an account ------------------------

def test_paying_on_a_new_device_joins_the_existing_account(client, second_client):
    buy(client, "pack5", email="same@example.com")
    original = account_id(client)

    # Second device, anonymous, converts a book, then pays with the same email.
    job_id = make_job(second_client, JobStatus.succeeded, with_files=True)
    device = account_id(second_client)
    assert device != original
    buy(second_client, "single", email="same@example.com")

    # Its cookie now resolves to the original account, which holds everything.
    a = second_client.get("/api/account").json()
    assert a["id"] == original and a["email"] == "same@example.com"
    assert a["credits"] == 6
    assert get_job_row(job_id).account_id == original
    assert get_account_row(device).merged_into == original
    # ...and both browsers see the same jobs.
    assert job_id in {j["id"] for j in client.get("/api/jobs").json()}
