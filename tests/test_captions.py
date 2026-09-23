"""Cloud captions for Subrep: hour packs, the balance, and the speech proxy."""

from __future__ import annotations

import io
import wave

import pytest
import stripe
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from backend import accounts, captions
from backend.db import Account, SessionLocal
from backend.settings import settings

from .conftest import FakeStripeObject, account_id, checkout_event, post_webhook

HOUR = 3600
SECOND = captions.BYTES_PER_SECOND


@pytest.fixture
def speech(monkeypatch):
    """A server with a key, and the speech service faked: it answers with a fixed text."""
    monkeypatch.setattr(settings, "caption_api_key", "gsk_test")
    calls = []

    def fake(pcm, language):
        calls.append((len(pcm), language))
        return "こんにちは"

    monkeypatch.setattr(captions, "transcribe", fake)
    return calls


def give(client, seconds):
    with SessionLocal() as s:
        captions.add(s, account_id(client), seconds)
        s.commit()


def left(client):
    return client.get("/api/captions").json()["seconds_left"]


def post_piece(client, n_bytes, lang="ja"):
    return client.post(f"/api/captions/transcribe?lang={lang}", content=b"\0" * n_bytes)


# --- packs -------------------------------------------------------------------

def test_the_book_page_does_not_show_the_caption_packs(client):
    ids = {p["id"] for p in client.get("/api/pricing").json()["plans"]}
    assert not ids & set(captions.HOURS)


def test_the_app_sees_three_packs_and_an_empty_balance(client):
    state = client.get("/api/captions").json()
    assert [(p["id"], p["hours"], p["price_display"]) for p in state["packs"]] == [
        ("captions20", 20, "$4.99"), ("captions80", 80, "$16.99"),
        ("captions200", 200, "$39.00"),
    ]
    assert state["seconds_left"] == 0


