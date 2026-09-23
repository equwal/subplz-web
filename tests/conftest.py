"""Test harness: a throwaway data dir, billing on, Stripe faked at the SDK edge.

Settings are read once at import, so the environment has to be in place before
anything under `backend` is imported - hence doing it here, at module load.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="subplz-web-tests-")
os.environ.update(
    SUBPLZ_WEB_DATA_DIR=_TMP,
    SUBPLZ_WEB_BILLING_ENABLED="true",
    SUBPLZ_WEB_STRIPE_SECRET_KEY="sk_test_dummy",
    SUBPLZ_WEB_STRIPE_WEBHOOK_SECRET="whsec_test_secret",
    SUBPLZ_WEB_PUBLIC_BASE_URL="https://example.test",
    SUBPLZ_WEB_MATCH_CHECK="false",
    SUBPLZ_WEB_SMTP_HOST="",
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import api  # noqa: E402
from backend.db import (  # noqa: E402
    Account, Artifact, Job, JobStatus, SessionLocal, init_db,
)
from backend.main import app  # noqa: E402
from backend.storage import storage  # noqa: E402

WEBHOOK_SECRET = "whsec_test_secret"

init_db()


@pytest.fixture(autouse=True)
def _no_real_work(monkeypatch):
    """Starting a job must not actually launch an alignment."""
    monkeypatch.setattr(api.queue, "enqueue", lambda job_id, paid=True: None)


@pytest.fixture(autouse=True)
def _no_stripe_prices(monkeypatch):
    """Stripe has no prices for the plans, unless a test says so. No test may
    reach the real Stripe API."""
    import stripe
    monkeypatch.setattr(stripe.Price, "list", lambda **kw: FakeStripeObject({"data": []}))


@pytest.fixture
def client():
    """A browser: keeps its cookie, so it stays one account across requests."""
    with TestClient(app, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def second_client():
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def account_id(client: TestClient) -> str:
    return client.get("/api/account").json()["id"]


def make_job(
    client: TestClient, status: JobStatus = JobStatus.draft, with_files: bool = False
) -> str:
    """A job row owned by `client`, skipping the upload (which needs real audio)."""
    acct = account_id(client)
    with SessionLocal() as s:
        job = Job(
            account_id=acct, status=status, language="en", splitter="pysbd",
            model="tiny", audio_filename="book.m4b", text_filename="book.epub",
        )
        s.add(job)
        s.commit()
        job_id = job.id

        if with_files:
            for kind, name in [("srt", "book.en.srt"), ("video", "book.en.mp4"),
                               ("video_embedded", "book.en.mkv")]:
                src = Path(_TMP) / f"{job_id}-{name}"
                src.write_bytes(b"x" * 10)
                key = f"{job_id}/{name}"
                storage.put_file(key, src)
                s.add(Artifact(job_id=job_id, kind=kind, filename=name,
                               storage_key=key, size_bytes=10))
            s.commit()
    return job_id


def get_account_row(account_id_: str) -> Account:
    with SessionLocal() as s:
        return s.get(Account, account_id_)


def get_job_row(job_id: str) -> Job:
    with SessionLocal() as s:
        return s.get(Job, job_id)


def signed(payload: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, dict]:
    """A webhook body plus the header Stripe would have sent with it."""
    body = json.dumps(payload).encode()
    stamp = int(time.time())
    digest = hmac.new(
        secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return body, {
        "stripe-signature": f"t={stamp},v1={digest}",
        "content-type": "application/json",
    }


def checkout_event(
    session_id: str, account: str, plan: str, email: str | None = "buyer@example.com",
    job_id: str = "", payment_status: str = "paid", subscription: str | None = None,
    customer: str | None = "cus_test",
) -> dict:
    return {
        "id": f"evt_{session_id}",
        "type": "checkout.session.completed",
        "data": {"object": checkout_object(
            session_id, account, plan, email, job_id, payment_status,
            subscription, customer,
        )},
    }


def checkout_object(session_id, account, plan, email="buyer@example.com",
                    job_id="", payment_status="paid", subscription=None,
                    customer="cus_test") -> dict:
    return {
        "id": session_id,
        "object": "checkout.session",
        "client_reference_id": account,
        "customer": customer,
        "customer_details": {"email": email} if email else None,
        "payment_status": payment_status,
        "amount_total": 1299,
        "currency": "usd",
        "subscription": subscription,
        "metadata": {"account_id": account, "plan_id": plan, "job_id": job_id},
    }


def post_webhook(client: TestClient, event: dict, secret: str = WEBHOOK_SECRET):
    body, headers = signed(event, secret)
    return client.post("/api/billing/webhook", content=body, headers=headers)


class FakeStripeObject:
    """Stands in for an SDK return value: not a dict, but has to_dict()."""

    def __init__(self, data: dict):
        self._data = data

    def to_dict(self) -> dict:
        return self._data
