"""Cloud captions for Subrep: hours of speech recognition, bought in packs.

Subrep makes live captions on the phone for free. With cloud captions, the app
sends each piece of speech here, and this server gives it to Groq
(whisper-large-v3-turbo), a larger model than a phone can run. The account
pays with hours from a pack.

  20 hours    $4.99
  80 hours    $16.99
  200 hours   $39.00

Groq bills about $0.04 for each audio hour, and at least 10 seconds for each
request. Pieces of speech are 1 to 11 seconds long, so one hour costs about
$0.08. The packs keep more than half of the price after that cost and the
Stripe fee (owner decision, 2026-09-22).

The server takes the seconds of a piece before it calls Groq, and gives them
back if Groq fails. So two requests at the same time cannot spend one second
twice. The server does not keep the sound or the text.

A pack is a pricing.Plan, so payments.py sells it the same way as a book pack.
The packs are not in pricing.plans(): the book page does not show them.
"""
from __future__ import annotations

import io
import json
import logging
import math
import secrets
import urllib.error
import urllib.request
import wave
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import ForeignKey, Integer, String, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from .api import get_account, get_session
from .db import Account, Base
from .pricing import Plan
from .settings import settings

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # 16-bit mono

PACKS: list[Plan] = [
    Plan(id="captions20", name="20 hours of cloud captions", credits=0, price_cents=499),
    Plan(id="captions80", name="80 hours of cloud captions", credits=0, price_cents=1699),
    Plan(id="captions200", name="200 hours of cloud captions", credits=0, price_cents=3900),
]
HOURS = {"captions20": 20, "captions80": 80, "captions200": 200}


class CaptionBalance(Base):
    """The seconds of cloud captions that an account has."""

    __tablename__ = "caption_balances"

    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("accounts.id"), primary_key=True
    )
    seconds_left: Mapped[int] = mapped_column(Integer, default=0)
    seconds_used: Mapped[int] = mapped_column(Integer, default=0)


def pack(plan_id: str) -> Plan | None:
    return next((p for p in PACKS if p.id == plan_id), None)


def seconds_left(session: Session, account_id: str) -> int:
    row = session.get(CaptionBalance, account_id)
    return row.seconds_left if row else 0


def add(session: Session, account_id: str, seconds: int) -> None:
    """Give `seconds` to the account. The caller commits."""
    row = session.get(CaptionBalance, account_id)
    if row is None:
        row = CaptionBalance(account_id=account_id, seconds_left=0, seconds_used=0)
        session.add(row)
    row.seconds_left += seconds


def spend(session: Session, account_id: str, seconds: int) -> bool:
    """Take `seconds` from the account, and commit. False if it has fewer."""
    done = session.execute(
        update(CaptionBalance)
        .where(CaptionBalance.account_id == account_id,
               CaptionBalance.seconds_left >= seconds)
        .values(seconds_left=CaptionBalance.seconds_left - seconds,
                seconds_used=CaptionBalance.seconds_used + seconds)
    ).rowcount
    session.commit()
    return done == 1


def refund(session: Session, account_id: str, seconds: int) -> None:
    session.execute(
        update(CaptionBalance)
        .where(CaptionBalance.account_id == account_id)
        .values(seconds_left=CaptionBalance.seconds_left + seconds,
                seconds_used=CaptionBalance.seconds_used - seconds)
    )
    session.commit()


def fulfil(session: Session, account: Account, plan: Plan) -> None:
    """Called by payments.fulfil for a paid pack. The caller commits."""
    if plan.id in HOURS:
        add(session, account.id, HOURS[plan.id] * 3600)


def merge(session: Session, src: Account, dst: Account) -> None:
    """Called by accounts.merge: the hours of `src` go to `dst`."""
    row = session.get(CaptionBalance, src.id)
    if row is None or row.seconds_left == 0:
        return
    add(session, dst.id, row.seconds_left)
    row.seconds_left = 0


def billed_seconds(pcm_bytes: int) -> int:
    """The seconds that a piece of `pcm_bytes` costs: its length, rounded up."""
    return max(1, math.ceil(pcm_bytes / BYTES_PER_SECOND))


def wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return out.getvalue()


class TranscribeError(RuntimeError):
    pass


def transcribe(pcm: bytes, language: str) -> str:
    """Send one piece of speech to Groq. Returns its text."""
    boundary = secrets.token_hex(16)
    fields = [("model", settings.caption_model), ("response_format", "json"),
              ("temperature", "0")]
    if language and language != "auto":
        fields.append(("language", language))
    body = io.BytesIO()
    for name, value in fields:
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                   f"\r\n\r\n{value}\r\n".encode())
    body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
               f'filename="piece.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
    body.write(wav(pcm))
    body.write(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        settings.caption_api_url, data=body.getvalue(), method="POST",
        headers={"Authorization": f"Bearer {settings.groq_api_key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response).get("text", "").strip()
    except urllib.error.HTTPError as exc:
        raise TranscribeError(f"speech service answered {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise TranscribeError(f"speech service unreachable: {exc}") from exc


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/captions")


class PackIn(BaseModel):
    pack_id: str


def _state(session: Session, account: Account) -> dict:
    return {
        "account_id": account.id,
        "seconds_left": seconds_left(session, account.id),
        "available": bool(settings.groq_api_key) and settings.payments_configured,
        "packs": [
            {"id": p.id, "name": p.name, "hours": HOURS[p.id],
             "price_display": p.price_display}
            for p in PACKS
        ],
    }


@router.get("")
def caption_state(
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    return _state(session, account)


@router.post("/checkout")
def caption_checkout(
    body: PackIn,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    from . import payments

    plan = pack(body.pack_id)
    if plan is None:
        raise HTTPException(404, "No such pack.")
    try:
        return {"url": payments.start_checkout(session, account, plan)}
    except payments.PaymentsUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except payments.PaymentError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/transcribe")
async def caption_transcribe(
    request: Request,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
    lang: Annotated[str, Query(max_length=8)] = "auto",
):
    """The body is one piece of speech: 16 kHz mono 16-bit little-endian PCM."""
    if not settings.groq_api_key:
        raise HTTPException(503, "Cloud captions are not set up on this server.")
    pcm = await request.body()
    if not pcm or len(pcm) % 2:
        raise HTTPException(400, "The body must be 16-bit PCM.")
    if len(pcm) > settings.caption_max_seconds * BYTES_PER_SECOND:
        raise HTTPException(413, f"A piece is at most {settings.caption_max_seconds} s.")

    cost = billed_seconds(len(pcm))
    if not spend(session, account.id, cost):
        raise HTTPException(402, "No cloud caption hours left.")
    try:
        text = await run_in_threadpool(transcribe, pcm, lang)
    except TranscribeError as exc:
        refund(session, account.id, cost)
        log.warning("caption piece for %s failed: %s", account.id, exc)
        raise HTTPException(502, str(exc)) from exc
    return {"text": text, "seconds": cost,
            "seconds_left": seconds_left(session, account.id)}