def test_checkout_sells_the_pack_to_this_account(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(stripe.checkout.Session, "create",
                        lambda **p: seen.update(p) or FakeStripeObject({"url": "https://stripe.test/pay"}))
    r = client.post("/api/captions/checkout", json={"pack_id": "captions80"})
    assert r.status_code == 200 and r.json()["url"] == "https://stripe.test/pay"
    assert seen["metadata"]["plan_id"] == "captions80"
    assert seen["client_reference_id"] == account_id(client)
    assert seen["line_items"][0]["price_data"]["unit_amount"] == 1699
    assert client.post("/api/captions/checkout", json={"pack_id": "pack10"}).status_code == 404


def test_a_paid_pack_gives_its_hours_once(client):
    acct = account_id(client)
    event = checkout_event("cs_captions", acct, "captions20")
    assert post_webhook(client, event).status_code == 200
    assert post_webhook(client, event).status_code == 200  # Stripe retries
    assert left(client) == 20 * HOUR
    # A caption pack is not a book credit.
    assert client.get("/api/account").json()["credits"] == 0


def test_a_merge_moves_the_hours(client, second_client):
    give(client, 5 * HOUR)
    give(second_client, 2 * HOUR)
    with SessionLocal() as s:
        src = s.get(Account, account_id(client))
        dst = s.get(Account, account_id(second_client))
        accounts.merge(s, src, dst)
        s.commit()
        assert captions.seconds_left(s, dst.id) == 7 * HOUR
        assert captions.seconds_left(s, src.id) == 0


# --- transcribe --------------------------------------------------------------

def test_no_key_on_the_server_is_503(client, monkeypatch):
    monkeypatch.setattr(settings, "caption_api_key", "")
    give(client, HOUR)
    assert post_piece(client, 3 * SECOND).status_code == 503
    assert left(client) == HOUR


def test_no_hours_is_402_and_the_service_is_not_called(client, speech):
    assert post_piece(client, 3 * SECOND).status_code == 402
    assert speech == []


def test_a_piece_costs_its_length_rounded_up(client, speech):
    give(client, 60)
    r = post_piece(client, int(2.5 * SECOND))
    assert r.status_code == 200
    assert r.json() == {"text": "こんにちは", "seconds": 3, "seconds_left": 57}
    assert speech == [(int(2.5 * SECOND), "ja")]


def test_the_last_seconds_do_not_pay_for_a_longer_piece(client, speech):
    give(client, 2)
    assert post_piece(client, 3 * SECOND).status_code == 402
    assert left(client) == 2


def test_a_failure_of_the_service_gives_the_seconds_back(client, monkeypatch):
    monkeypatch.setattr(settings, "caption_api_key", "gsk_test")

    def down(pcm, language):
        raise captions.TranscribeError("speech service answered 500")

    monkeypatch.setattr(captions, "transcribe", down)
    give(client, 60)
    assert post_piece(client, 4 * SECOND).status_code == 502
    assert left(client) == 60


@pytest.mark.parametrize("n_bytes, status", [(0, 400), (3, 400), (31 * SECOND, 413)])
def test_a_bad_piece_is_refused_without_cost(client, speech, n_bytes, status):
    give(client, HOUR)
    assert post_piece(client, n_bytes).status_code == status
    assert left(client) == HOUR and speech == []


# --- properties --------------------------------------------------------------

@hsettings(max_examples=50, deadline=None)
@given(given_seconds=st.integers(0, 100),
       pieces=st.lists(st.integers(1, 40 * SECOND), max_size=12),
       failures=st.lists(st.booleans(), max_size=12))
def test_the_balance_never_goes_below_zero_and_no_second_is_lost(given_seconds, pieces, failures):
    with SessionLocal() as s:
        acct = accounts.new_account(s)
        s.commit()
        captions.add(s, acct.id, given_seconds)
        s.commit()
        for i, n in enumerate(pieces):
            cost = captions.billed_seconds(n)
            if captions.spend(s, acct.id, cost) and i < len(failures) and failures[i]:
                captions.refund(s, acct.id, cost)
            row = s.get(captions.CaptionBalance, acct.id)
            s.refresh(row)
            assert row.seconds_left >= 0
            assert row.seconds_left + row.seconds_used == given_seconds


@given(st.binary(max_size=4 * SECOND).filter(lambda b: len(b) % 2 == 0))
def test_wav_holds_the_pcm_unchanged(pcm):
    with wave.open(io.BytesIO(captions.wav(pcm))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 16_000)
        assert w.readframes(w.getnframes()) == pcm


@given(st.integers(1, 60 * SECOND))
def test_the_billed_seconds_cover_the_piece(n_bytes):
    billed = captions.billed_seconds(n_bytes)
    assert billed * SECOND >= n_bytes > (billed - 1) * SECOND



def test_the_request_is_the_openai_form(monkeypatch):
    """transcribe() against a local server that reads the form as the service does."""
    import json
    import threading
    from email.parser import BytesParser
    from email.policy import HTTP
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class Service(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            head = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
            form = BytesParser(policy=HTTP).parsebytes(head + body)
            for part in form.iter_parts():
                seen[part.get_param("name", header="content-disposition")] = part.get_payload(decode=True)
            seen["auth"] = self.headers["Authorization"]
            out = json.dumps({"text": " 今日は "}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Service)
    threading.Thread(target=server.handle_request, daemon=True).start()
    monkeypatch.setattr(settings, "caption_api_url", f"http://127.0.0.1:{server.server_port}/")
    monkeypatch.setattr(settings, "caption_api_key", "gsk_test")
    pcm = bytes(range(256)) * 10

    assert captions.transcribe(pcm, "ja") == "今日は"
    server.server_close()
    assert seen["auth"] == "Bearer gsk_test"
    assert seen["model"] == b"openai/whisper-large-v3-turbo"
    assert seen["language"] == b"ja"
    assert seen["file"] == captions.wav(pcm)
