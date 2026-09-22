"""The burst controller: when it adds, stops and destroys burst workers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import fakeredis
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from rq import Queue, Worker

from tools import burst_controller as bc
from tools.burst_controller import Config, Memory, decide

REF = "new"
NOW = 1_000_000.0
MIN = 60.0


def worker(created_ago: float, ref: str = REF) -> dict:
    return {"created": NOW - created_ago, "ref": ref}


# --- invariants over any state -------------------------------------------------

names = st.sampled_from([f"{bc.NAME_PREFIX}{i}" for i in range(8)])


@st.composite
def states(draw):
    pool = draw(st.sets(names, max_size=8))
    workers = {
        n: {"created": NOW - draw(st.floats(0, 60 * MIN)), "ref": draw(st.sampled_from(["old", REF]))}
        for n in pool
    }
    live = {n: draw(st.sampled_from(["busy", "idle"])) for n in pool if draw(st.booleans())}
    registered = {n for n in pool if n in live or draw(st.booleans())}
    draining = {n for n in registered if draw(st.booleans())}
    idle_since = {n: NOW - draw(st.floats(0, 30 * MIN)) for n in live if live[n] == "idle"}
    memory = Memory(workers=workers, registered=registered, idle_since=idle_since, draining=draining)
    return draw(st.integers(0, 12)), live, memory, Config(max_workers=draw(st.integers(0, 6)))


@settings(max_examples=500)
@given(states())
def test_invariants(state):
    waiting, live, memory, config = state
    d, after = decide(NOW, waiting, live, memory, config, REF)

    # A worker that rq knows is never destroyed: it must stop by itself first.
    assert not d.remove & set(live)
    # Every old worker is kept or destroyed, never both; new ones are new.
    assert set(after.workers) == (set(memory.workers) - d.remove) | set(d.add)
    assert not d.remove & set(after.workers)
    assert all(n.startswith(bc.NAME_PREFIX) and n not in memory.workers and n not in live
               for n in d.add)
    # Never more workers at work than the cap, and never more new ones than waiting jobs.
    assert len([n for n in after.workers if n not in after.draining]) <= config.max_workers
    assert len(d.add) <= waiting
    # Only a worker that rq knows can get the shutdown command.
    assert d.drain <= set(live)


@settings(max_examples=500)
@given(states())
def test_a_fresh_busy_worker_under_the_cap_is_never_stopped(state):
    waiting, live, memory, config = state
    d, _ = decide(NOW, waiting, live, memory, config, REF)
    at_work = [n for n in memory.workers if n not in memory.draining
               and (n in live or n not in memory.registered)]
    if len(at_work) <= config.max_workers:
        for n, s in live.items():
            if s == "busy" and n in memory.workers and memory.workers[n]["ref"] == REF:
                assert n not in d.drain


@settings(max_examples=500)
@given(states())
def test_an_idle_worker_is_stopped_for_idleness_only_when_no_job_waits(state):
    waiting, live, memory, config = state
    d, _ = decide(NOW, waiting, live, memory, config, REF)
    at_work = [n for n in memory.workers if n not in memory.draining
               and (n in live or n not in memory.registered)]
    if waiting > 0 and len(at_work) <= config.max_workers:
        for n in d.drain:
            assert live[n] == "busy" or memory.workers[n]["ref"] != REF


# --- scenarios -------------------------------------------------------------------

def test_a_backlog_adds_workers_up_to_the_cap():
    d, after = decide(NOW, 5, {}, Memory(), Config(max_workers=3), REF)
    assert len(d.add) == 3 and not d.remove and not d.drain
    assert all(after.workers[n] == {"created": NOW, "ref": REF} for n in d.add)


def test_booting_and_idle_workers_count_as_capacity():
    memory = Memory(workers={"subplz-burst-a": worker(2 * MIN), "subplz-burst-b": worker(15 * MIN)},
                    registered={"subplz-burst-b"})
    d, _ = decide(NOW, 2, {"subplz-burst-b": "idle"}, memory, Config(max_workers=5), REF)
    assert d.add == () and not d.drain and not d.remove
    d, _ = decide(NOW, 3, {"subplz-burst-b": "idle"}, memory, Config(max_workers=5), REF)
    assert len(d.add) == 1


def test_an_idle_worker_stops_after_the_grace_time_then_its_droplet_goes():
    name = "subplz-burst-a"
    memory = Memory(workers={name: worker(60 * MIN)}, registered={name},
                    idle_since={name: NOW - 11 * MIN})
    d, after = decide(NOW, 0, {name: "idle"}, memory, Config(max_workers=3), REF)
    assert d.drain == {name} and not d.remove
    # It stopped: rq no longer lists it, so the droplet is destroyed.
    d, after = decide(NOW + MIN, 0, {}, after, Config(max_workers=3), REF)
    assert d.remove == {name} and after.workers == {}


def test_an_idle_worker_waits_for_new_jobs_during_the_grace_time():
    name = "subplz-burst-a"
    memory = Memory(workers={name: worker(60 * MIN)}, registered={name},
                    idle_since={name: NOW - 5 * MIN})
    d, _ = decide(NOW, 0, {name: "idle"}, memory, Config(max_workers=3), REF)
    assert not d.drain and not d.remove


def test_a_droplet_that_never_starts_its_worker_is_destroyed():
    memory = Memory(workers={"subplz-burst-a": worker(21 * MIN)})
    d, _ = decide(NOW, 1, {}, memory, Config(max_workers=1), REF)
    assert d.remove == {"subplz-burst-a"}
    assert len(d.add) == 1  # and a new one takes its place


def test_after_a_deploy_the_workers_of_the_old_commit_are_replaced():
    memory = Memory(
        workers={"subplz-burst-idle": worker(60 * MIN, "old"),
                 "subplz-burst-busy": worker(60 * MIN, "old"),
                 "subplz-burst-boot": worker(2 * MIN, "old")},
        registered={"subplz-burst-idle", "subplz-burst-busy"},
    )
    live = {"subplz-burst-idle": "idle", "subplz-burst-busy": "busy"}
    d, after = decide(NOW, 2, live, memory, Config(max_workers=3), REF)
    assert d.drain == {"subplz-burst-idle", "subplz-burst-busy"}  # the busy one finishes first
    assert d.remove == {"subplz-burst-boot"}
    assert len(d.add) == 2 and all(after.workers[n]["ref"] == REF for n in d.add)


def test_a_cap_of_zero_stops_every_worker_and_starts_none():
    memory = Memory(
        workers={"subplz-burst-boot": worker(MIN), "subplz-burst-idle": worker(60 * MIN),
                 "subplz-burst-busy": worker(60 * MIN)},
        registered={"subplz-burst-idle", "subplz-burst-busy"},
    )
    live = {"subplz-burst-idle": "idle", "subplz-burst-busy": "busy"}
    d, _ = decide(NOW, 4, live, memory, Config(max_workers=0), REF)
    assert d.remove == {"subplz-burst-boot"}
    assert d.drain == {"subplz-burst-idle", "subplz-burst-busy"}
    assert d.add == ()


# --- Redis side -----------------------------------------------------------------

def test_the_controller_counts_the_paid_queue_of_the_app():
    from backend.queue import PAID_QUEUE
    assert bc.QUEUE_NAME == PAID_QUEUE


def register(conn, rq_name: str, host: str, state: str) -> None:
    w = Worker([Queue(bc.QUEUE_NAME, connection=conn)], connection=conn, name=rq_name)
    w.hostname = host
    w.register_birth()
    w.set_state(state)


def test_read_queue_reports_burst_workers_only():
    conn = fakeredis.FakeRedis()
    register(conn, "rq-1", "subplz-burst-1-0", "busy")
    register(conn, "rq-2", "subplz-burst-1-1", "idle")
    register(conn, "rq-web", "racknerd-655a1e9", "busy")  # the web server's own worker
    Queue(bc.QUEUE_NAME, connection=conn).enqueue("os.getcwd")
    waiting, live, rq_names = bc.read_queue(conn)
    assert waiting == 1
    assert live == {"subplz-burst-1-0": "busy", "subplz-burst-1-1": "idle"}
    assert rq_names == {"subplz-burst-1-0": "rq-1", "subplz-burst-1-1": "rq-2"}


def test_send_drain_sends_the_rq_shutdown_command(monkeypatch):
    sent = []
    monkeypatch.setattr("rq.command.send_shutdown_command", lambda conn, name: sent.append(name))
    bc.send_drain(None, {"subplz-burst-a": "rq-a", "subplz-burst-b": "rq-b"},
                  frozenset({"subplz-burst-a"}))
    assert sent == ["rq-a"]


# --- one run, with a fake Terraform -------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    conn = fakeredis.FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", lambda url: conn)
    released = []
    monkeypatch.setattr(bc, "ensure_release", lambda app_dir, ref, bucket: released.append(ref))
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "fake_terraform.py"
    fake.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(calls)!r}).open('a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.exit(int(pathlib.Path({str(tmp_path / 'exit_code')!r}).read_text() or 0))\n"
    )
    (tmp_path / "exit_code").write_text("0")

    def run(extra=()):
        argv = ["--redis-url", "redis://x", "--bucket", "b", "--max-workers", "2",
                "--app-dir", str(Path(__file__).resolve().parents[1]),
                "--module-dir", str(tmp_path / "workers"),
                "--state", str(tmp_path / "state.json"),
                "--var-file", str(tmp_path / "vars.json"),
                "--terraform", sys.executable, str(fake), *extra]
        return bc.main(argv)

    return conn, run, tmp_path, calls, released


def test_a_run_writes_the_workers_and_applies(world):
    conn, run, tmp, calls, released = world
    for _ in range(3):
        Queue(bc.QUEUE_NAME, connection=conn).enqueue("os.getcwd")
    assert run() == 0
    workers = json.loads((tmp / "vars.json").read_text())["workers"]
    assert len(workers) == 2  # the cap
    assert "apply -auto-approve" in calls.read_text()
    assert len(released) == 1  # the app went to the bucket for the new workers
    state = json.loads((tmp / "state.json").read_text())
    assert set(state["workers"]) == set(workers)


def test_a_failed_apply_keeps_the_old_state(world):
    conn, run, tmp, calls, released = world
    Queue(bc.QUEUE_NAME, connection=conn).enqueue("os.getcwd")
    (tmp / "exit_code").write_text("1")
    assert run() == 1
    assert json.loads((tmp / "vars.json").read_text()) == {"workers": {}}
    assert not (tmp / "state.json").exists()


def test_a_dry_run_changes_nothing(world):
    conn, run, tmp, calls, released = world
    Queue(bc.QUEUE_NAME, connection=conn).enqueue("os.getcwd")
    assert run(["--dry-run"]) == 0
    assert not calls.exists() and not (tmp / "vars.json").exists() and not released
