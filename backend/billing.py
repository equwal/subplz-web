"""Entitlement: what an account may do right now.

The line between free and paid is where the work is done, not what comes out:

  free    The job runs in the visitor's browser, on the visitor's machine. It
          costs this server nothing, so it has no price and no limit, and it
          gives each output: subtitles, videos, the read-along book.
  cloud   The job runs on this server's hardware: a large speech model on a
          GPU, minutes and not hours, from any device. One credit for a book,
          or nothing on a recurring plan if the operator sells one. Each
          visitor gets some free credits (settings.free_credits_*); a job
          spends those before the bought ones.

All of the code is public and anyone may host it. What is sold is the use of
this operator's machines.

Disabled on localhost (SUBPLZ_WEB_BILLING_ENABLED=false) so nothing gets in the
way while you use it yourself.

Taking the money lives in payments.py; this file only decides who may do what.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from .db import Account, Job, JobStatus, SessionLocal, utcnow
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
    # Every credit the account can spend now: the bought and the free ones.
    credits: int
    # The part of `credits` that is free.
    free_credits: int
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


def _today() -> date:
    """The day of the daily free credit (UTC). Tests replace this."""
    return utcnow().date()


def _books_converted(session: Session, account: Account) -> int:
    """Books that the server converted for `account`.

    Books converted in the browser do not count: the server cannot check
    that a browser did the work.
    """
    return (
        session.query(func.count(Job.id))
        .filter(Job.account_id == account.id, Job.local == 0,
                Job.status == JobStatus.succeeded)
        .scalar()
        or 0
    )


def free_allowance(session: Session, account: Account, verified: bool | None = None) -> int:
    """The free credits (not the daily one) that `account` gets in all.

    Zero while this server takes no conversions: a free credit must not
    promise a job that cannot run. Give `verified=True` to get the number
    after the email is verified.
    """
    if not settings.cloud_enabled:
        return 0
    if verified is None:
        verified = bool(account.email_verified)
    if not verified:
        return settings.free_credits_anonymous
    bonus = 0
    if settings.books_per_bonus_credit > 0:
        bonus = _books_converted(session, account) // settings.books_per_bonus_credit
    return settings.free_credits_verified + bonus


def free_credits_left(session: Session, account: Account, verified: bool | None = None) -> int:
    return max(0, free_allowance(session, account, verified) - account.free_credits_used)


def daily_credit_left(account: Account) -> int:
    """1 while a verified account has not spent the free credit of today."""
    if not (settings.cloud_enabled and settings.daily_free_credit and account.email_verified):
        return 0
    return 0 if account.daily_credit_on == _today() else 1


def check(session: Session, account: Account) -> Entitlement:
    free = free_credits_left(session, account) + daily_credit_left(account)
    return Entitlement(
        credits=account.purchased_credits + free,
        free_credits=free,
        subscribed=is_subscribed(account),
        subscription_ends=_aware(account.subscription_period_end),
    )


def _spend_daily_credit(session: Session, account: Account) -> date | None:
    """Take the free credit of today, atomically. Its day, or None."""
    if not daily_credit_left(account):
        return None
    today = _today()
    taken = session.execute(
        update(Account)
        .where(
            Account.id == account.id,
            or_(Account.daily_credit_on.is_(None), Account.daily_credit_on != today),
        )
        .values(daily_credit_on=today)
    ).rowcount
    session.refresh(account)
    return today if taken else None


def _spend_free_credit(session: Session, account: Account) -> bool:
    """Take one free credit, atomically. False if there was none to take."""
    taken = session.execute(
        update(Account)
        .where(
            Account.id == account.id,
            Account.free_credits_used < free_allowance(session, account),
        )
        .values(free_credits_used=Account.free_credits_used + 1)
    ).rowcount
    session.refresh(account)
    return bool(taken)


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
    a cloud job. A cloud job spends the daily credit first (it does not
    carry over), then a free credit, then a bought one.
    """
    job.tier = FREE if job.local else CLOUD
    job.billed, job.credit_spent, job.free_credit_spent = 0, 0, 0
    job.daily_credit_on = None
    if job.local or not settings.billing_enabled or is_subscribed(account):
        return
    day = _spend_daily_credit(session, account)
    if day is not None:
        job.daily_credit_on = day
        return
    if _spend_free_credit(session, account):
        job.free_credit_spent = 1
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
    if job.free_credit_spent:
        session.execute(
            update(Account)
            .where(Account.id == job.account_id, Account.free_credits_used > 0)
            .values(free_credits_used=Account.free_credits_used - 1)
        )
        job.free_credit_spent = 0
    if job.daily_credit_on is not None:
        # Only the credit of the same day comes back: a later day has its own.
        session.execute(
            update(Account)
            .where(Account.id == job.account_id,
                   Account.daily_credit_on == job.daily_credit_on)
            .values(daily_credit_on=None)
        )
        job.daily_credit_on = None
    session.add(job)


def refund_job(job_id: str) -> None:
    """`refund`, for the runner, which works in job ids rather than sessions."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is not None:
            refund(session, job)
            session.commit()
