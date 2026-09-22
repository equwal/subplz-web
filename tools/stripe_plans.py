"""Make the Stripe prices of the monthly plans.

The Stripe customer portal switches a subscription only between prices of the
Stripe catalogue. This makes one monthly price, with its product, for each
monthly plan in backend/pricing.py. The plan id is the lookup key of the price.
It is safe to run again: a plan that has a matching price keeps it.

    # on the server, from the checkout, with the server's settings
    runuser -u subplz -- .venv/bin/python tools/stripe_plans.py

Then let the customer portal switch between these prices (Stripe: Settings ->
Billing -> Customer portal -> customers can switch plans).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import payments  # noqa: E402


def main() -> int:
    try:
        rows = payments.ensure_plan_prices()
    except payments.PaymentsUnavailable as exc:
        print(exc)
        return 1
    for plan_id, price_id, made in rows:
        print(f"{plan_id}: {price_id} ({'made now' if made else 'already there'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
