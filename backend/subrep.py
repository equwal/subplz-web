"""Subrep Pro: the plan of the Subrep desktop app, sold with the accounts of this site.

Subrep (https://honjimaku.com/subrep/) makes live captions of the sound on the
customer's own computer. The free edition makes live captions. Pro unlocks the
paid features of the desktop app (FEATURES in subrep/licensing.py of the
Subrep repo). The owner chose the prices on 2026-09-22:

  yearly   $9.99 a year    (first on the page: 16% less than 12 x $0.99)
  monthly  $0.99 a month

There is no free trial: the free edition is the trial.

How the desktop app gets Pro:

1. The customer pays here. A Stripe subscription makes the account Pro until
   the end of the paid period (SubrepLicence).
2. The Subrep page (/subrep.html) shows a link command with the account id
   and a refresh key. The customer runs it once on each computer.
3. The app sends the two to POST /api/subrep/refresh and gets a licence token:
   JSON claims, signed with the Ed25519 key of this server
   (SUBPLZ_WEB_SUBREP_LICENSE_KEY, made by tools/subrep_key.py). The app
   checks the token offline with the public key in its build, and asks for a
   new one before the token ends.

The token format is that of sign_token in subrep/licensing.py:
base64url(JSON with sorted keys) "." base64url(signature). A token ends with
the paid period, and after TOKEN_DAYS at most, so a refund or a chargeback
ends Pro within a month.

A Subrep plan is not a book plan: billing.py does not see it, and an account
can have both. payments.py sells the plans and gives each subscription event
to apply_subscription first.

The iOS waitlist is here too: the emails that ask to hear when an iPhone app
ships. The owner counts them on 2026-11-30 (waitlist_count): 30 is one of the
conditions to build the app.
"""
from __future__ import annotations

import base64
import hmac
import importlib.util
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import accounts, billing, payments
from .api import get_account, get_session
from .db import Account, Base, utcnow
from .pricing import Plan
from .settings import settings

log = logging.getLogger(__name__)

# A plan that was sold must stay in this list: a subscription that still runs
# names it. Change a price with a new plan id.
PLANS: list[Plan] = [
    Plan(id="subrep_year", name="Subrep Pro, yearly", credits=0, price_cents=999,
         recurring=True, interval="year",
         blurb="Renews each year until you cancel. 16% less than monthly."),
    Plan(id="subrep_month", name="Subrep Pro, monthly", credits=0, price_cents=99,
         recurring=True, interval="month",
         blurb="Renews each month until you cancel."),
]

# The longest time that one licence token is good for.
TOKEN_DAYS = 31

WAITLISTS = {"ios"}


