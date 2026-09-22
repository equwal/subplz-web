"""Stripe: start a checkout, credit the account when it completes.

Everything Stripe-specific is in this file. billing.py decides what an account
may do; this only moves money into `Account.purchased_credits` and keeps the
subscription fields honest.

Two rules the rest follows from:

* The browser coming back from Stripe is not proof of payment. Fulfilment
  always re-reads the checkout session from Stripe, and only acts on "paid".
* Stripe tells us about one payment more than once - the webhook, the
  browser's return trip, and any number of webhook retries - so fulfilment is
  idempotent on the checkout session id (see db.Purchase).

Prices are sent inline from pricing.py rather than configured in the Stripe
dashboard, so the catalogue has one source of truth and a new deployment needs
nothing but an API key.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import accounts, billing, pricing
from .db import Account, Purchase
from .settings import settings

log = logging.getLogger(__name__)


class PaymentsUnavailable(RuntimeError):
    pass


class PaymentError(RuntimeError):
    pass


def _stripe():
    if not settings.payments_configured:
        raise PaymentsUnavailable(
            "Payments are not set up on this server yet."
        )
    import stripe  # lazy: localhost never needs the package

    stripe.api_key = settings.stripe_secret_key
    return stripe


def _plain(obj) -> dict:
    """A Stripe object as plain data. They stopped being dicts in v13."""
    if isinstance(obj, dict):
        return obj
    return obj.to_dict()


def _base() -> str:
    return settings.public_base_url.rstrip("/")


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------

def start_checkout(session: Session, account: Account, plan: pricing.Plan) -> str:
    """Return the Stripe-hosted payment page for `plan`."""
    stripe = _stripe()

    if plan.recurring and billing.is_subscribed(account):
        raise PaymentError("You are already on this plan.")

    price = {
        "currency": plan.currency,
        "unit_amount": plan.price_cents,
        "product_data": {"name": f"{settings.site_name} - {plan.name}"},
    }
    if plan.recurring:
        price["recurring"] = {"interval": "month"}

    meta = {"account_id": account.id, "plan_id": plan.id}
    params: dict = {
        "mode": "subscription" if plan.recurring else "payment",
        "line_items": [{"quantity": 1, "price_data": price}],
        "client_reference_id": account.id,
        "metadata": meta,
        # Stripe substitutes the real id; the return handler re-reads the
        # session from it rather than trusting anything in the URL.
        "success_url": f"{_base()}/api/billing/return"
                       "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": f"{_base()}/?checkout=cancelled",
        "allow_promotion_codes": True,
    }
    if account.stripe_customer_id:
        params["customer"] = account.stripe_customer_id
    else:
        if account.email:
            params["customer_email"] = account.email
        if not plan.recurring:
            # One-off payments do not create a customer unless asked, and
            # without one there is nothing to hang a later purchase on.
            params["customer_creation"] = "always"
    if plan.recurring:
        params["subscription_data"] = {"metadata": meta}

    try:
        checkout = _plain(stripe.checkout.Session.create(**params))
    except stripe.StripeError as exc:
        log.warning("checkout create failed for %s: %s", account.id, exc)
        raise PaymentError("Could not start the checkout. Try again shortly.") from exc
    return checkout["url"]


def portal_url(account: Account) -> str:
    """Stripe's own page for cancelling or updating the unlimited plan."""
    stripe = _stripe()
    if not account.stripe_customer_id:
        raise PaymentError("There is no payment history on this account yet.")
    try:
        portal = _plain(
            stripe.billing_portal.Session.create(
                customer=account.stripe_customer_id, return_url=f"{_base()}/"
            )
        )
    except stripe.StripeError as exc:
        log.warning("portal create failed for %s: %s", account.id, exc)
        raise PaymentError("Could not open the billing page. Try again shortly.") from exc
    return portal["url"]


# ---------------------------------------------------------------------------
# fulfilment
# ---------------------------------------------------------------------------

def _period_end(sub: dict) -> datetime | None:
    # Moved from the subscription onto its items in the 2025 API versions;
    # which one arrives depends on the account's webhook API version.
    stamp = sub.get("current_period_end")
    if stamp is None:
        items = (sub.get("items") or {}).get("data") or []
        stamp = items[0].get("current_period_end") if items else None
    return datetime.fromtimestamp(stamp, tz=timezone.utc) if stamp else None


