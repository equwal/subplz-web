"""Tiers, credits, Stripe fulfilment. The rules under test:

  free     srt + mkv, one book per window
  youtube  + the clean mp4; one credit, or the unlimited plan
"""

from __future__ import annotations

import time

import stripe

from backend import billing, runner
from backend.db import JobStatus

from .conftest import (
    FakeStripeObject, account_id, checkout_event, checkout_object,
    get_account_row, get_job_row, make_job, post_webhook,
)


def start(client, job_id, tier="free"):
    return client.post(f"/api/jobs/{job_id}/start", json={"language": "en", "tier": tier})


def buy(client, plan="pack5", session_id=None, **kw):
    session_id = session_id or f"cs_{time.time_ns()}"
    r = post_webhook(client, checkout_event(session_id, account_id(client), plan, **kw))
    assert r.status_code == 200, r.text
    return session_id


# --- free tier ---------------------------------------------------------------

def test_new_visitor_is_anonymous_with_a_free_book(client):
    a = client.get("/api/account").json()
    assert a["signed_in"] is False and a["email"] is None
    assert a["free_allowed"] is True and a["free_remaining"] == 1
    assert a["credits"] == 0 and a["subscribed"] is False
    assert a["youtube_allowed"] is False


def test_free_window_allows_one_book_then_asks_for_payment(client):
    assert start(client, make_job(client)).status_code == 200

    r = start(client, make_job(client))
    assert r.status_code == 402
    assert "every 24 hours" in r.json()["detail"]

    a = client.get("/api/account").json()
    assert a["free_allowed"] is False and a["next_free_at"]


def test_failed_job_gives_the_free_slot_back(client):
    job_id = make_job(client)
    assert start(client, job_id).status_code == 200
    # No staged inputs, so the real runner fails it - which is the point.
    runner.run_job(job_id)
    assert get_job_row(job_id).status == JobStatus.failed
    assert start(client, make_job(client)).status_code == 200


def test_free_job_locks_only_the_youtube_video(client):
    job_id = make_job(client, JobStatus.succeeded, with_files=True)
    arts = {a["kind"]: a for a in client.get(f"/api/jobs/{job_id}").json()["artifacts"]}
    assert arts["video"]["locked"] is True
    assert arts["srt"]["locked"] is False
    assert arts["video_embedded"]["locked"] is False

    assert client.get(f"/api/jobs/{job_id}/files/srt").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/files/video_embedded").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/files/video").status_code == 402


def test_youtube_tier_needs_payment(client):
    r = start(client, make_job(client), tier="youtube")
    assert r.status_code == 402
    # Refused, so nothing was taken and the free book is still there.
    assert client.get("/api/account").json()["free_remaining"] == 1


# --- credits -----------------------------------------------------------------

def test_purchase_credits_the_account_and_signs_it_in(client):
    buy(client, "pack5", email="Reader@Example.com")
    a = client.get("/api/account").json()
    assert a["credits"] == 5
    assert a["signed_in"] is True and a["email"] == "reader@example.com"
    assert a["youtube_allowed"] is True


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


def test_youtube_job_spends_a_credit_and_skips_the_free_window(client):
    buy(client, "single", email="yt@example.com")
    # Use up the free book first: a credit must not care.
    assert start(client, make_job(client)).status_code == 200

    job_id = make_job(client, with_files=True)
    assert start(client, job_id, tier="youtube").status_code == 200
    assert client.get("/api/account").json()["credits"] == 0

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["tier"] == "youtube"
    assert all(not a["locked"] for a in job["artifacts"])
    assert client.get(f"/api/jobs/{job_id}/files/video").status_code == 200

    # And with the credit gone, the next one is refused again.
    assert start(client, make_job(client), tier="youtube").status_code == 402


def test_failed_youtube_job_returns_the_credit(client):
    buy(client, "single", email="refund@example.com")
    job_id = make_job(client)
    assert start(client, job_id, tier="youtube").status_code == 200
    assert client.get("/api/account").json()["credits"] == 0

    runner.run_job(job_id)  # fails: nothing was staged

    assert client.get("/api/account").json()["credits"] == 1
    job = get_job_row(job_id)
    assert job.credit_spent == 0 and job.tier == billing.FREE


def test_cancelled_youtube_job_returns_the_credit(client):
    buy(client, "single", email="cancel@example.com")
    job_id = make_job(client)
    assert start(client, job_id, tier="youtube").status_code == 200
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    assert client.get("/api/account").json()["credits"] == 1


def test_unlock_a_finished_free_job(client):
    job_id = make_job(client, JobStatus.succeeded, with_files=True)
    assert client.post(f"/api/jobs/{job_id}/unlock").status_code == 402

    buy(client, "single", email="unlock@example.com")
    r = client.post(f"/api/jobs/{job_id}/unlock")
    assert r.status_code == 200 and r.json()["tier"] == "youtube"
    assert client.get("/api/account").json()["credits"] == 0
    assert client.get(f"/api/jobs/{job_id}/files/video").status_code == 200

    # Unlocking twice must not charge twice.
    buy(client, "single", email="unlock@example.com")
    assert client.post(f"/api/jobs/{job_id}/unlock").status_code == 200
    assert client.get("/api/account").json()["credits"] == 1


