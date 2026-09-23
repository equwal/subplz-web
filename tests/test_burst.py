"""The burst controller (tools/burst): which workers it starts, where, and when it stops."""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from rq import Queue, Worker

from backend.db import Account, Purchase, SessionLocal
from tools.burst import __main__ as run_mod
from tools.burst import offers as offers_mod
from tools.burst.core import (
    NAME_PREFIX, Config, Memory, Offer, decide, machine_cost, spend_30d, spend_cap_usd,
)
from tools.burst.revenue import net_pack_cents, net_subscription_cents, revenue_30d_usd

REF = "new"
NOW = 1_000_000.0
MIN = 60.0
AWS = Offer("aws", "c6a.xlarge", "us-west-2a", 4, 0.07)
HETZNER = Offer("hetzner", "cx33", "fsn1", 4, 0.0164, per_started_hour=True)


def worker(created_ago: float, ref: str = REF, offer: Offer = AWS) -> dict:
    return {"created": NOW - created_ago, "ref": ref, "provider": offer.provider,
            "machine": offer.machine, "zone": offer.zone, "usd_per_hour": offer.usd_per_hour,
            "per_started_hour": offer.per_started_hour}


# --- invariants over any state -------------------------------------------------

names = st.sampled_from([f"{NAME_PREFIX}{i}" for i in range(8)])
offer_st = st.builds(
    Offer, provider=st.sampled_from(["aws", "hetzner"]), machine=st.sampled_from(["a", "b"]),
    zone=st.sampled_from(["z1", "z2"]), vcpus=st.sampled_from([2, 4, 8]),
    usd_per_hour=st.floats(0.005, 0.2), per_started_hour=st.booleans(),
    capacity=st.integers(0, 3),
)


@st.composite
def states(draw):
    pool = draw(st.sets(names, max_size=8))
    workers = {n: worker(draw(st.floats(0, 60 * MIN)), draw(st.sampled_from(["old", REF])),
                         draw(st.sampled_from([AWS, HETZNER]))) for n in pool}
    live = {n: draw(st.sampled_from(["busy", "idle"])) for n in pool if draw(st.booleans())}
    registered = {n for n in pool if n in live or draw(st.booleans())}
    draining = {n for n in registered if draw(st.booleans())}
    idle_since = {n: NOW - draw(st.floats(0, 30 * MIN)) for n in live if live[n] == "idle"}
    memory = Memory(workers=workers, registered=registered, idle_since=idle_since,
                    draining=draining, cooldown=draw(st.sampled_from([{}, {"aws": NOW + 60}])))
    offers = draw(st.lists(offer_st, max_size=4, unique=True))
    cap = draw(st.sampled_from([math.inf, 0.0, 0.5, 5.0]))
    return (draw(st.integers(0, 12)), live, memory, Config(max_workers=draw(st.integers(0, 6))),
            offers, cap)


@settings(max_examples=500)
@given(states())
def test_invariants(state):
    waiting, live, memory, config, offers, cap = state
    d, after = decide(NOW, waiting, live, memory, config, REF, offers, cap)
    added = [n for n, _ in d.add]

    # A worker that rq knows is never destroyed: it must stop by itself first.
    assert not d.remove & set(live)
    assert set(after.workers) == (set(memory.workers) - d.remove) | set(added)
    assert all(n.startswith(NAME_PREFIX) and n not in memory.workers and n not in live for n in added)
    # The cap on workers holds, and no more new workers than waiting jobs.
    assert len([n for n in after.workers if n not in after.draining]) <= config.max_workers
    assert len(added) <= waiting
    # Only a worker that rq knows gets the shutdown command.
    assert d.drain <= set(live)
    # New workers come from offers that have room, from a cloud that is not resting.
    for offer in {o for _, o in d.add}:
        assert offer in offers and memory.cooldown.get(offer.provider, 0) <= NOW
        assert sum(1 for _, o in d.add if o == offer) <= offer.capacity
    # Each machine that ends goes to the ledger, once.
    assert sorted(e["name"] for e in after.ledger if e["end"] == NOW) == sorted(d.remove)


