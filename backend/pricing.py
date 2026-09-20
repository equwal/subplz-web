"""What we charge, and why.

Market as of 2026:

  Direct competitors (audiobook <-> ebook sync)
    Voxlight          $29.99/year, but alignment runs on the user's own Mac
    Spokt             freemium, metered sync minutes
    Storyteller       free, self-hosted; you administer a server
    syncabook         free CLI

  Adjacent (AI subtitling SaaS, priced for transcription)
    Otter             $16.99/mo
    Happy Scribe      $17/mo for 120 AI minutes, $89/mo for 6,000
    Veed              $22/mo
    Kapwing/Descript  $24/mo
    Sonix             $10/hour pay-as-you-go

The adjacent tools price per *minute of transcription*, which does not transfer:
a 10-hour audiobook is 600 minutes, so it would cost ~$100 at Sonix's rate and
need Happy Scribe's $89 tier. Forced alignment is much cheaper to run than
transcription - the model is tiny and its output is thrown away, since the
subtitle text comes from the user's own book.

So we price per book, cheap enough to be an impulse buy, and land the
subscription just under the general subtitling tools.

What is free and what is paid is split by output, not by quality. The free tier
is the complete product for someone reading along at home: the .srt for
HoshiReader and an .mkv with the subtitles built in. What costs money is the
clean .mp4 made for publishing on YouTube - the one output whose whole point is
an audience, and so the one whose users can be asked to pay. A credit buys one
book with every output, and skips the free tier's 24-hour wait.

  free              1 book / 24h, srt + mkv
  single book       $3.49
  5-book pack       $12.99   ($2.60/book)
  20-book pack      $39.99   ($2.00/book)
  unlimited month   $14.99   (under Otter/Happy Scribe, well under Veed/Kapwing)

Every number is overridable by env var; these are defaults, not decisions cast
in code.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from .settings import settings


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    # None for the subscription, which is not a credit pack.
    credits: int | None
    price_cents: int
    currency: str = "usd"
    recurring: bool = False
    blurb: str = ""

    @property
    def price_display(self) -> str:
        return f"${self.price_cents / 100:,.2f}"

    @property
    def per_book_cents(self) -> int | None:
        if not self.credits:
            return None
        return round(self.price_cents / self.credits)


DEFAULT_PLANS: list[Plan] = [
    Plan(
        id="single",
        name="One book",
        credits=1,
        price_cents=349,
        blurb="One book with the YouTube video, no waiting.",
    ),
    Plan(
        id="pack5",
        name="5 books",
        credits=5,
        price_cents=1299,
        blurb="Credits never expire.",
    ),
    Plan(
        id="pack20",
        name="20 books",
        credits=20,
        price_cents=3999,
        blurb="For working through a series.",
    ),
    Plan(
        id="unlimited",
        name="Unlimited monthly",
        credits=None,
        price_cents=1499,
        recurring=True,
        blurb="Every book, YouTube video included. Cancel any time.",
    ),
]


def plans() -> list[Plan]:
    """The catalogue, overridable with SUBPLZ_WEB_PLANS_JSON."""
    raw = os.environ.get("SUBPLZ_WEB_PLANS_JSON")
    if not raw:
        return DEFAULT_PLANS
    try:
        return [Plan(**item) for item in json.loads(raw)]
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"SUBPLZ_WEB_PLANS_JSON is not valid: {exc}") from exc


def get(plan_id: str) -> Plan | None:
    return next((p for p in plans() if p.id == plan_id), None)


def as_dicts() -> list[dict]:
    out = []
    for p in plans():
        d = asdict(p)
        d["price_display"] = p.price_display
        d["per_book_cents"] = p.per_book_cents
        out.append(d)
    return out


def free_tier_summary() -> str:
    n = settings.free_conversions
    hours = settings.free_window_hours
    book = "book" if n == 1 else "books"
    return f"{n} free {book} every {hours} hours"


# What each tier hands over, for the UI. Kinds match Artifact.kind.
TIER_OUTPUTS = {
    "free": ["srt", "video_embedded"],
    "youtube": ["srt", "video_embedded", "video"],
}