def _account_for(session: Session, account_id: str | None, customer: str | None):
    account = session.get(Account, account_id) if account_id else None
    if account is None and customer:
        account = (
            session.query(Account)
            .filter(Account.stripe_customer_id == customer)
            .first()
        )
    return accounts.resolve(session, account) if account else None


def apply_subscription(session: Session, sub: dict) -> None:
    """Mirror a Stripe subscription onto its account."""
    meta = sub.get("metadata") or {}
    account = _account_for(session, meta.get("account_id"), sub.get("customer"))
    if account is None:
        log.warning("subscription %s matches no account", sub.get("id"))
        return
    # An old subscription being deleted must not wipe out its replacement.
    if account.subscription_id and account.subscription_id != sub.get("id") \
            and sub.get("status") in ("canceled", "incomplete_expired"):
        return
    account.subscription_id = sub.get("id")
    account.subscription_status = sub.get("status")
    account.subscription_period_end = _period_end(sub)
    if sub.get("customer") and not account.stripe_customer_id:
        account.stripe_customer_id = sub["customer"]
    session.commit()


def fulfil(session: Session, checkout: dict) -> Account | None:
    """Credit a completed checkout. Safe to call any number of times."""
    if checkout.get("payment_status") not in ("paid", "no_payment_required"):
        return None  # e.g. a bank debit still clearing; a later event follows

    meta = checkout.get("metadata") or {}
    account = _account_for(
        session,
        checkout.get("client_reference_id") or meta.get("account_id"),
        checkout.get("customer"),
    )
    # A retired plan too: the buyer may have opened the payment page before
    # the plan was retired.
    plan = pricing.get(meta.get("plan_id") or "", retired=True)
    if account is None or plan is None:
        log.error("checkout %s: unknown account or plan %r", checkout.get("id"), meta)
        return None

    # Paying is also how most people sign in: Stripe has verified nothing about
    # the address, but it is where the receipt went, which is good enough to
    # be the account's identity.
    email = ((checkout.get("customer_details") or {}).get("email") or "").strip().lower()
    if email and account.email is None:
        account = accounts.adopt_email(session, account, email)

    session.add(
        Purchase(
            account_id=account.id,
            plan_id=plan.id,
            credits=plan.credits or 0,
            amount_cents=checkout.get("amount_total") or plan.price_cents,
            currency=checkout.get("currency") or plan.currency,
            stripe_session_id=checkout["id"],
        )
    )
    try:
        session.flush()
    except IntegrityError:
        session.rollback()  # already fulfilled by the other delivery path
        return _account_for(session, account.id, None)

    if checkout.get("customer") and not account.stripe_customer_id:
        account.stripe_customer_id = checkout["customer"]
    if plan.credits:
        account.purchased_credits += plan.credits
    session.commit()

    if checkout.get("subscription"):
        try:
            sub = _plain(_stripe().Subscription.retrieve(checkout["subscription"]))
            apply_subscription(session, sub)
        except Exception as exc:  # noqa: BLE001 - the subscription webhook will land too
            log.warning("could not read subscription %s: %s",
                        checkout["subscription"], exc)

    log.info("fulfilled %s: plan=%s account=%s", checkout["id"], plan.id, account.id)
    return account


def fulfil_by_id(session: Session, session_id: str) -> Account | None:
    """The browser's return trip: re-read the session from Stripe, then fulfil."""
    stripe = _stripe()
    try:
        checkout = _plain(stripe.checkout.Session.retrieve(session_id))
    except stripe.StripeError as exc:
        log.warning("could not retrieve checkout %s: %s", session_id, exc)
        return None
    return fulfil(session, checkout)


# ---------------------------------------------------------------------------
# webhooks
# ---------------------------------------------------------------------------

def verify_webhook(payload: bytes, signature: str | None) -> dict:
    """Check Stripe's signature and return the event as plain data."""
    stripe = _stripe()
    if not settings.stripe_webhook_secret:
        raise PaymentsUnavailable("No webhook signing secret is configured.")
    try:
        stripe.Webhook.construct_event(
            payload, signature or "", settings.stripe_webhook_secret
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise PaymentError("Bad webhook signature.") from exc
    return json.loads(payload)


def handle_event(session: Session, event: dict) -> None:
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if kind in ("checkout.session.completed",
                "checkout.session.async_payment_succeeded"):
        fulfil(session, obj)
    elif kind.startswith("customer.subscription."):
        apply_subscription(session, obj)
