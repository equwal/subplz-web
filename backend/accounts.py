"""Who an account is, and keeping one email to exactly one account.

Every visitor starts anonymous, identified by a cookie. An email gets attached
in one of two ways - following a sign-in link, or paying (Stripe collects one at
checkout) - and from then on that email is the account. When the email already
belongs to another row, the anonymous row is folded into it, so whatever someone
converted before signing in is still there afterwards.
"""

from __future__ import annotations

import re
import secrets

from sqlalchemy.orm import Session

from .db import Account, Job, Purchase

# Deliberately loose: the real test of an address is whether the link arrives.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidEmail(ValueError):
    pass


def normalize_email(raw: str | None) -> str:
    email = (raw or "").strip().lower()
    if not email or len(email) > 320 or not _EMAIL.match(email):
        raise InvalidEmail("That does not look like an email address.")
    return email


def new_account(session: Session) -> Account:
    account = Account(device_token=secrets.token_urlsafe(24))
    session.add(account)
    session.flush()
    return account


def by_email(session: Session, email: str) -> Account | None:
    return session.query(Account).filter(Account.email == email).first()


def resolve(session: Session, account: Account) -> Account:
    """Follow merged_into to the account that survived."""
    seen = {account.id}
    while account.merged_into:
        target = session.get(Account, account.merged_into)
        if target is None or target.id in seen:
            break
        seen.add(target.id)
        account = target
    return account


def merge(session: Session, src: Account, dst: Account) -> None:
    """Fold `src` into `dst`: its books, its purchases and anything it paid for."""
    if src.id == dst.id:
        return
    session.query(Job).filter(Job.account_id == src.id).update(
        {Job.account_id: dst.id}, synchronize_session=False
    )
    session.query(Purchase).filter(Purchase.account_id == src.id).update(
        {Purchase.account_id: dst.id}, synchronize_session=False
    )
    dst.purchased_credits += src.purchased_credits
    src.purchased_credits = 0
    if src.stripe_customer_id and not dst.stripe_customer_id:
        dst.stripe_customer_id = src.stripe_customer_id
    if src.subscription_id and not dst.subscription_id:
        dst.subscription_id = src.subscription_id
        dst.subscription_status = src.subscription_status
        dst.subscription_period_end = src.subscription_period_end
    src.stripe_customer_id = None
    src.subscription_id = src.subscription_status = None
    src.subscription_period_end = None
    src.merged_into = dst.id


def adopt_email(session: Session, account: Account, email: str) -> Account:
    """Attach `email` to `account`, returning whichever account ends up owning it.

    The caller must point the browser at the returned account - it is not
    always the one passed in.
    """
    account = resolve(session, account)
    if account.email == email:
        return account

    owner = by_email(session, email)

    if account.email is None:
        if owner is None:
            account.email = email
            return account
        # Known email, anonymous device: bring the device's work along.
        merge(session, account, owner)
        return owner

    # Signed in as someone else already. Switch users; never merge two people.
    if owner is None:
        owner = new_account(session)
        owner.email = email
    return owner