@settings(max_examples=500)
@given(states())
def test_a_new_worker_goes_to_the_cheapest_offer_that_fits(state):
    waiting, live, memory, config, offers, cap = state
    d, _ = decide(NOW, waiting, live, memory, config, REF, offers, cap)
    if not d.add:
        return
    first = d.add[0][1]
    fitting = [o for o in offers if o.capacity > 0 and memory.cooldown.get(o.provider, 0) <= NOW]
    cheaper = [o for o in fitting if o.usd_per_book(config) < first.usd_per_book(config)]
    # A cheaper offer is left out only when the spend cap has no room for its
    # reserve, which is its hourly price: so it must cost more by the hour.
    # (A counterexample of 2026-09-22: 8 vCPU at $0.30/h is cheaper by the book
    # than 2 vCPU at $0.125/h, and a small cap takes only the second.)
    for o in cheaper:
        assert o.usd_per_hour > first.usd_per_hour
    if cap == math.inf:
        assert not cheaper


@settings(max_examples=300)
@given(states())
def test_the_spend_cap_holds(state):
    waiting, live, memory, config, offers, cap = state
    d, after = decide(NOW, waiting, live, memory, config, REF, offers, cap)
    if d.add:
        reserve = sum(config.reserve_hours * w["usd_per_hour"] for n, w in after.workers.items()
                      if n not in after.draining)
        assert spend_30d(NOW, after) + reserve <= cap + 1e-9


# --- scenarios -------------------------------------------------------------------

def test_a_backlog_adds_workers_on_the_cheapest_cloud_up_to_the_cap():
    d, after = decide(NOW, 5, {}, Memory(), Config(max_workers=3), REF, [AWS, HETZNER])
    assert [o for _, o in d.add] == [HETZNER, HETZNER, HETZNER]
    assert all(after.workers[n]["provider"] == "hetzner" for n, _ in d.add)


def test_when_the_cheapest_cloud_is_full_the_next_one_gets_the_worker():
    small = Offer("hetzner", "cx33", "fsn1", 4, 0.0164, per_started_hour=True, capacity=1)
    d, _ = decide(NOW, 3, {}, Memory(), Config(max_workers=3), REF, [AWS, small])
    assert [o.provider for _, o in d.add] == ["hetzner", "aws", "aws"]


def test_a_resting_cloud_gets_no_worker():
    memory = Memory(cooldown={"hetzner": NOW + 10 * MIN})
    d, _ = decide(NOW, 1, {}, memory, Config(max_workers=3), REF, [AWS, HETZNER])
    assert [o.provider for _, o in d.add] == ["aws"]


def test_a_small_cap_takes_the_offer_with_the_lower_hourly_price():
    # Found by the property test: the big machine is cheaper by the book, but
    # the cap has room only for the reserve of the small one.
    small = Offer("aws", "small", "z1", 2, 0.125)  # $0.52 a book, $0.31 reserve
    big = Offer("hetzner", "big", "z1", 8, 0.35)  # $0.40 a book, $0.88 reserve
    d, _ = decide(NOW, 1, {}, Memory(), Config(max_workers=1), REF, [small, big], cap_usd=0.5)
    assert [o for _, o in d.add] == [small]
    d, _ = decide(NOW, 1, {}, Memory(), Config(max_workers=1), REF, [small, big])
    assert [o for _, o in d.add] == [big]


def test_egress_can_make_the_cheaper_machine_the_dearer_book():
    aws_with_egress = Offer("aws", "c6a.xlarge", "us-west-2a", 4, 0.03, extra_usd_per_book=0.2)
    d, _ = decide(NOW, 1, {}, Memory(), Config(max_workers=1), REF, [aws_with_egress, HETZNER])
    assert d.add[0][1] == HETZNER


def test_the_spend_cap_stops_new_workers_but_not_running_ones():
    memory = Memory(workers={"subplz-burst-a": worker(60 * MIN)}, registered={"subplz-burst-a"},
                    ledger=[{"name": "old", "provider": "aws", "start": NOW - 50 * 3600,
                             "end": NOW - 10 * 3600, "usd_per_hour": 0.07, "per_started_hour": False}])
    # 40 h at $0.07 = $2.80 spent, plus 1 h of the running worker, and its reserve.
    d, after = decide(NOW, 3, {"subplz-burst-a": "busy"}, memory, Config(max_workers=3), REF,
                      [AWS], cap_usd=3.2)
    assert d.add == () and not d.drain and "subplz-burst-a" in after.workers
    d, _ = decide(NOW, 3, {"subplz-burst-a": "busy"}, memory, Config(max_workers=3), REF,
                  [AWS], cap_usd=10.0)
    assert len(d.add) == 2


