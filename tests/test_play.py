"""Google Play purchases of cloud caption hours: the Play build of Subrep.

No test reaches Google. FakeGooglePlay replaces play.get, play.consume and
play.voided, and the tests of the HTTP layer give play._session a fake session.
"""

from __future__ import annotations

import itertools
import json
import logging
import secrets
from urllib.parse import unquote

import pytest
import requests
from google.auth import exceptions as google_errors
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from backend import accounts, captions, main, play
from backend.db import Account, SessionLocal
from backend.settings import settings

from .conftest import account_id

HOUR = 3600
UNKNOWN = "Google Play does not know this purchase."
_orders = itertools.count(1)


class FakeGooglePlay:
    """Google Play as this server sees it: the purchases of the app, by token."""

    def __init__(self):
        self.purchases: dict[str, dict] = {}
        self.consumed: list[str] = []
        self.gets: list[tuple[str, str]] = []
        self.down = False
        self.fail_consume = 0
        # The tokens of the purchases that Google refunded or charged back.
        self.voided_tokens: list[str] = []

    def buy(self, token: str, account: str | None, product: str = "captions20",
            state: int = 0, quantity: int | None = None, test: bool = False) -> dict:
        """Add a purchase, as purchases.products.get returns it.

        `account` None is a purchase without an account id, for example with
        a promo code in the Play Store app.
        """
        purchase = {"kind": "androidpublisher#productPurchase", "productId": product,
                    "purchaseState": state, "consumptionState": 0,
                    "orderId": f"GPA.3300-{next(_orders):04d}"}
        if account is not None:
            purchase["obfuscatedExternalAccountId"] = account
        if quantity is not None:
            purchase["quantity"] = quantity
        if test:
            purchase["purchaseType"] = 0
        self.purchases[token] = purchase
        return purchase

    def get(self, product_id: str, token: str) -> dict:
        self.gets.append((product_id, token))
        if self.down:
            raise play.PlayError(502, play.NO_ANSWER)
        if token not in self.purchases:
            raise play.PlayError(400, UNKNOWN)
        return dict(self.purchases[token])

    def consume(self, product_id: str, token: str) -> None:
        if self.fail_consume:
            self.fail_consume -= 1
            raise play.PlayError(502, play.NO_ANSWER)
        self.consumed.append(token)
        self.purchases[token]["consumptionState"] = 1

    def voided(self) -> list[str]:
        return list(self.voided_tokens)


@pytest.fixture(autouse=True)
def _no_google(monkeypatch):
    """No test may reach Google. A test of the HTTP layer gives its own fake session."""
    def refuse():
        raise AssertionError("a test tried to reach Google Play")

    monkeypatch.setattr(play, "_session", refuse)


@pytest.fixture
def google(monkeypatch, tmp_path):
    """A server with a Play key file, and Google Play faked."""
    fake = FakeGooglePlay()
    monkeypatch.setattr(play, "get", fake.get)
    monkeypatch.setattr(play, "consume", fake.consume)
    monkeypatch.setattr(play, "voided", fake.voided)
    key = tmp_path / "play-key.json"
    key.write_text("{}")  # play.get is fake, so nothing reads the key.
    monkeypatch.setattr(settings, "play_service_account_file", str(key))
    return fake


def new_token() -> str:
    """A purchase token that no other test uses: the test database lives for the whole run."""
    return f"tok.{secrets.token_hex(8)}-_"


def post_play(client, token: str, product: str = "captions20"):
    return client.post("/api/captions/play-purchase",
                       json={"product_id": product, "purchase_token": token})


def left(client) -> int:
    return client.get("/api/captions").json()["seconds_left"]


def ledger(token: str) -> list[play.PlayPurchase]:
    with SessionLocal() as s:
        return s.query(play.PlayPurchase).filter_by(token_hash=play._hash(token)).all()


def test_the_play_build_can_sell_only_when_the_server_can_check_purchases(
        client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "caption_api_key", "dg_test")
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    monkeypatch.setattr(settings, "play_service_account_file", "")
    assert client.get("/api/captions").json()["play_available"] is False

    key = tmp_path / "play-key.json"
    monkeypatch.setattr(settings, "play_service_account_file", str(key))
    assert client.get("/api/captions").json()["play_available"] is False  # no file yet

    key.write_text("{}")
    state = client.get("/api/captions").json()
    # The Play build does not need Stripe, and the web build does not need Google.
    assert state["play_available"] is True
    assert state["available"] is False

    monkeypatch.setattr(settings, "caption_api_key", "")
    assert client.get("/api/captions").json()["play_available"] is False


