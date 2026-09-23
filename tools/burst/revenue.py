"""The net server revenue of the last 30 days: the base of the burst spend cap.

Packs count for 30 days after the purchase. A subscription counts while it is
active and paid for the current period. Free credits count for nothing. Stripe
takes 2.9% + 30 cents of each payment, and 0.7% more of a subscription payment
(Stripe Billing).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import pricing
from backend.db import Account, Purchase


def net_pack_cents(amount_cents: int) -> int:
    return amount_cents - math.ceil(0.029 * amount_cents) - 30


def net_subscription_cents(amount_cents: int) -> int:
    return amount_cents - math.ceil(0.036 * amount_cents) - 30


def _aware(when: datetime | None) -> datetime | None:
    """SQLite gives back naive times. The app writes UTC."""
    if when is None or when.tzinfo is not None:
        return when
    from datetime import timezone
    return when.replace(tzinfo=timezone.utc)


def revenue_30d_usd(session: Session, now: datetime) -> float:
    since = now - timedelta(days=30)
    total = 0
    for plan_id, amount, created in session.execute(
        select(Purchase.plan_id, Purchase.amount_cents, Purchase.created_at)
    ):
        plan = pricing.get(plan_id, retired=True)
        if plan is None or plan.recurring or _aware(created) < since:
            continue
        total += net_pack_cents(amount)
    for plan_id, ends in session.execute(
        select(Account.subscription_plan_id, Account.subscription_period_end)
        .where(Account.subscription_status == "active")
    ):
        plan = pricing.get(plan_id or "", retired=True)
        if plan is None or not plan.recurring or ends is None or _aware(ends) <= now:
            continue
        total += net_subscription_cents(plan.price_cents)
    return total / 100
