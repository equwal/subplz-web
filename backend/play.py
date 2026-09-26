"""Google Play purchases of cloud caption hours: the Play build of Subrep.

Google Play requires its own billing for digital goods that an app from Google
Play sells. So the Play build of Subrep buys the hour packs of captions.PACKS
with Google Play Billing. The Play product ids are the pack ids.

The app sends each purchase token here with its device cookie. This module asks
Google for the purchase, adds the hours of the pack to the account once, and
consumes the purchase, so that the user can buy the pack again. The app never
consumes. If this server does not answer, the app sends the purchase again at
its next start.
"""
from __future__ import annotations

import functools
import hashlib
import logging
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import accounts, captions
from .api import get_account, get_session
from .db import Account, Base, new_id, utcnow
from .settings import settings

log = logging.getLogger(__name__)

ANDROIDPUBLISHER = "https://www.googleapis.com/auth/androidpublisher"
API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
PACKAGE = "com.honjimaku.subrep"
NOT_SET_UP = "Google Play purchases are not set up on this server."
NO_ANSWER = "Google Play did not answer. Try again later."
# purchases.products.get numbers the states of a purchase in another way than
# the Android library, where PURCHASED is 1. Do not mix the two.
PURCHASED = 0  # purchaseState
CONSUMED = 1  # consumptionState
TEST = 0  # purchaseType: a license tester bought it, and paid nothing


class PlayError(Exception):
    """A purchase that gives no hours now. `status` is the HTTP answer for the app."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class PlayPurchase(Base):
    """Ledger of Google Play purchases.

    The unique token hash makes the credit idempotent. The app sends a purchase
    again after a crash or a lost answer, and the hours must come only once.
    Only the hash is kept: no code needs the token after the credit.
    """

    __tablename__ = "play_purchases"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("play")
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    product_id: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    seconds: Mapped[int] = mapped_column(Integer)
    test: Mapped[bool] = mapped_column(Boolean, default=False)
    # The Google order number on the receipt of the buyer (GPA....). Support
    # finds the account with it. A purchase with a promo code has none.
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# the Google Play Developer API
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _credentials(path: str):
    """Read the key file once. A missing or bad file is PlayError 503."""
    from google.auth import exceptions
    from google.oauth2 import service_account

    try:
        return service_account.Credentials.from_service_account_file(
            path, scopes=[ANDROIDPUBLISHER]
        )
    except (OSError, ValueError, exceptions.DefaultCredentialsError) as exc:
        # Log only the type: the message can hold parts of the file.
        log.error("cannot read the Google Play key file %s: %s", path, type(exc).__name__)
        raise PlayError(503, NOT_SET_UP) from exc


def _session():
    """An HTTP session that signs each request. Tests replace this function."""
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(_credentials(settings.play_service_account_file))


def _url(product_id: str, token: str) -> str:
    return (f"{API}/{PACKAGE}/purchases/products/{quote(product_id, safe='')}"
            f"/tokens/{quote(token, safe='')}")


def _call(method: str, url: str):
    """Send one request to Google. Never log `url`: it can hold a purchase token."""
    import requests
    from google.auth import exceptions

    try:
        with _session() as http:
            response = http.request(method, url, timeout=15)
    except exceptions.RefreshError as exc:
        log.error("Google refused the Play key: %s", type(exc).__name__)
        raise PlayError(503, NOT_SET_UP) from exc
    except (requests.RequestException, exceptions.TransportError) as exc:
        raise PlayError(502, NO_ANSWER) from exc
    if response.status_code in (200, 204):
        return response
    if response.status_code in (400, 404, 410):
        raise PlayError(400, "Google Play does not know this purchase.")
    if response.status_code in (401, 403):
        log.error("the Play key has no access to the orders of %s (%d)",
                  PACKAGE, response.status_code)
        raise PlayError(503, NOT_SET_UP)
    raise PlayError(502, NO_ANSWER)


def get(product_id: str, token: str) -> dict:
    """The purchase as Google sees it (purchases.products.get)."""
    return _call("GET", _url(product_id, token)).json()


def consume(product_id: str, token: str) -> None:
    """Close the purchase, so that the user can buy the pack again."""
    _call("POST", _url(product_id, token) + ":consume")


# ---------------------------------------------------------------------------
# the credit
# ---------------------------------------------------------------------------

def credit(session: Session, account: Account, product_id: str, token: str,
           purchase: dict) -> bool:
    """Add the hours of one purchase that Google returned, once for each token.

    Return True when this call added them, and False when an earlier call did.
    Raise PlayError when the purchase gives no hours.
    """
    if purchase.get("purchaseState") != PURCHASED:
        raise PlayError(409, "The purchase is not complete yet.")
    if purchase.get("productId", product_id) != product_id:
        raise PlayError(400, "Google Play does not know this purchase.")
    owner_id = purchase.get("obfuscatedExternalAccountId")
    # A purchase made outside the app, for example with a promo code, has no
    # account id. Google says that the app can give it to the user who sends it.
    if owner_id:
        owner = session.get(Account, owner_id)
        if owner is None or accounts.resolve(session, owner).id != account.id:
            raise PlayError(403, "This purchase belongs to another account.")
    quantity = int(purchase.get("quantity") or 1)
    seconds = captions.HOURS[product_id] * 3600 * quantity
    test = purchase.get("purchaseType") == TEST
    session.add(PlayPurchase(
        token_hash=_hash(token), account_id=account.id, product_id=product_id,
        quantity=quantity, seconds=seconds, test=test, order_id=purchase.get("orderId"),
    ))
    try:
        session.flush()
    except IntegrityError:
        session.rollback()  # An earlier call added this token.
        return False
    if purchase.get("consumptionState") == CONSUMED:
        # Only this server consumes, and it commits before it consumes. So a
        # consumed token without a row means that the ledger lost the row.
        session.rollback()
        log.warning("consumed Play purchase %s has no ledger row",
                    purchase.get("orderId") or "(no order id)")
        raise PlayError(409, "This purchase was used already.")
    captions.add(session, account.id, seconds)
    session.commit()
    log.info("play purchase %s: %s x%d for %s%s",
             purchase.get("orderId") or "(no order id)", product_id, quantity,
             account.id, " (test)" if test else "")
    return True


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/captions")


class PlayIn(BaseModel):
    product_id: str = Field(max_length=64)
    purchase_token: str = Field(min_length=1, max_length=4096)


@router.post("/play-purchase")
def play_purchase(
    body: PlayIn,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    """The Play build sends each purchase here, once or more. It gives hours once."""
    if not settings.play_configured:
        raise HTTPException(503, NOT_SET_UP)
    if body.product_id not in captions.HOURS:
        raise HTTPException(404, "No such pack.")
    try:
        purchase = get(body.product_id, body.purchase_token)
        credit(session, account, body.product_id, body.purchase_token, purchase)
    except PlayError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    if purchase.get("consumptionState") != CONSUMED:
        try:
            consume(body.product_id, body.purchase_token)
        except PlayError as exc:
            # The hours are in the account. The next send of the app consumes it.
            log.warning("could not consume a Play purchase of %s: %s", account.id, exc.detail)
    return {"seconds_left": captions.seconds_left(session, account.id)}