# --- POST /api/captions/play-purchase -----------------------------------------

def test_no_play_key_is_503_and_google_is_not_asked(client, google, monkeypatch, tmp_path):
    token = new_token()
    google.buy(token, account_id(client))
    monkeypatch.setattr(settings, "play_service_account_file", "")
    r = post_play(client, token)
    assert r.status_code == 503 and r.json()["detail"] == play.NOT_SET_UP
    monkeypatch.setattr(settings, "play_service_account_file", str(tmp_path / "missing.json"))
    assert post_play(client, token).status_code == 503
    assert google.gets == [] and left(client) == 0


def test_an_unknown_pack_is_404(client, google):
    token = new_token()
    google.buy(token, account_id(client))
    assert post_play(client, token, product="pack10").status_code == 404
    assert google.gets == []


def test_a_paid_purchase_gives_its_hours_and_is_consumed(client, google):
    token = new_token()
    purchase = google.buy(token, account_id(client))
    r = post_play(client, token)
    assert r.status_code == 200 and r.json() == {"seconds_left": 20 * HOUR}
    assert google.consumed == [token]
    assert left(client) == 20 * HOUR
    [row] = ledger(token)
    assert (row.account_id, row.product_id, row.quantity, row.seconds, row.order_id) == (
        account_id(client), "captions20", 1, 20 * HOUR, purchase["orderId"])


def test_the_same_purchase_twice_gives_its_hours_once(client, google):
    token = new_token()
    google.buy(token, account_id(client))
    assert post_play(client, token).status_code == 200
    r = post_play(client, token)  # The app sends it again after a lost answer.
    assert r.status_code == 200 and r.json() == {"seconds_left": 20 * HOUR}
    assert google.consumed == [token]
    assert len(ledger(token)) == 1


def test_a_pending_purchase_gives_nothing_until_it_is_paid(client, google):
    token = new_token()
    google.buy(token, account_id(client), state=2)
    r = post_play(client, token)
    assert r.status_code == 409 and r.json()["detail"] == "The purchase is not complete yet."
    assert left(client) == 0 and google.consumed == [] and ledger(token) == []

    google.purchases[token]["purchaseState"] = 0
    assert post_play(client, token).status_code == 200
    assert left(client) == 20 * HOUR and google.consumed == [token]


def test_a_cancelled_purchase_gives_nothing(client, google):
    token = new_token()
    google.buy(token, account_id(client), state=1)
    assert post_play(client, token).status_code == 409
    assert left(client) == 0 and google.consumed == [] and ledger(token) == []


def test_a_purchase_for_another_account_gives_nothing(client, second_client, google):
    token, stranger = new_token(), new_token()
    google.buy(token, account_id(client))
    google.buy(stranger, "acct_0000000000000000")  # No such account on this server.
    for sender, sent in ((second_client, token), (client, stranger)):
        r = post_play(sender, sent)
        assert r.status_code == 403
        assert r.json()["detail"] == "This purchase belongs to another account."
    assert left(client) == 0 and left(second_client) == 0 and google.consumed == []

    assert post_play(client, token).status_code == 200
    assert left(client) == 20 * HOUR and left(second_client) == 0


def test_a_purchase_without_an_account_id_goes_to_the_caller_once(client, second_client, google):
    token = new_token()
    google.buy(token, None)
    assert post_play(client, token).status_code == 200
    r = post_play(second_client, token)
    assert r.status_code == 200 and r.json() == {"seconds_left": 0}
    assert left(client) == 20 * HOUR and left(second_client) == 0
    assert google.consumed == [token]


def test_a_purchase_from_before_a_merge_goes_to_the_merged_account(client, second_client, google):
    token = new_token()
    old, survivor = account_id(client), account_id(second_client)
    google.buy(token, old)
    with SessionLocal() as s:
        accounts.merge(s, s.get(Account, old), s.get(Account, survivor))
        s.commit()
    assert post_play(second_client, token).status_code == 200
    assert left(second_client) == 20 * HOUR


def test_a_failed_consume_keeps_the_hours_and_the_next_call_consumes(client, google):
    token = new_token()
    google.buy(token, account_id(client))
    google.fail_consume = 1
    r = post_play(client, token)
    assert r.status_code == 200 and r.json() == {"seconds_left": 20 * HOUR}
    assert google.consumed == []

    assert post_play(client, token).status_code == 200
    assert google.consumed == [token] and left(client) == 20 * HOUR


def test_no_answer_from_google_gives_nothing(client, google):
    token = new_token()
    google.buy(token, account_id(client))
    google.down = True
    r = post_play(client, token)
    assert r.status_code == 502 and r.json()["detail"] == play.NO_ANSWER
    assert left(client) == 0 and ledger(token) == []