def test_an_hourly_machine_stops_late_in_its_paid_hour():
    name = "subplz-burst-h"
    for age, stops in ((70 * MIN, False), (115 * MIN, True)):
        memory = Memory(workers={name: worker(age, offer=HETZNER)}, registered={name},
                        idle_since={name: NOW - 20 * MIN})
        d, _ = decide(NOW, 0, {name: "idle"}, memory, Config(max_workers=3), REF)
        assert (name in d.drain) is stops, age


def test_an_idle_worker_stops_after_the_grace_time_then_its_machine_goes():
    name = "subplz-burst-a"
    memory = Memory(workers={name: worker(60 * MIN)}, registered={name},
                    idle_since={name: NOW - 11 * MIN})
    d, after = decide(NOW, 0, {name: "idle"}, memory, Config(max_workers=3), REF)
    assert d.drain == {name} and not d.remove
    d, after = decide(NOW + MIN, 0, {}, after, Config(max_workers=3), REF)
    assert d.remove == {name} and after.workers == {}
    assert after.ledger[-1]["name"] == name and after.ledger[-1]["end"] == NOW + MIN


def test_a_machine_that_never_starts_its_worker_is_destroyed_and_replaced():
    memory = Memory(workers={"subplz-burst-a": worker(21 * MIN)})
    d, _ = decide(NOW, 1, {}, memory, Config(max_workers=1), REF, [AWS])
    assert d.remove == {"subplz-burst-a"} and len(d.add) == 1


def test_after_a_deploy_the_workers_of_the_old_commit_are_replaced():
    memory = Memory(
        workers={"subplz-burst-idle": worker(60 * MIN, "old"), "subplz-burst-busy": worker(60 * MIN, "old"),
                 "subplz-burst-boot": worker(2 * MIN, "old")},
        registered={"subplz-burst-idle", "subplz-burst-busy"},
    )
    live = {"subplz-burst-idle": "idle", "subplz-burst-busy": "busy"}
    d, after = decide(NOW, 2, live, memory, Config(max_workers=3), REF, [AWS])
    assert d.drain == {"subplz-burst-idle", "subplz-burst-busy"}
    assert d.remove == {"subplz-burst-boot"}
    assert len(d.add) == 2 and all(after.workers[n]["ref"] == REF for n, _ in d.add)


def test_a_cap_of_zero_stops_every_worker_and_starts_none():
    memory = Memory(
        workers={"subplz-burst-boot": worker(MIN), "subplz-burst-idle": worker(60 * MIN),
                 "subplz-burst-busy": worker(60 * MIN)},
        registered={"subplz-burst-idle", "subplz-burst-busy"},
    )
    live = {"subplz-burst-idle": "idle", "subplz-burst-busy": "busy"}
    d, _ = decide(NOW, 4, live, memory, Config(max_workers=0), REF, [AWS])
    assert d.remove == {"subplz-burst-boot"}
    assert d.drain == {"subplz-burst-idle", "subplz-burst-busy"} and d.add == ()


def test_machine_cost_and_spend():
    assert machine_cost(0, 5400, 0.1, False) == pytest.approx(0.15)
    assert machine_cost(0, 5400, 0.1, True) == pytest.approx(0.2)  # 2 started hours
    memory = Memory(workers={"w": worker(3600)},
                    ledger=[{"name": "x", "provider": "aws", "start": 0, "end": NOW - 31 * 86400,
                             "usd_per_hour": 9.0, "per_started_hour": False}])
    assert spend_30d(NOW, memory) == pytest.approx(0.07)  # the old machine is out of the window


def test_spend_cap():
    assert spend_cap_usd(0.0) == 5.0  # the floor serves the first buyer
    assert spend_cap_usd(115.85) == pytest.approx(34.755)
    assert spend_cap_usd(1000.0) == 100.0


# --- live offers --------------------------------------------------------------------

