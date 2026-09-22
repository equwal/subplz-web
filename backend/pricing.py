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

  Raw forced alignment as an API
    ElevenLabs        $0.22 per hour of audio: $2.20 for a 10-hour book

The adjacent tools price per *minute of transcription*, which does not transfer:
a 10-hour audiobook is 600 minutes, so it would cost ~$100 at Sonix's rate.

What is free and what is paid is split by where the work is done. In the
visitor's browser a conversion costs this server nothing: it is free, without
limit, with each output. On this server's hardware (Whisper tiny on a CPU, one
book at a time: about 2 hours for a 10-hour book, from any device) a book takes
one credit. The code is public, so what is sold is the use of these machines
and nothing else.

A book costs about $0.64 to convert on a rented GPU. Above about $5 a technical
buyer wraps the ElevenLabs API; below $3 the fixed card fee takes too much.
There is no unlimited plan: use comes in bursts (a backlog, then nothing), and
one heavy user of an unlimited plan costs more than the plan brings in.

  free              in the browser: no limit, each output
  10-book pack      $4.99    ($0.50/book)
  100-book pack     $39.99   ($0.40/book, 20% off: the middle of what credit
                             packs give at 10x volume)
  500-book pack     $174.99  ($0.35/book, 30% off)

The owner set $4.99 for ten books on 2026-09-22, and asked for bigger packs the
same day. The owner chose to sell the 500-book pack against the advice of the
financial advocate: one such sale can hold this one CPU server for about six
weeks, so the pack says how fast the server works. The packs sold before that
(one book $4.99, five $16.99, twenty $39) are in RETIRED_PLANS.

Every number is overridable by env var; these are defaults, not decisions cast
in code. An operator who wants a recurring plan can add one with
SUBPLZ_WEB_PLANS_JSON ("recurring": true, "credits": null).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass



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
        id="pack10",
        name="10 books",
        credits=10,
        price_cents=499,
        blurb="Ten books on our server, from any device. Credits never expire.",
    ),
    Plan(
        id="pack100",
        name="100 books",
        credits=100,
        price_cents=3999,
        blurb="For a whole library. Our server does one book at a time: about "
              "2 hours for a 10-hour book. Credits never expire.",
    ),
    Plan(
        id="pack500",
        name="500 books",
        credits=500,
        price_cents=17499,
        blurb="For a very large library. Our server does one book at a time: "
              "about 2 hours for a 10-hour book, so 500 long books take weeks. "
              "Credits never expire.",
    ),
]

# Not for sale. A checkout that started before a plan was retired can still
# finish, and it must credit what the buyer saw on the payment page.
RETIRED_PLANS: list[Plan] = [
    Plan(id="single", name="One book", credits=1, price_cents=499),
    Plan(id="pack5", name="5 books", credits=5, price_cents=1699),
    Plan(id="pack20", name="20 books", credits=20, price_cents=3900),
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


def get(plan_id: str, retired: bool = False) -> Plan | None:
    """A plan for sale. With `retired`, also a plan that is no longer sold."""
    pool = plans() + (RETIRED_PLANS if retired else [])
    return next((p for p in pool if p.id == plan_id), None)


def as_dicts() -> list[dict]:
    out = []
    for p in plans():
        d = asdict(p)
        d["price_display"] = p.price_display
        d["per_book_cents"] = p.per_book_cents
        out.append(d)
    return out


def free_tier_summary() -> str:
    return "free in your browser, without limit"


# What each tier hands over, for the UI. Kinds match Artifact.kind. The tiers
# differ in where the work is done, not in what comes out.
_OUTPUTS = ["srt", "video_embedded", "video"]
TIER_OUTPUTS = {"free": _OUTPUTS, "cloud": _OUTPUTS}