def test_a_token_that_google_does_not_know_is_400(client, google):
    r = post_play(client, new_token())
    assert r.status_code == 400 and r.json()["detail"] == UNKNOWN
    assert left(client) == 0


def test_a_purchase_for_another_pack_is_400(client, google):
    token = new_token()
    google.buy(token, account_id(client), product="captions80")
    r = post_play(client, token, product="captions20")
    assert r.status_code == 400 and r.json()["detail"] == UNKNOWN
    assert left(client) == 0 and google.consumed == [] and ledger(token) == []


def test_the_quantity_multiplies_the_hours(client, google):
    token = new_token()
    google.buy(token, account_id(client), quantity=3)
    assert post_play(client, token).json() == {"seconds_left": 60 * HOUR}


def test_the_log_never_holds_the_token(client, google, caplog):
    caplog.set_level(logging.DEBUG)
    token, lost = new_token(), new_token()
    google.buy(token, account_id(client))
    google.buy(lost, account_id(client))["consumptionState"] = 1
    google.fail_consume = 1
    assert post_play(client, token).status_code == 200  # The hours, and a failed consume.
    assert post_play(client, token).status_code == 200  # The consume.
    assert post_play(client, lost).status_code == 409  # Consumed, but not in the ledger.
    assert "play purchase" in caplog.text and "could not consume" in caplog.text
    assert token not in caplog.text and lost not in caplog.text


def test_a_consumed_purchase_without_a_row_gives_nothing(client, google):
    token = new_token()
    google.buy(token, account_id(client))["consumptionState"] = 1
    r = post_play(client, token)
    assert r.status_code == 409 and r.json()["detail"] == "This purchase was used already."
    assert left(client) == 0 and google.consumed == [] and ledger(token) == []


def test_a_test_purchase_is_marked_in_the_ledger(client, google):
    tested, paid = new_token(), new_token()
    google.buy(tested, account_id(client), test=True)
    google.buy(paid, account_id(client))
    assert post_play(client, tested).status_code == 200
    assert post_play(client, paid).status_code == 200
    assert [row.test for row in ledger(tested)] == [True]
    assert [row.test for row in ledger(paid)] == [False]


def test_a_bad_body_is_422(client, google):
    token = new_token()
    google.buy(token, account_id(client))
    assert post_play(client, "").status_code == 422
    assert post_play(client, "x" * 4097).status_code == 422
    assert google.gets == []


