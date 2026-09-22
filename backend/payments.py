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
from urllib.parse import quote

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

def _catalogue_price(stripe, plan: pricing.Plan) -> str | None:
    """The id of the Stripe price of a recurring plan (lookup key = plan id).

    The customer portal switches a subscription only between prices of the
    Stripe catalogue, so a plan checks out with its catalogue price when one
    exists. A price that does not match the plan is not used.
    tools/stripe_plans.py makes the prices.
    """
    try:
        found = _plain(stripe.Price.list(lookup_keys=[plan.id], active=True, limit=1))
    except stripe.StripeError as exc:
        log.warning("could not read the Stripe price of %s: %s", plan.id, exc)
        return None
    for price in found.get("data") or []:
        if (price.get("unit_amount") == plan.price_cents
                and price.get("currency") == plan.currency
                and (price.get("recurring") or {}).get("interval") == plan.interval):
            return price["id"]
        log.warning("Stripe price %s does not match plan %s", price.get("id"), plan.id)
    return None


def ensure_plan_prices() -> list[tuple[str, str, bool]]:
    """Make a Stripe price for each monthly plan that has none.

    Safe to run again: a plan with a matching price keeps it. Returns
    (plan id, price id, made now) for each monthly plan. tools/stripe_plans.py
    runs this.
    """
    stripe = _stripe()
    out = []
    for plan in pricing.plans():
        if not plan.recurring:
            continue
        price_id = _catalogue_price(stripe, plan)
        made = price_id is None
        if made:
            price = _plain(stripe.Price.create(
                unit_amount=plan.price_cents,
                currency=plan.currency,
                recurring={"interval": plan.interval},
                lookup_key=plan.id,
                # A price that does not match keeps its product; move the key.
                transfer_lookup_key=True,
                product_data={"name": f"{settings.site_name} - {plan.name}"},
            ))
            price_id = price["id"]
        out.append((plan.id, price_id, made))
    return out


def start_checkout(session: Session, account: Account, plan: pricing.Plan,
                   page: str = "/") -> str:
    """Return the Stripe-hosted payment page for `plan`.

    `page` is the page of this site that the browser comes back to. It must be
    one of api.RETURN_PAGES.
    """
    stripe = _stripe()

    # One book plan at a time. A Subrep plan (subrep.py) is not a book plan.
    if plan.recurring and pricing.get(plan.id, retired=True) is not None \
            and billing.is_subscribed(account):
        raise PaymentError(
            "You already have a monthly plan. Change or cancel it with Manage plan."
        )

    price = {
        "currency": plan.currency,
        "unit_amount": plan.price_cents,
        "product_data": {"name": f"{settings.site_name} - {plan.name}"},
    }
    if plan.recurring:
        price["recurring"] = {"interval": plan.interval}

    line: dict = {"quantity": 1, "price_data": price}
    if plan.recurring:
        price_id = _catalogue_price(stripe, plan)
        if price_id:
            line = {"quantity": 1, "price": price_id}

    meta = {"account_id": account.id, "plan_id": plan.id}
    back = "" if page == "/" else f"&page={quote(page, safe='')}"
    params: dict = {
        "mode": "subscription" if plan.recurring else "payment",
        "line_items": [line],
        "client_reference_id": account.id,
        "metadata": meta,
        # Stripe substitutes the real id; the return handler re-reads the
        # session from it rather than trusting anything in the URL.
        "success_url": f"{_base()}/api/billing/return"
                       "?session_id={CHECKOUT_SESSION_ID}" + back,
        "cancel_url": f"{_base()}{page}?checkout=cancelled",
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
    """Stripe's own page for cancelling a monthly plan or changing the card."""
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


def _our_plan_id(sub: dict) -> str | None:
    """The monthly plan of a subscription, or None if it is not one of ours.

    The lookup key of the price names the plan, and it changes when the
    customer switches plans in the portal. A subscription that started with
    an inline price has no lookup key: its checkout metadata names the plan.
    The Stripe account also sells other products, and their events come here
    too. A subscription that names no monthly plan of this site is not ours.
    """
    items = (sub.get("items") or {}).get("data") or []
    price = (items[0].get("price") or {}) if items else {}
    for plan_id in (price.get("lookup_key"), (sub.get("metadata") or {}).get("plan_id")):
        plan = pricing.get(plan_id or "", retired=True)
        if plan is not None and plan.recurring:
            return plan.id
    return None


def apply_subscription(session: Session, sub: dict) -> None:
    """Mirror a Stripe subscription onto its account."""
    from . import subrep  # subrep imports api, which imports this module

    if subrep.apply_subscription(session, sub):
        return
    plan_id = _our_plan_id(sub)
    if plan_id is None:
        log.info("subscription %s is not for a plan of this site; ignored", sub.get("id"))
        return
    meta = sub.get("metadata") or {}
    account = _account_for(session, meta.get("account_id"), sub.get("customer"))
    if account is None:
        log.warning("subscription %s matches no account", sub.get("id"))
        return
    # An old subscription being deleted must not wipe out its replacement.
    if account.subscription_id and account.subscription_id != sub.get("id") \
            and sub.get("status") in ("canceled", "incomplete_expired"):
        return
    period_end = _period_end(sub)
    # A new subscription, or a new month of it, starts the count of its books
    # again: the books of one month do not carry over.
    if account.subscription_id != sub.get("id") \
            or billing._aware(account.subscription_period_end) != period_end:
        account.subscription_credits_used = 0
    account.subscription_id = sub.get("id")
    account.subscription_status = sub.get("status")
    account.subscription_period_end = period_end
    # A switch in the portal changes the plan and keeps the month: the books
    # used this month still count against the new plan.
    account.subscription_plan_id = plan_id
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
    from . import captions, subrep  # they import api, which imports this module

    plan = (pricing.get(meta.get("plan_id") or "", retired=True)
            or captions.pack(meta.get("plan_id") or "")
            or subrep.plan(meta.get("plan_id") or ""))
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
    # A pack adds credits that never expire. A monthly plan adds none here:
    # its books come from the subscription, month by month.
    if plan.credits and not plan.recurring:
        account.purchased_credits += plan.credits
    captions.fulfil(session, account, plan)
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
