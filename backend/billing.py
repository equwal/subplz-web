"""Entitlement: what an account may do right now.

Two tiers, split by what you walk away with:

  free      The subtitles (.srt, for HoshiReader) and the video with the
            subtitles built in (.mkv, for MPV/VLC). One book per rolling
            24 hours.
  youtube   All of that plus the clean .mp4 made for uploading to YouTube.
            Costs one credit, or nothing on the unlimited plan, and starts
            right away - paying customers do not queue behind the free window.

Disabled on localhost (SUBPLZ_WEB_BILLING_ENABLED=false) so nothing gets in the
way while you use it yourself. The accounting still runs either way, so turning
billing on for the public release does not need a backfill.

The window is rolling, not a calendar day: the allowance comes back 24 hours
after the run that used it, which avoids a midnight stampede and is easier to
explain than "resets at 00:00 in some timezone".

Taking the money lives in payments.py; this file only decides who may do what.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from .db import Account, Job, JobStatus, SessionLocal, utcnow
from .settings import settings

FREE = "free"
YOUTUBE = "youtube"
TIERS = (FREE, YOUTUBE)

# Jobs in these states hold a slot in the free window. A failed or cancelled
# run releases it: charging for our own failure is not a business model.
_HOLDING = [JobStatus.queued, JobStatus.running, JobStatus.succeeded]

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
    # Free tier.
    free_allowed: bool
    free_used: int
    free_allowance: int
    free_remaining: int
    window_hours: int
    # When the next free conversion becomes available, if the window is full.
    next_free_at: datetime | None
    # Paid tier.
    credits: int
    subscribed: bool
    subscription_ends: datetime | None
    # Why a free start is blocked, ready to show.
    reason: str = ""

    @property
    def youtube_allowed(self) -> bool:
        return self.subscribed or self.credits > 0


def is_subscribed(account: Account) -> bool:
    if account.subscription_status not in _SUBSCRIBED:
        return False
    ends = _aware(account.subscription_period_end)
    return ends is None or ends + _GRACE > utcnow()


def _window_start() -> datetime:
    return utcnow() - timedelta(hours=settings.free_window_hours)


def _window_filter(account_id: str):
    return (
        Job.account_id == account_id,
        Job.billed == 1,
        Job.status.in_(_HOLDING),
        Job.created_at >= _window_start(),
    )


def check(session: Session, account: Account) -> Entitlement:
    used = (
        session.query(func.count(Job.id)).filter(*_window_filter(account.id)).scalar()
        or 0
    )
    free = settings.free_conversions
    window = settings.free_window_hours
    subscribed = is_subscribed(account)
    remaining = max(0, free - used)

    def build(allowed: bool, reason: str = "", when: datetime | None = None):
        return Entitlement(
            free_allowed=allowed, free_used=used, free_allowance=free,
            free_remaining=remaining, window_hours=window, next_free_at=when,
            credits=account.purchased_credits, subscribed=subscribed,
            subscription_ends=_aware(account.subscription_period_end),
            reason=reason,
        )

    if not settings.billing_enabled or subscribed or remaining > 0:
        return build(True)

    # The earliest job still inside the window decides when a slot frees up.
    oldest = _aware(
        session.query(func.min(Job.created_at))
        .filter(*_window_filter(account.id))
        .scalar()
    )
    when = oldest + timedelta(hours=window) if oldest else None
    book = "book" if free == 1 else "books"
    return build(
        False,
        reason=(
            f"The free tier is {free} {book} every {window} hours. "
            + (
                f"Your next one unlocks at {when:%H:%M UTC on %d %b}. "
                if when
                else ""
            )
            + "A credit converts a book right now, YouTube video included."
        ),
        when=when,
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


def authorize_start(session: Session, account: Account, job: Job, tier: str) -> None:
    """Charge whatever starting `job` on `tier` costs, or raise PaymentRequired."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")

    job.tier, job.billed, job.credit_spent = tier, 0, 0

    if not settings.billing_enabled:
        job.billed = 1  # accounting only; nothing is ever refused
        return

    if is_subscribed(account):
        return

    if tier == YOUTUBE:
        if not _spend_credit(session, account):
            raise PaymentRequired(
                "The YouTube video is a paid extra: one credit per book, "
                "or the unlimited plan."
            )
        job.credit_spent = 1
        return

    ent = check(session, account)
    if not ent.free_allowed:
        raise PaymentRequired(ent.reason)
    job.billed = 1


def unlock(session: Session, account: Account, job: Job) -> None:
    """Upgrade a free job to the YouTube tier after the fact.

    The mp4 already exists - the mkv is made from it - so this is only ever a
    permission change, never a second run.
    """
    if job.tier == YOUTUBE or not settings.billing_enabled:
        job.tier = YOUTUBE
        return
    if not is_subscribed(account):
        if not _spend_credit(session, account):
            raise PaymentRequired(
                "Unlocking the YouTube video takes one credit, "
                "or the unlimited plan."
            )
        job.credit_spent = 1
    job.tier = YOUTUBE


def can_download(job: Job, kind: str) -> bool:
    if kind != "video" or not settings.billing_enabled:
        return True
    return job.tier == YOUTUBE


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
        # Back to free, so a retry is priced again rather than riding on a
        # credit that has just been returned.
        job.tier = FREE
    session.add(job)


def refund_job(job_id: str) -> None:
    """`refund`, for the runner, which works in job ids rather than sessions."""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is not None:
            refund(session, job)
            session.commit()