def test_aws_offers_take_the_newest_price_and_skip_a_price_above_the_maximum():
    items = [
        {"InstanceType": "c6a.xlarge", "AvailabilityZone": "us-west-2a", "SpotPrice": "0.090",
         "Timestamp": "2026-09-22T10:00:00Z"},
        {"InstanceType": "c6a.xlarge", "AvailabilityZone": "us-west-2a", "SpotPrice": "0.050",
         "Timestamp": "2026-09-22T11:00:00Z"},
        {"InstanceType": "c7a.xlarge", "AvailabilityZone": "us-west-2b", "SpotPrice": "0.080",
         "Timestamp": "2026-09-22T11:00:00Z"},
        {"InstanceType": "x9.huge", "AvailabilityZone": "us-west-2b", "SpotPrice": "0.001",
         "Timestamp": "2026-09-22T11:00:00Z"},
    ]
    found = offers_mod.aws_offers(items, max_price=0.0625, extra_usd_per_hour=0.0072,
                                  extra_usd_per_book=0.0, capacity=1)
    assert found == [Offer("aws", "c6a.xlarge", "us-west-2a", 4, pytest.approx(0.0572), False, 0.0, 1)]


def test_hetzner_offers_come_from_stock_at_the_net_price():
    api = {
        "/server_types?per_page=50": {"server_types": [
            {"id": 1, "name": "cx33", "cores": 4, "architecture": "x86", "deprecation": None,
             "prices": [{"location": "fsn1", "price_hourly": {"net": "0.0136", "gross": "0.0162"}},
                        {"location": "nbg1", "price_hourly": {"net": "0.0136", "gross": "0.0162"}}]},
            {"id": 2, "name": "cax21", "cores": 4, "architecture": "arm", "deprecation": None,
             "prices": [{"location": "fsn1", "price_hourly": {"net": "0.0100", "gross": "0.0119"}}]},
            {"id": 3, "name": "cpx31", "cores": 4, "architecture": "x86", "deprecation": None,
             "prices": [{"location": "hil", "price_hourly": {"net": "0.0997", "gross": "0.0997"}}]},
        ]},
        "/datacenters?per_page=50": {"datacenters": [
            {"location": {"name": "fsn1"}, "server_types": {"available": [1, 2]}},
            {"location": {"name": "nbg1"}, "server_types": {"available": []}},  # sold out
            {"location": {"name": "hil"}, "server_types": {"available": [3]}},
        ]},
    }
    found = offers_mod.hetzner_offers(api.__getitem__, machines={"cx33", "cax21", "cpx31"},
                                      usd_per_eur=1.15, ipv4_eur_per_hour=0.0007, capacity=2)
    assert [(o.machine, o.zone) for o in found] == [("cpx31", "hil"), ("cx33", "fsn1")]
    assert found[1].usd_per_hour == pytest.approx((0.0136 + 0.0007) * 1.15)
    assert all(o.per_started_hour and o.capacity == 2 for o in found)


# --- revenue -------------------------------------------------------------------------

def test_net_after_stripe():
    assert [net_pack_cents(a) for a in (499, 3999, 17499)] == [454, 3853, 16961]
    assert [net_subscription_cents(a) for a in (499, 999, 5000)] == [451, 933, 4790]


def test_revenue_counts_recent_packs_and_active_subscriptions():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    tag = str(time.time_ns())
    with SessionLocal() as s:
        base = revenue_30d_usd(s, now)
        s.add(Account(id=f"acct_a{tag}", device_token=f"a{tag}"))
        s.add(Account(id=f"acct_b{tag}", device_token=f"b{tag}", subscription_plan_id="month30",
                      subscription_status="active", subscription_period_end=now + timedelta(days=9)))
        s.add(Account(id=f"acct_c{tag}", device_token=f"c{tag}", subscription_plan_id="unlimited",
                      subscription_status="canceled", subscription_period_end=now + timedelta(days=9)))
        s.add(Purchase(account_id=f"acct_a{tag}", plan_id="pack100", credits=100, amount_cents=3999,
                       stripe_session_id=f"cs_new{tag}", created_at=now - timedelta(days=3)))
        s.add(Purchase(account_id=f"acct_a{tag}", plan_id="pack10", credits=10, amount_cents=499,
                       stripe_session_id=f"cs_old{tag}", created_at=now - timedelta(days=40)))
        s.commit()
        assert revenue_30d_usd(s, now) - base == pytest.approx((3853 + 933) / 100)


# --- one run, with fake clouds and a fake Terraform ------------------------------------

def register(conn, rq_name: str, host: str, state: str) -> None:
    w = Worker([Queue(run_mod.QUEUE_NAME, connection=conn)], connection=conn, name=rq_name)
    w.hostname = host
    w.register_birth()
    w.set_state(state)


def test_the_controller_counts_the_paid_queue_of_the_app():
    from backend.queue import PAID_QUEUE
    assert run_mod.QUEUE_NAME == PAID_QUEUE


