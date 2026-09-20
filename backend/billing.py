"""Entitlement: one free book per rolling 24 hours, pay for more.

Disabled on localhost (SUBPLZ_WEB_BILLING_ENABLED=false) so nothing gets in the
way while you use it yourself. The accounting still runs either way - every job
records whether it consumed an allowance - so turning billing on for the public
release does not need a backfill.

The window is rolling, not a calendar day: the allowance comes back 24 hours
after the run that used it, which avoids a midnight stampede and is easier to
explain than "resets at 00:00 in some timezone".

Deliberately not included: a payment provider. `start_checkout` is the single
seam where Stripe (or anything else) plugs in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from .db import Account, Job, JobStatus, utcnow
from .settings import settings

# Jobs in these states hold an allowance. A failed or cancelled run releases it:
# charging for our own failure is not a business model.
_HOLDING = [JobStatus.queued, JobStatus.running, JobStatus.succeeded]


@dataclass(frozen=True)
class Entitlement:
    allowed: bool
    used: int
    free_allowance: int
    purchased_credits: int
    remaining: int
    window_hours: int
    # When the next free conversion becomes available, if the window is full.
    next_free_at: datetime | None = None
    reason: str = ""

    @property
    def needs_payment(self) -> bool:
        return not self.allowed


def _window_start() -> datetime:
    return utcnow() - timedelta(hours=settings.free_window_hours)


def _jobs_in_window(session: Session, account_id: str) -> int:
    """Billable jobs started inside the current window."""
    return (
        session.query(func.count(Job.id))
        .filter(
            Job.account_id == account_id,
            Job.billed == 1,
            Job.status.in_(_HOLDING),
            Job.created_at >= _window_start(),
        )
        .scalar()
        or 0
    )


def _oldest_in_window(session: Session, account_id: str) -> datetime | None:
    """The earliest billable job still inside the window.

    Its age is what decides when the allowance frees up again.
    """
    return (
        session.query(func.min(Job.created_at))
        .filter(
            Job.account_id == account_id,
            Job.billed == 1,
            Job.status.in_(_HOLDING),
            Job.created_at >= _window_start(),
        )
        .scalar()
    )


def check(session: Session, account: Account) -> Entitlement:
    used = _jobs_in_window(session, account.id)
    free = settings.free_conversions
    purchased = account.purchased_credits
    remaining = max(0, free + purchased - used)
    window = settings.free_window_hours

    def build(allowed: bool, reason: str = "", when: datetime | None = None):
        return Entitlement(
            allowed=allowed, used=used, free_allowance=free,
            purchased_credits=purchased, remaining=remaining,
            window_hours=window, next_free_at=when, reason=reason,
        )

    if not settings.billing_enabled:
        return build(True, "billing disabled")

    if remaining > 0:
        return build(True)

    oldest = _oldest_in_window(session, account.id)
    when = None
    if oldest is not None:
        if oldest.tzinfo is None:  # SQLite hands back naive datetimes
            oldest = oldest.replace(tzinfo=timezone.utc)
        when = oldest + timedelta(hours=window)

    return build(
        False,
        reason=(
            f"You get {free} free book every {window} hours. "
            + (
                f"Your next free conversion unlocks at "
                f"{when:%H:%M UTC on %d %b}."
                if when
                else "Try again later."
            )
            + " Add credit to convert one now."
        ),
        when=when,
    )


def consume(session: Session, job: Job) -> None:
    """Mark a job as having used an allowance. Called as the job is accepted."""
    job.billed = 1
    session.add(job)


def refund(session: Session, job: Job) -> None:
    """Release the allowance a failed or cancelled job held."""
    job.billed = 0
    session.add(job)


def start_checkout(account: Account, quantity: int = 1) -> str:
    """Return a payment URL for `quantity` extra conversions.

    Wire Stripe in here for the public release, e.g.:

        session = stripe.checkout.Session.create(
            customer=account.stripe_customer_id,
            line_items=[{"price": PRICE_ID, "quantity": quantity}],
            mode="payment",
            success_url=..., cancel_url=...,
        )
        return session.url

    and credit `Account.purchased_credits` from the webhook, not from the
    success redirect - the redirect is not a payment confirmation.
    """
    raise NotImplementedError(
        "No payment provider configured. Implement billing.start_checkout "
        "before enabling SUBPLZ_WEB_BILLING_ENABLED."
    )
