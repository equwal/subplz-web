"""Entitlement: what an account may do right now.

The line between free and paid is where the work is done, not what comes out:

  free    The job runs in the visitor's browser, on the visitor's machine. It
          costs this server nothing, so it has no price and no limit, and it
          gives each output: subtitles, videos, the read-along book.
  cloud   The job runs on this server's hardware: a large speech model on a
          GPU, minutes and not hours, from any device. One credit for a book,
          or nothing on a recurring plan if the operator sells one.

All of the code is public and anyone may host it. What is sold is the use of
this operator's machines.

Disabled on localhost (SUBPLZ_WEB_BILLING_ENABLED=false) so nothing gets in the
way while you use it yourself.

Taking the money lives in payments.py; this file only decides who may do what.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from .db import Account, Job, SessionLocal, utcnow
from .settings import settings

FREE = "free"
CLOUD = "cloud"
TIERS = (FREE, CLOUD)

# Stripe keeps retrying a failed renewal for a while; do not lock someone out
# the second their card hiccups.
_SUBSCRIBED = {"active", "trialing", "past_due"}
_GRACE = timedelta(days=1)


class PaymentRequired(RuntimeError):
    """The account cannot do this without paying. Carries the reason to show."""


def _aware(when: datetime | None) -> datetime | None:
    if when is not None and when.tzinfo is None:  # SQLite hands back naive
        return when.replace(tzinfo=timezone.utc)
    return when


@dataclass(frozen=True)
class Entitlement:
    credits: int
    subscribed: bool
    subscription_ends: datetime | None

    @property
    def cloud_allowed(self) -> bool:
        return self.subscribed or self.credits > 0


def is_subscribed(account: Account) -> bool:
    if account.subscription_status not in _SUBSCRIBED:
        return False
    ends = _aware(account.subscription_period_end)
    return ends is None or ends + _GRACE > utcnow()


def check(account: Account) -> Entitlement:
    return Entitlement(
        credits=account.purchased_credits,
        subscribed=is_subscribed(account),
        subscription_ends=_aware(account.subscription_period_end),
    )


def _spend_credit(session: Session, account: Account) -> bool:
    """Take one credit, atomically. False if there was none to take."""
    taken = session.execute(
        update(Account)
        .where(Account.id == account.id, Account.purchased_credits > 0)
        .values(purchased_credits=Account.purchased_credits - 1)
    ).rowcount
    session.refresh(account)
    return bool(taken)


def authorize_start(session: Session, account: Account, job: Job) -> None:
    """Charge what starting `job` costs, or raise PaymentRequired.

    Where the job runs sets its tier: a browser job is free, a server job is
    a cloud job.
    """
    job.tier = FREE if job.local else CLOUD
    job.billed, job.credit_spent = 0, 0
    if job.local or not settings.billing_enabled or is_subscribed(account):
        return
    if not _spend_credit(session, account):
        raise PaymentRequired(
            "A conversion on our servers takes one credit. "
            "In your browser it is free, without limit."
        )
    job.credit_spent = 1


def refund(session: Session, job: Job) -> None:
    """Hand back whatever a failed or cancelled job was holding."""
    job.billed = 0
    if job.credit_spent:
        session.execute(
            update(Account)
            .where(Account.id == job.account_id)
            .values(purchased_credits=Account.purchased_credits + 1)
        )
        job.credit_spent = 0
    session.add(job)


def refund_job(job_id: str) -> None:
    """`refund`, for the runner, which works in job ids rather than sessions."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is not None:
            refund(session, job)
            session.commit()