# --- the HTTP layer -----------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class FakeHttp:
    """Stands in for the signed session: records each request and gives the next answer."""

    def __init__(self):
        self.answers: list = []
        self.seen: list[tuple[str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method: str, url: str, timeout: float | None = None):
        assert timeout  # A request without a timeout can hang a worker.
        self.seen.append((method, url))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(play, "_session", lambda: fake)
    return fake


def test_get_asks_google_for_the_pack_and_the_token(http):
    http.answers = [FakeResponse(200, {"purchaseState": 0})]
    assert play.get("captions20", "abc.DEF-_1") == {"purchaseState": 0}
    url = ("https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
           "com.honjimaku.subrep/purchases/products/captions20/tokens/abc.DEF-_1")
    assert http.seen == [("GET", url)]


def test_consume_posts_to_the_consume_address(http):
    http.answers = [FakeResponse(204)]
    play.consume("captions80", "abc")
    [(method, url)] = http.seen
    assert method == "POST"
    assert url.endswith("/com.honjimaku.subrep/purchases/products/captions80/tokens/abc:consume")


@pytest.mark.parametrize("google_status, status", [
    (400, 400), (404, 400), (410, 400), (401, 503), (403, 503), (429, 502), (500, 502), (503, 502),
])
def test_google_errors_become_clear_answers(http, google_status, status):
    http.answers = [FakeResponse(google_status)]
    with pytest.raises(play.PlayError) as caught:
        play.get("captions20", "abc")
    assert caught.value.status == status


@pytest.mark.parametrize("error", [
    requests.ConnectionError("no route to host"),
    requests.Timeout("read timed out"),
    google_errors.TransportError("no route to the token server"),
])
def test_no_connection_to_google_is_502(http, error):
    http.answers = [error]
    with pytest.raises(play.PlayError) as caught:
        play.get("captions20", "abc")
    assert caught.value.status == 502


def test_a_key_that_google_refuses_is_503(http):
    http.answers = [google_errors.RefreshError("invalid_grant: account not found")]
    with pytest.raises(play.PlayError) as caught:
        play.get("captions20", "abc")
    assert (caught.value.status, caught.value.detail) == (503, play.NOT_SET_UP)


def test_the_key_file_gives_credentials_for_the_play_api(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    key = tmp_path / "play-key.json"
    key.write_text(json.dumps({
        "type": "service_account", "project_id": "subrep-test", "private_key_id": "k1",
        "private_key": pem, "client_email": "play@subrep-test.iam.gserviceaccount.com",
        "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token",
    }))
    credentials = play._credentials(str(key))
    assert credentials.scopes == ["https://www.googleapis.com/auth/androidpublisher"]
    assert credentials.service_account_email == "play@subrep-test.iam.gserviceaccount.com"


@pytest.mark.parametrize("content", ["{}", "not json", None])
def test_a_bad_key_file_is_503(tmp_path, content):
    key = tmp_path / "play-key.json"
    if content is not None:
        key.write_text(content)
    with pytest.raises(play.PlayError) as caught:
        play._credentials(str(key))
    assert caught.value.status == 503


def test_the_voided_list_reads_every_page(http):
    http.answers = [
        FakeResponse(200, {"voidedPurchases": [{"purchaseToken": "a", "orderId": "GPA.1"}],
                           "tokenPagination": {"nextPageToken": "p2"}}),
        FakeResponse(200, {"voidedPurchases": [{"purchaseToken": "b", "orderId": "GPA.2"}]}),
        FakeResponse(200, {}),
    ]
    assert play.voided() == ["a", "b"]
    assert play.voided() == []  # No refunds in the last 30 days.
    url = ("https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
           "com.honjimaku.subrep/purchases/voidedpurchases")
    assert http.seen == [("GET", url), ("GET", url + "?token=p2"), ("GET", url)]


# --- refunds ------------------------------------------------------------------
# These tests use SessionLocal and no TestClient: each TestClient starts a pass
# of the housekeeping in the background.

def paid(product: str = "captions20", account: str | None = None) -> dict:
    """A paid purchase that nobody consumed yet, as Google returns it."""
    purchase = {"purchaseState": 0, "consumptionState": 0, "productId": product}
    if account is not None:
        purchase["obfuscatedExternalAccountId"] = account
    return purchase


def test_a_refund_takes_back_the_hours_once():
    token = new_token()
    with SessionLocal() as s:
        me = accounts.new_account(s)
        captions.add(s, me.id, 5 * HOUR)  # Hours from an earlier purchase.
        s.commit()
        assert play.credit(s, me, "captions20", token, paid(account=me.id))
        assert play.take_back(s, [token]) == 1
        assert play.take_back(s, [token]) == 0
        assert captions.seconds_left(s, me.id) == 5 * HOUR
        [row] = s.query(play.PlayPurchase).filter_by(token_hash=play._hash(token)).all()
        assert row.voided_at is not None


def test_two_passes_at_the_same_time_take_the_hours_back_once(monkeypatch):
    token = new_token()
    with SessionLocal() as s, SessionLocal() as other:
        me = accounts.new_account(s)
        captions.add(s, me.id, 5 * HOUR)
        s.commit()
        assert play.credit(s, me, "captions20", token, paid(account=me.id))
        # The other pass runs after this pass found the row, and before it
        # marks the row.
        other_pass = []
        execute = s.execute

        def execute_after_the_other_pass(*args, **kwargs):
            if not other_pass:
                other_pass.append(play.take_back(other, [token]))
            return execute(*args, **kwargs)

        monkeypatch.setattr(s, "execute", execute_after_the_other_pass)
        assert play.take_back(s, [token]) == 0
        assert other_pass == [1]
        me_id = me.id
    with SessionLocal() as s:
        assert captions.seconds_left(s, me_id) == 5 * HOUR


def test_a_refund_after_a_merge_takes_the_hours_from_the_survivor():
    token = new_token()
    with SessionLocal() as s:
        old, survivor = accounts.new_account(s), accounts.new_account(s)
        s.commit()
        assert play.credit(s, old, "captions20", token, paid(account=old.id))
        accounts.merge(s, old, survivor)
        s.commit()
        assert captions.seconds_left(s, survivor.id) == 20 * HOUR
        assert play.take_back(s, [token]) == 1
        assert captions.seconds_left(s, survivor.id) == 0
        assert captions.seconds_left(s, old.id) == 0


def test_housekeeping_takes_back_refunds_only_when_play_is_set_up(google, monkeypatch):
    token = new_token()
    with SessionLocal() as s:
        me = accounts.new_account(s)
        s.commit()
        assert play.credit(s, me, "captions20", token, paid(account=me.id))
        me_id = me.id
    google.voided_tokens = [token]
    asked = []

    def spy():
        asked.append(True)
        return google.voided()

    monkeypatch.setattr(play, "voided", spy)
    key = settings.play_service_account_file
    monkeypatch.setattr(settings, "play_service_account_file", "")
    main.housekeeping()
    assert asked == []

    monkeypatch.setattr(settings, "play_service_account_file", key)
    main.housekeeping()
    assert asked == [True]
    with SessionLocal() as s:
        assert captions.seconds_left(s, me_id) == 0


def test_a_google_failure_does_not_stop_housekeeping(google, monkeypatch, caplog):
    def down():
        raise play.PlayError(502, play.NO_ANSWER)

    monkeypatch.setattr(play, "voided", down)
    main.housekeeping()
    warnings = [record for record in caplog.records
                if record.name == "backend.play" and record.levelno == logging.WARNING]
    assert len(warnings) == 1 and play.NO_ANSWER in warnings[0].getMessage()


# --- properties ---------------------------------------------------------------

@hsettings(max_examples=50, deadline=None)
@given(buys=st.lists(st.tuples(st.sampled_from(sorted(captions.HOURS)), st.integers(1, 3),
                               st.sampled_from(["me", "other", "none"])),
                     min_size=1, max_size=4),
       calls=st.lists(st.tuples(st.integers(0, 3), st.sampled_from([0, 1, 2]),
                                st.sampled_from([0, 1])),
                      max_size=25))
def test_a_token_gives_its_hours_once_and_only_when_paid_for_this_account(buys, calls):
    run = secrets.token_hex(6)  # The database lives across examples.
    with SessionLocal() as s:
        me, other = accounts.new_account(s), accounts.new_account(s)
        s.commit()
        ids = {"me": me.id, "other": other.id, "none": None}
        expected, added = {}, {}
        for i, state, consumed in calls:
            if i >= len(buys):
                continue
            product, quantity, owner = buys[i]
            token = f"{run}.{i}"
            google = {"purchaseState": state, "consumptionState": consumed,
                      "productId": product, "quantity": quantity}
            if ids[owner]:
                google["obfuscatedExternalAccountId"] = ids[owner]
            try:
                if play.credit(s, me, product, token, google):
                    added[token] = added.get(token, 0) + 1
            except play.PlayError:
                pass
            # Hours come only from a call that saw the purchase paid and not consumed.
            if state == 0 and consumed == 0 and owner != "other":
                expected[token] = captions.HOURS[product] * HOUR * quantity
        assert captions.seconds_left(s, ids["me"]) == sum(expected.values())
        assert captions.seconds_left(s, ids["other"]) == 0
        assert added == {token: 1 for token in expected}
        assert s.query(play.PlayPurchase).filter_by(account_id=ids["me"]).count() == len(expected)


@hsettings(max_examples=50, deadline=None)
@given(bought=st.lists(st.sampled_from(sorted(captions.HOURS)), min_size=1, max_size=4),
       spent=st.integers(0, 300 * HOUR),
       voids=st.lists(st.lists(st.integers(0, 5), max_size=6), max_size=4))
def test_a_refund_takes_back_the_hours_once_and_never_below_zero(bought, spent, voids):
    run = secrets.token_hex(6)  # The database lives across examples.
    with SessionLocal() as s:
        me = accounts.new_account(s)
        s.commit()
        for i, product in enumerate(bought):
            assert play.credit(s, me, product, f"{run}.{i}", paid(product, me.id))
        total = sum(captions.HOURS[product] * HOUR for product in bought)
        used = min(spent, total)
        assert captions.spend(s, me.id, used)
        voided: set[int] = set()
        taken = 0
        for indexes in voids:  # An index past the purchases is a token that this server never saw.
            taken += play.take_back(s, [f"{run}.{i}" for i in indexes])
            voided |= {i for i in indexes if i < len(bought)}
        back = sum(captions.HOURS[bought[i]] * HOUR for i in voided)
        assert captions.seconds_left(s, me.id) == max(0, total - used - back)
        assert taken == len(voided)
        assert s.query(play.PlayPurchase).filter(
            play.PlayPurchase.account_id == me.id,
            play.PlayPurchase.voided_at.is_not(None)).count() == len(voided)


@given(st.text(min_size=1))
def test_any_token_is_one_path_segment_of_the_address(token):
    url = play._url("captions20", token)
    assert "?" not in url and "#" not in url
    head, tail = url.split("/tokens/", 1)
    assert head.endswith("/com.honjimaku.subrep/purchases/products/captions20")
    assert "/" not in tail
    assert unquote(tail) == token