def test_purchase_made_for_a_job_unlocks_it(client):
    job_id = make_job(client, JobStatus.succeeded, with_files=True)
    buy(client, "single", email="forjob@example.com", job_id=job_id)
    assert get_job_row(job_id).tier == "youtube"
    # The one credit bought was the one spent.
    assert client.get("/api/account").json()["credits"] == 0


def test_cannot_touch_someone_elses_job(client, second_client):
    job_id = make_job(client, JobStatus.succeeded, with_files=True)
    assert second_client.post(f"/api/jobs/{job_id}/unlock").status_code == 404
    assert second_client.get(f"/api/jobs/{job_id}/files/srt").status_code == 404
    r = second_client.post("/api/billing/checkout",
                           json={"plan_id": "single", "job_id": job_id})
    assert r.status_code == 404


# --- checkout ----------------------------------------------------------------

def test_checkout_sends_stripe_the_right_order(client, monkeypatch):
    seen = {}

    def create(**params):
        seen.update(params)
        return FakeStripeObject({"id": "cs_new", "url": "https://stripe.test/pay"})

    monkeypatch.setattr(stripe.checkout.Session, "create", create)
    r = client.post("/api/billing/checkout", json={"plan_id": "pack5"})
    assert r.status_code == 200 and r.json()["url"] == "https://stripe.test/pay"

    assert seen["mode"] == "payment"
    assert seen["client_reference_id"] == account_id(client)
    assert seen["metadata"]["plan_id"] == "pack5"
    item = seen["line_items"][0]["price_data"]
    assert item["unit_amount"] == 1299 and item["currency"] == "usd"
    assert "recurring" not in item
    assert seen["success_url"].startswith(
        "https://example.test/api/billing/return?session_id={CHECKOUT_SESSION_ID}")
    assert seen["customer_creation"] == "always"


def test_subscription_checkout_is_recurring(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        stripe.checkout.Session, "create",
        lambda **p: seen.update(p) or FakeStripeObject({"url": "https://stripe.test/sub"}),
    )
    assert client.post("/api/billing/checkout",
                       json={"plan_id": "unlimited"}).status_code == 200
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

def subscription_event(kind, account, status, ends_in=30 * 86400, sub_id="sub_1"):
    return {
        "id": f"evt_{kind}_{time.time_ns()}",
        "type": f"customer.subscription.{kind}",
        "data": {"object": {
            "id": sub_id, "object": "subscription", "status": status,
            "customer": "cus_sub", "metadata": {"account_id": account},
            # Where newer API versions put it; _period_end reads both places.
            "items": {"data": [{"current_period_end": int(time.time()) + ends_in}]},
        }},
    }


def test_subscription_lifts_every_limit_then_lapses(client):
    acct = account_id(client)
    assert post_webhook(client, subscription_event("created", acct, "active")).status_code == 200

    a = client.get("/api/account").json()
    assert a["subscribed"] is True and a["youtube_allowed"] is True
    assert a["subscription_ends"]

    # No window, no credits spent, mp4 unlocked.
    for _ in range(3):
        job_id = make_job(client, with_files=True)
        assert start(client, job_id, tier="youtube").status_code == 200
    assert get_job_row(job_id).credit_spent == 0
    assert client.get(f"/api/jobs/{job_id}/files/video").status_code == 200

    post_webhook(client, subscription_event("deleted", acct, "canceled"))
    a = client.get("/api/account").json()
    assert a["subscribed"] is False
    assert start(client, make_job(client), tier="youtube").status_code == 402


def test_subscription_past_its_paid_period_does_not_count(client):
    acct = account_id(client)
    post_webhook(client, subscription_event("updated", acct, "active", ends_in=-3 * 86400))
    assert client.get("/api/account").json()["subscribed"] is False


def test_cannot_subscribe_twice(client, monkeypatch):
    post_webhook(client, subscription_event("created", account_id(client), "active"))
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda **p: FakeStripeObject({"url": "x"}))
    r = client.post("/api/billing/checkout", json={"plan_id": "unlimited"})
    assert r.status_code == 400 and "already" in r.json()["detail"]


# --- paying with an email that already has an account ------------------------

def test_paying_on_a_new_device_joins_the_existing_account(client, second_client):
    buy(client, "pack5", email="same@example.com")
    original = account_id(client)

    # Second device, anonymous, converts a book, then pays with the same email.
    job_id = make_job(second_client, JobStatus.succeeded, with_files=True)
    device = account_id(second_client)
    assert device != original
    buy(second_client, "single", email="same@example.com", job_id=job_id)

    # Its cookie now resolves to the original account, which holds everything.
    a = second_client.get("/api/account").json()
    assert a["id"] == original and a["email"] == "same@example.com"
    assert a["credits"] == 5  # 5 + 1 bought - 1 spent unlocking the job
    assert get_job_row(job_id).account_id == original
    assert get_job_row(job_id).tier == "youtube"
    assert get_account_row(device).merged_into == original
    # ...and both browsers see the same jobs.
    assert job_id in {j["id"] for j in client.get("/api/jobs").json()}