def test_read_queue_reports_burst_workers_only():
    conn = fakeredis.FakeRedis()
    register(conn, "rq-1", "subplz-burst-1-0", "busy")
    register(conn, "rq-web", "racknerd-655a1e9", "busy")  # the web server's own worker
    Queue(run_mod.QUEUE_NAME, connection=conn).enqueue("os.getcwd")
    waiting, live, rq_names = run_mod.read_queue(conn)
    assert (waiting, live, rq_names) == (1, {"subplz-burst-1-0": "busy"}, {"subplz-burst-1-0": "rq-1"})


def test_terraform_gets_the_burst_keys_not_the_storage_keys(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "r2-key")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://r2.example")
    monkeypatch.setenv("SUBPLZ_WEB_DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("SUBPLZ_BURST_AWS_ACCESS_KEY_ID", "ec2-key")
    monkeypatch.setenv("HCLOUD_TOKEN", "hz")
    aws = run_mod.terraform_env("aws")
    assert aws["AWS_ACCESS_KEY_ID"] == "ec2-key" and "AWS_ENDPOINT_URL_S3" not in aws
    assert "SUBPLZ_WEB_DATABASE_URL" not in aws
    hetzner = run_mod.terraform_env("hetzner")
    assert hetzner["HCLOUD_TOKEN"] == "hz" and "AWS_ACCESS_KEY_ID" not in hetzner


@pytest.fixture
def world(tmp_path, monkeypatch):
    conn = fakeredis.FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", lambda url: conn)
    released = []
    monkeypatch.setattr(run_mod, "ensure_release", lambda app_dir, ref, bucket: released.append(ref))
    monkeypatch.setattr(run_mod, "revenue_30d", lambda: 0.0)  # the floor: $5
    offers = [AWS, HETZNER]
    monkeypatch.setattr(run_mod, "gather_offers", lambda memory: list(offers))
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake_terraform.py"
    fake.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(calls)!r}).open('a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"bad = pathlib.Path({str(tmp_path / 'fail')!r}).read_text()\n"
        "sys.exit(1 if bad and bad in ' '.join(sys.argv) else 0)\n"
    )
    (tmp_path / "fail").write_text("")

    def run(extra=()):
        argv = ["--redis-url", "redis://x", "--bucket", "b", "--max-workers", "2",
                "--app-dir", str(Path(__file__).resolve().parents[1]),
                "--modules-dir", str(tmp_path / "modules"), "--state-dir", str(tmp_path / "state"),
                "--terraform", sys.executable, str(fake), *extra]
        return run_mod.main(argv)

    return conn, run, tmp_path, calls, released


def enqueue(conn, n: int) -> None:
    for _ in range(n):
        Queue(run_mod.QUEUE_NAME, connection=conn).enqueue("os.getcwd")


def test_a_run_starts_workers_on_the_cheapest_cloud(world):
    conn, run, tmp, calls, released = world
    enqueue(conn, 3)
    assert run() == 0
    hetzner = json.loads((tmp / "state" / "workers-hetzner.tfvars.json").read_text())["workers"]
    assert len(hetzner) == 2 and all(w["machine"] == "cx33" for w in hetzner.values())
    assert "modules" in calls.read_text() and "hetzner" in calls.read_text()
    assert not (tmp / "state" / "workers-aws.tfvars.json").exists()  # nothing changed there
    assert len(released) == 1


def test_a_failed_apply_rests_the_cloud_and_the_next_run_uses_another(world):
    conn, run, tmp, calls, released = world
    enqueue(conn, 1)
    (tmp / "fail").write_text("hetzner")
    assert run() == 1
    state = json.loads((tmp / "state" / "controller.json").read_text())
    assert state["workers"] == {} and state["cooldown"]["hetzner"] > time.time()
    assert json.loads((tmp / "state" / "workers-hetzner.tfvars.json").read_text()) == {"workers": {}}
    assert run() == 0  # Hetzner rests: AWS gets the worker
    aws = json.loads((tmp / "state" / "workers-aws.tfvars.json").read_text())["workers"]
    assert [w["machine"] for w in aws.values()] == ["c6a.xlarge"]


def test_a_dry_run_changes_nothing(world):
    conn, run, tmp, calls, released = world
    enqueue(conn, 1)
    assert run(["--dry-run"]) == 0
    assert not calls.exists() and not (tmp / "state").exists() and not released