class SubrepLicence(Base):
    """The Subrep Pro subscription of an account, and its refresh key."""

    __tablename__ = "subrep_licences"

    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("accounts.id"), primary_key=True
    )
    # The desktop app sends it to get a licence. It is kept as it is, like the
    # device token of an account: the page shows the same link command on
    # each visit, so that each computer of the customer can use it.
    refresh_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WaitlistEntry(Base):
    """An email that asks to hear when an app ships (kind "ios")."""

    __tablename__ = "waitlist"
    __table_args__ = (UniqueConstraint("kind", "email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    email: Mapped[str] = mapped_column(String(320))
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def plan(plan_id: str) -> Plan | None:
    return next((p for p in PLANS if p.id == plan_id), None)


def licence(session: Session, account_id: str) -> SubrepLicence | None:
    return session.get(SubrepLicence, account_id)


def is_pro(row: SubrepLicence | None, now: datetime | None = None) -> bool:
    """Whether the subscription is paid for the current period."""
    if row is None or row.status not in billing._SUBSCRIBED:
        return False
    end = billing._aware(row.period_end)
    return end is None or end > (now or utcnow())


def can_sign() -> bool:
    """A key is set, and the package that signs with it is installed."""
    return bool(settings.subrep_license_key) and (
        importlib.util.find_spec("cryptography") is not None
    )


def available() -> bool:
    """Subrep Pro is for sale: the owner opened the sale, and the server can
    take the money and sign a licence."""
    return settings.subrep_for_sale and can_sign() and settings.payments_configured


# ---------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------

def _plan_of(sub: dict) -> Plan | None:
    """The Subrep plan of a subscription, or None if it is not one.

    As in payments._our_plan_id: the lookup key of the price names the plan
    after a switch in the customer portal, and the checkout metadata names it
    before that.
    """
    items = (sub.get("items") or {}).get("data") or []
    price = (items[0].get("price") or {}) if items else {}
    for plan_id in (price.get("lookup_key"), (sub.get("metadata") or {}).get("plan_id")):
        found = plan(plan_id or "")
        if found is not None:
            return found
    return None


def apply_subscription(session: Session, sub: dict) -> bool:
    """Mirror a Stripe subscription of a Subrep plan. False if it is not one."""
    found = _plan_of(sub)
    if found is None:
        return False
    meta = sub.get("metadata") or {}
    account = payments._account_for(session, meta.get("account_id"), sub.get("customer"))
    if account is None:
        log.warning("subrep subscription %s matches no account", sub.get("id"))
        return True
    row = licence(session, account.id) or SubrepLicence(account_id=account.id)
    # The end of an old subscription must not end its replacement.
    if row.subscription_id and row.subscription_id != sub.get("id") \
            and sub.get("status") in ("canceled", "incomplete_expired"):
        return True
    row.subscription_id = sub.get("id")
    row.status = sub.get("status")
    row.plan_id = found.id
    row.period_end = payments._period_end(sub)
    session.add(row)
    if sub.get("customer") and not account.stripe_customer_id:
        account.stripe_customer_id = sub["customer"]
    session.commit()
    return True


def merge(session: Session, src: Account, dst: Account) -> None:
    """Called by accounts.merge: the Subrep plan of `src` goes to `dst`."""
    row = licence(session, src.id)
    if row is None:
        return
    target = licence(session, dst.id)
    if target is None or (is_pro(row) and not is_pro(target)):
        target = target or SubrepLicence(account_id=dst.id)
        target.subscription_id, target.status = row.subscription_id, row.status
        target.plan_id, target.period_end = row.plan_id, row.period_end
        session.add(target)
    # The computers that were linked to `src` keep their link command.
    target.refresh_key = target.refresh_key or row.refresh_key
    session.delete(row)


# ---------------------------------------------------------------------------
# licence tokens
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _private_key():
    # cryptography only here: a server without Subrep Pro does not need it.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    text = settings.subrep_license_key
    return Ed25519PrivateKey.from_private_bytes(
        base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    )


def sign(claims: dict) -> str:
    """A licence token, in the format of sign_token in subrep/licensing.py."""
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    return _b64e(payload) + "." + _b64e(_private_key().sign(payload))


def public_key() -> str:
    """The key to build into the desktop app (SUBREP_LICENSE_PUBKEY)."""
    return _b64e(_private_key().public_key().public_bytes_raw())


class LicenceRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def token(session: Session, account_id: str, refresh_key: str,
          now: datetime | None = None) -> str:
    """A licence token for the desktop app, or LicenceRefused."""
    now = now or utcnow()
    account = session.get(Account, account_id or "")
    if account is not None:
        account = accounts.resolve(session, account)
    row = licence(session, account.id) if account is not None else None
    if row is None or not row.refresh_key or not hmac.compare_digest(
            row.refresh_key.encode(), (refresh_key or "").encode()):
        raise LicenceRefused(
            403, "This link command is not valid. "
                 "Copy it again from the Subrep page of subread.space.")
    if not is_pro(row, now):
        raise LicenceRefused(402, "This account has no active Subrep Pro plan.")
    latest = now + timedelta(days=TOKEN_DAYS)
    end = min(billing._aware(row.period_end) or latest, latest)
    return sign({"sub": account.email or account.id, "tier": "pro",
                 "plan": row.plan_id, "iat": int(now.timestamp()),
                 "exp": int(end.timestamp())})


def link(session: Session, account: Account) -> dict:
    """The link command of a Pro account. The first call makes its refresh key."""
    row = licence(session, account.id)
    if row is None:
        raise LicenceRefused(402, "This account has no Subrep Pro plan.")
    if not row.refresh_key:
        row.refresh_key = secrets.token_urlsafe(24)
        session.commit()
    server = f"{settings.public_base_url.rstrip('/')}/api/subrep"
    return {
        "account": account.id,
        "refresh_key": row.refresh_key,
        "server": server,
        "command": f'subrep license --link "{account.id}" "{row.refresh_key}" "{server}"',
    }


# ---------------------------------------------------------------------------
# waitlist
# ---------------------------------------------------------------------------

def join(session: Session, kind: str, email: str, account_id: str | None) -> bool:
    """Put `email` on a waitlist. False if it was on it already."""
    session.add(WaitlistEntry(kind=kind, email=email, account_id=account_id))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True


def waitlist_count(session: Session, kind: str) -> int:
    return session.query(WaitlistEntry).filter(WaitlistEntry.kind == kind).count()


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/subrep")


class PlanIn(BaseModel):
    plan_id: str


class RefreshIn(BaseModel):
    account: str = ""
    refresh_key: str = ""


class WaitlistIn(BaseModel):
    email: str
    kind: str = "ios"


def _needs_sign_in(account: Account) -> bool:
    # A $0.99 checkout draws card testing. Where email sign-in exists, only a
    # verified account may pay.
    return settings.sign_in_available and not account.email_verified


@router.get("")
def subrep_state(
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    row = licence(session, account.id)
    pro = is_pro(row)
    end = billing._aware(row.period_end) if pro and row is not None else None
    return {
        "available": available(),
        "public_key": public_key() if can_sign() else None,
        "plans": [
            {"id": p.id, "name": p.name, "interval": p.interval,
             "price_cents": p.price_cents, "price_display": p.price_display,
             "blurb": p.blurb}
            for p in PLANS
        ],
        "pro": pro,
        "plan_id": row.plan_id if pro and row is not None else None,
        "period_end": end.isoformat() if end else None,
        "link": link(session, account) if pro else None,
        "needs_sign_in": _needs_sign_in(account),
        "email": account.email,
        "can_manage": bool(account.stripe_customer_id),
    }


@router.post("/checkout")
def subrep_checkout(
    body: PlanIn,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    found = plan(body.plan_id)
    if found is None:
        raise HTTPException(404, "No such plan.")
    if not available():
        raise HTTPException(503, "Subrep Pro is not for sale on this server yet.")
    if _needs_sign_in(account):
        raise HTTPException(403, "Sign in with your email first.")
    if is_pro(licence(session, account.id)):
        raise HTTPException(
            400, "You already have Subrep Pro. Change or cancel it with Manage plan.")
    try:
        return {"url": payments.start_checkout(session, account, found, page="/subrep.html")}
    except payments.PaymentsUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except payments.PaymentError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/refresh")
def subrep_refresh(
    body: RefreshIn,
    session: Annotated[Session, Depends(get_session)],
):
    """The desktop app asks for a licence (refresh in subrep/licensing.py).

    No cookie: the app is not a browser. A refusal is {"error": ...}, because
    the app shows that text to the customer.
    """
    if not can_sign():
        return JSONResponse({"error": "Subrep Pro is not set up on this server."},
                            status_code=503)
    try:
        return {"token": token(session, body.account, body.refresh_key)}
    except LicenceRefused as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)


@router.post("/waitlist")
def subrep_waitlist(
    body: WaitlistIn,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    if body.kind not in WAITLISTS:
        raise HTTPException(404, "No such waitlist.")
    try:
        email = accounts.normalize_email(body.email)
    except accounts.InvalidEmail as exc:
        raise HTTPException(400, str(exc)) from exc
    join(session, body.kind, email, account.id)
    return {"joined": True, "kind": body.kind}
