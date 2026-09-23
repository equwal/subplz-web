"""Subrep Pro: the plan of the Subrep desktop app, sold with the accounts of this site.

  yearly   $9.99 a year (first on the page)
  monthly  $0.99 a month

A Stripe subscription makes the account Pro. The desktop app does not have the
cookie of the browser: the page gives a link command with the account id and a
refresh key, and the app sends the two to /api/subrep/refresh. The answer is a
licence token that the app checks offline with the public key in its build.
A Subrep plan is not a book plan: an account can have both, or one of them.
"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
import stripe
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from backend import accounts, subrep
from backend.db import Account, SessionLocal
from backend.settings import settings

from .conftest import FakeStripeObject, account_id, checkout_event, post_webhook

SEED = bytes(range(32))  # a fixed key for the tests, never the key of a server
PRIVATE_KEY = base64.urlsafe_b64encode(SEED).rstrip(b"=").decode()
PUBLIC_KEY = Ed25519PrivateKey.from_private_bytes(SEED).public_key()
DAY = 86400


@pytest.fixture(autouse=True)
def licence_key(monkeypatch):
    monkeypatch.setattr(settings, "subrep_license_key", PRIVATE_KEY)


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify(token: str) -> dict:
    """The check of the desktop app (verify_token in subrep/licensing.py)."""
    payload, sig = token.split(".")
    PUBLIC_KEY.verify(b64d(sig), b64d(payload))
    return json.loads(b64d(payload))


def sub_object(account, plan_id, status="active", period_end=None, sub_id=None,
               price_key=None) -> dict:
    item: dict = {"current_period_end": period_end or int(time.time()) + 30 * DAY}
    if price_key:
        item["price"] = {"id": f"price_{price_key}", "lookup_key": price_key}
    return {
        "id": sub_id or f"sub_subrep_{account}", "object": "subscription",
        "status": status, "customer": f"cus_{account}",
        "metadata": {"account_id": account, "plan_id": plan_id},
        "items": {"data": [item]},
    }


def sub_event(kind: str, obj: dict) -> dict:
    return {"id": f"evt_{kind}_{time.time_ns()}", "type": f"customer.subscription.{kind}",
            "data": {"object": obj}}


def buy(client, monkeypatch, plan_id="subrep_year", email=None) -> str:
    """Pay for Subrep Pro, as the checkout webhook tells it. Returns the account id."""
    acct = account_id(client)
    sub = sub_object(acct, plan_id)
    monkeypatch.setattr(stripe.Subscription, "retrieve", lambda sid: FakeStripeObject(sub))
    event = checkout_event(f"cs_{time.time_ns()}", acct, plan_id,
                           email=email or f"pro-{time.time_ns()}@example.com",
                           subscription=sub["id"])
    assert post_webhook(client, event).status_code == 200
    return acct


def state(client) -> dict:
    return client.get("/api/subrep").json()


def refresh(client, account: str, key: str):
    return client.post("/api/subrep/refresh", json={"account": account, "refresh_key": key})


def fake_checkout(monkeypatch) -> dict:
    seen: dict = {}
    monkeypatch.setattr(
        stripe.checkout.Session, "create",
        lambda **p: seen.update(p) or FakeStripeObject({"url": "https://stripe.test/pro"}),
    )
    return seen


# --- plans and checkout ------------------------------------------------------

def test_the_page_offers_the_yearly_plan_first_then_the_monthly_plan(client):
    plans = state(client)["plans"]
    assert [(p["id"], p["price_cents"], p["interval"]) for p in plans] == [
        ("subrep_year", 999, "year"), ("subrep_month", 99, "month")]


def test_the_book_page_does_not_show_subrep_pro(client):
    ids = {p["id"] for p in client.get("/api/pricing").json()["plans"]}
    assert not ids & {p.id for p in subrep.PLANS}


def test_a_plan_checks_out_as_a_subscription_of_its_interval(client, monkeypatch):
    seen = fake_checkout(monkeypatch)
    r = client.post("/api/subrep/checkout", json={"plan_id": "subrep_year"})
    assert r.status_code == 200 and r.json()["url"] == "https://stripe.test/pro"
    assert seen["mode"] == "subscription"
    price = seen["line_items"][0]["price_data"]
    assert price["recurring"] == {"interval": "year"} and price["unit_amount"] == 999
    assert seen["subscription_data"]["metadata"]["plan_id"] == "subrep_year"


def test_the_checkout_comes_back_to_the_subrep_page(client, monkeypatch):
    seen = fake_checkout(monkeypatch)
    client.post("/api/subrep/checkout", json={"plan_id": "subrep_month"})
    assert seen["cancel_url"] == "https://example.test/subrep.html?checkout=cancelled"
    success = urlparse(seen["success_url"])
    assert success.path == "/api/billing/return"
    assert parse_qs(success.query)["page"] == ["/subrep.html"]


def test_the_return_trip_goes_only_to_a_known_page(client, monkeypatch):
    monkeypatch.setattr(stripe.checkout.Session, "retrieve",
                        lambda sid: FakeStripeObject({"id": sid, "payment_status": "unpaid"}))
    r = client.get("/api/billing/return?session_id=cs_x&page=/subrep.html",
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/subrep.html?checkout=pending"
    r = client.get("/api/billing/return?session_id=cs_x&page=https://evil.test/",
                   follow_redirects=False)
    assert r.headers["location"] == "/?checkout=pending"


def test_a_book_plan_does_not_stop_a_subrep_checkout(client, monkeypatch):
    acct = account_id(client)
    books = sub_object(acct, "month10", sub_id=f"sub_books_{acct}")
    assert post_webhook(client, sub_event("created", books)).status_code == 200
    assert client.get("/api/account").json()["subscribed"] is True
    fake_checkout(monkeypatch)
    assert client.post("/api/subrep/checkout", json={"plan_id": "subrep_month"}).status_code == 200


def test_a_second_subrep_plan_is_refused(client, monkeypatch):
    buy(client, monkeypatch)
    fake_checkout(monkeypatch)
    r = client.post("/api/subrep/checkout", json={"plan_id": "subrep_month"})
    assert r.status_code == 400 and "Manage plan" in r.json()["detail"]


def test_only_a_subrep_plan_is_sold_here(client):
    assert client.post("/api/subrep/checkout", json={"plan_id": "month10"}).status_code == 404


def test_checkout_needs_a_verified_email_when_the_server_can_send_one(client, monkeypatch):
    # A $0.99 checkout draws card testing. With email sign-in, only a verified
    # account may pay. Without it, the checks of Stripe are all there is.
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    fake_checkout(monkeypatch)
    assert state(client)["needs_sign_in"] is True
    assert client.post("/api/subrep/checkout", json={"plan_id": "subrep_year"}).status_code == 403
    with SessionLocal() as s:
        row = s.get(Account, account_id(client))
        row.email, row.email_verified = f"v-{time.time_ns()}@example.com", 1
        s.commit()
    assert client.post("/api/subrep/checkout", json={"plan_id": "subrep_year"}).status_code == 200


def test_a_server_without_a_licence_key_sells_nothing(client, monkeypatch):
    monkeypatch.setattr(settings, "subrep_license_key", "")
    assert state(client)["available"] is False
    assert client.post("/api/subrep/checkout", json={"plan_id": "subrep_year"}).status_code == 503


# --- Pro, the link command and the licence -------------------------------------

def test_paying_makes_the_account_pro_and_gives_a_link_command(client, monkeypatch):
    acct = buy(client, monkeypatch)
    s = state(client)
    assert s["pro"] is True and s["plan_id"] == "subrep_year"
    link = s["link"]
    assert link["account"] == acct and link["server"] == "https://example.test/api/subrep"
    assert link["command"] == (
        f'subrep license --link "{acct}" "{link["refresh_key"]}" "{link["server"]}"')


def test_the_page_shows_the_same_link_command_on_each_visit(client, monkeypatch):
    # Each computer of the customer runs the same command.
    buy(client, monkeypatch)
    assert state(client)["link"] == state(client)["link"]


def test_a_subrep_plan_does_not_touch_the_book_plan(client, monkeypatch):
    buy(client, monkeypatch)
    a = client.get("/api/account").json()
    assert a["subscribed"] is False and a["plan_credits"] == 0


def test_the_desktop_app_gets_a_licence_that_the_public_key_verifies(client, monkeypatch):
    email = f"desk-{time.time_ns()}@example.com"
    acct = buy(client, monkeypatch, email=email)
    r = refresh(client, acct, state(client)["link"]["refresh_key"])
    assert r.status_code == 200
    claims = verify(r.json()["token"])
    assert claims["tier"] == "pro" and claims["sub"] == email
    assert time.time() < claims["exp"] <= time.time() + 31 * DAY


def test_a_licence_ends_with_the_paid_period(client, monkeypatch):
    acct = buy(client, monkeypatch, plan_id="subrep_month")
    end = int(time.time()) + 3 * DAY
    post_webhook(client, sub_event("updated", sub_object(acct, "subrep_month", period_end=end)))
    token = refresh(client, acct, state(client)["link"]["refresh_key"]).json()["token"]
    assert verify(token)["exp"] == end


def test_a_yearly_licence_ends_within_31_days(client, monkeypatch):
    # The app asks again before then, so a refund ends Pro within a month.
    acct = buy(client, monkeypatch)
    post_webhook(client, sub_event("updated", sub_object(
        acct, "subrep_year", period_end=int(time.time()) + 365 * DAY)))
    token = refresh(client, acct, state(client)["link"]["refresh_key"]).json()["token"]
    assert verify(token)["exp"] <= time.time() + 31 * DAY


def test_a_wrong_refresh_key_gets_no_licence(client, monkeypatch):
    acct = buy(client, monkeypatch)
    key = state(client)["link"]["refresh_key"]  # the account has a key now
    r = refresh(client, acct, key[:-1] + ("A" if key[-1] != "A" else "B"))
    assert r.status_code == 403 and "error" in r.json()
    assert refresh(client, acct, key).status_code == 200


def test_an_account_without_a_link_command_gets_no_licence(client, monkeypatch):
    acct = buy(client, monkeypatch)  # the page was never opened: no key yet
    assert refresh(client, acct, "").status_code == 403


def test_the_control_panel_links_with_the_email_and_the_setup_key(client, monkeypatch):
    # The Licence card of the desktop control panel asks for the email that
    # paid and the setup key (the refresh key). The email names the account.
    email = f"Panel-{time.time_ns()}@Example.com"
    buy(client, monkeypatch, email=email)
    link = state(client)["link"]
    assert link["email"] == email.lower()
    r = refresh(client, email, link["refresh_key"])
    assert r.status_code == 200 and verify(r.json()["token"])["sub"] == email.lower()


def test_another_email_gets_no_licence(client, monkeypatch):
    buy(client, monkeypatch)
    key = state(client)["link"]["refresh_key"]
    assert refresh(client, f"other-{time.time_ns()}@example.com", key).status_code == 403


def test_a_licence_says_when_the_paid_period_ends(client, monkeypatch):
    # The desktop app shows "paid until" from the "paid" claim. The token
    # itself ends after TOKEN_DAYS at most.
    acct = buy(client, monkeypatch)
    end = int(time.time()) + 365 * DAY
    post_webhook(client, sub_event("updated", sub_object(acct, "subrep_year", period_end=end)))
    claims = verify(refresh(client, acct, state(client)["link"]["refresh_key"]).json()["token"])
    assert claims["paid"] == end and claims["exp"] < end


def test_a_cancelled_plan_gets_no_licence(client, monkeypatch):
    acct = buy(client, monkeypatch, plan_id="subrep_month")
    key = state(client)["link"]["refresh_key"]
    ended = sub_object(acct, "subrep_month", status="canceled")
    assert post_webhook(client, sub_event("deleted", ended)).status_code == 200
    s = state(client)
    assert s["pro"] is False and s["link"] is None
    r = refresh(client, acct, key)
    assert r.status_code == 402 and "error" in r.json()


def test_a_switch_in_the_portal_changes_the_plan(client, monkeypatch):
    acct = buy(client, monkeypatch, plan_id="subrep_month")
    post_webhook(client, sub_event("updated", sub_object(
        acct, "subrep_month", price_key="subrep_year")))
    assert state(client)["plan_id"] == "subrep_year"


def test_the_end_of_an_old_subscription_does_not_end_its_replacement(client, monkeypatch):
    acct = buy(client, monkeypatch)
    old = sub_object(acct, "subrep_month", status="canceled", sub_id="sub_old")
    post_webhook(client, sub_event("deleted", old))
    assert state(client)["pro"] is True


def test_merging_accounts_keeps_the_licence(client, monkeypatch):
    acct = buy(client, monkeypatch)
    with SessionLocal() as s:
        owner = accounts.new_account(s)
        accounts.merge(s, s.get(Account, acct), owner)
        s.commit()
        assert subrep.is_pro(subrep.licence(s, owner.id))
        assert subrep.licence(s, acct) is None


def test_the_link_command_of_a_merged_account_still_works(client, monkeypatch):
    acct = buy(client, monkeypatch)
    key = state(client)["link"]["refresh_key"]
    with SessionLocal() as s:
        owner = accounts.new_account(s)
        accounts.merge(s, s.get(Account, acct), owner)
        s.commit()
    assert refresh(client, acct, key).status_code == 200


# --- the iOS waitlist ------------------------------------------------------------

def test_the_ios_waitlist_keeps_one_entry_for_each_email(client):
    email = f"Wait-{time.time_ns()}@Example.com"
    for _ in range(2):
        r = client.post("/api/subrep/waitlist", json={"email": email})
        assert r.status_code == 200 and r.json()["joined"] is True
    with SessionLocal() as s:
        rows = s.query(subrep.WaitlistEntry).filter_by(email=email.lower()).all()
        assert [row.kind for row in rows] == ["ios"]
        assert subrep.waitlist_count(s, "ios") >= 1


def test_the_waitlist_refuses_what_is_not_an_email(client):
    assert client.post("/api/subrep/waitlist", json={"email": "nope"}).status_code == 400


def test_there_is_no_waitlist_for_an_unknown_app(client):
    r = client.post("/api/subrep/waitlist", json={"email": "a@example.com", "kind": "tv"})
    assert r.status_code == 404


# --- the token format ------------------------------------------------------------

claims_st = st.dictionaries(
    st.text(min_size=1, max_size=12),
    st.one_of(st.integers(), st.text(max_size=20), st.booleans()),
    max_size=6,
)


@hsettings(max_examples=60, deadline=None)
@given(claims=claims_st)
def test_a_token_carries_its_claims_and_its_signature_checks(claims):
    assert verify(subrep.sign(claims)) == claims


@hsettings(max_examples=60, deadline=None)
@given(claims=claims_st, where=st.integers(min_value=0))
def test_a_changed_token_fails_the_check(claims, where):
    payload, sig = subrep.sign(claims).split(".")
    raw = bytearray(b64d(payload))
    raw[where % len(raw)] ^= 1
    changed = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode() + "." + sig
    with pytest.raises(InvalidSignature):
        verify(changed)


def test_the_page_shows_the_public_key_for_the_desktop_build(client):
    assert b64d(state(client)["public_key"]) == PUBLIC_KEY.public_bytes_raw()


def test_the_key_tool_makes_one_key_and_never_replaces_it(tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "subrep_key", Path(__file__).resolve().parent.parent / "tools" / "subrep_key.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    env = tmp_path / ".env"
    env.write_text("SUBPLZ_WEB_OTHER=1", encoding="utf-8")  # no newline at the end
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    monkeypatch.setattr(tool, "ENV", env)

    assert tool.main() == 0
    public = capsys.readouterr().out.strip()
    text = env.read_text(encoding="utf-8")
    assert text.startswith("SUBPLZ_WEB_OTHER=1\nSUBPLZ_WEB_SUBREP_LICENSE_KEY=")
    saved = text.split("SUBPLZ_WEB_SUBREP_LICENSE_KEY=")[1].strip()
    monkeypatch.setattr(settings, "subrep_license_key", saved)
    assert subrep.public_key() == public  # the printed key belongs to the saved key

    assert tool.main() == 0
    assert capsys.readouterr().out.strip() == public
    assert env.read_text(encoding="utf-8") == text


def test_the_page_gives_a_buyer_the_windows_installer(client):
    # A buyer lands on the Pro part of the page after paying, and needs the
    # app there, with the setup key.
    page = client.get("/subrep.html").text
    pro = page[page.index('id="pro"'):page.index('id="ios"')]
    assert 'href="https://honjimaku.com/subrep/SubrepSetup.exe"' in pro
    assert 'id="link-key"' in pro
