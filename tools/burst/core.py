"""The decisions of the burst controller. Pure: no I/O, no clock, no network.

`decide` takes what rq reports about the queue and the workers, the live
offers of the clouds, and the spend cap. It returns which workers to start (on
the cheapest offer), which to stop, and which machines to destroy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

NAME_PREFIX = "subplz-burst-"
HOUR = 3600.0
WINDOW = 30 * 86400.0  # the spend cap and the revenue both look back 30 days


@dataclass(frozen=True)
class Config:
    max_workers: int
    # A new machine installs the speech stack before its worker starts.
    boot_timeout_s: float = 20 * 60
    # An idle worker waits this long for a new job before it stops.
    idle_grace_s: float = 10 * 60
    # A 10-hour book on 4 vCPU: the web server does it at about 5x real time.
    book_hours_4vcpu: float = 2.0
    boot_hours: float = 0.15
    # The cap keeps this much work of each worker in reserve.
    reserve_hours: float = 2.5
    # A cloud that bills each started hour gets its worker stopped only late in
    # the hour that is paid already.
    hour_drain_after_s: float = 50 * 60


@dataclass(frozen=True)
class Offer:
    """A machine that one cloud can start now, at its live price."""

    provider: str  # "aws" or "hetzner": the Terraform module infra/burst/workers/<provider>
    machine: str  # instance type or server type
    zone: str  # AWS availability zone or Hetzner location
    vcpus: int
    usd_per_hour: float  # all that the machine costs per hour: price, IPv4 address, disk
    per_started_hour: bool = False  # Hetzner bills each started hour in full
    extra_usd_per_book: float = 0.0  # for example the transfer fee of the results
    capacity: int = 1000  # machines of this offer that the account can start now

    def usd_per_book(self, config: Config) -> float:
        hours = config.book_hours_4vcpu * 4 / self.vcpus + config.boot_hours
        return self.usd_per_hour * hours + self.extra_usd_per_book


@dataclass
class Memory:
    """What the controller keeps between runs, in a JSON file."""

    # name -> {"created", "ref", "provider", "machine", "zone", "usd_per_hour", "per_started_hour"}
    workers: dict[str, dict] = field(default_factory=dict)
    # Names that had an rq worker at least once.
    registered: set[str] = field(default_factory=set)
    # name -> time from which the worker was idle without a break.
    idle_since: dict[str, float] = field(default_factory=dict)
    # Names that received the rq shutdown command.
    draining: set[str] = field(default_factory=set)
    # Machines that ended in the last 30 days:
    # {"name", "provider", "start", "end", "usd_per_hour", "per_started_hour"}.
    ledger: list[dict] = field(default_factory=list)
    # provider -> time until which the controller does not use it (an apply failed).
    cooldown: dict[str, float] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "workers": self.workers,
            "registered": sorted(self.registered),
            "idle_since": self.idle_since,
            "draining": sorted(self.draining),
            "ledger": self.ledger,
            "cooldown": self.cooldown,
        }

    @classmethod
    def from_json(cls, data: dict) -> Memory:
        return cls(
            workers=dict(data.get("workers", {})),
            registered=set(data.get("registered", [])),
            idle_since=dict(data.get("idle_since", {})),
            draining=set(data.get("draining", [])),
            ledger=list(data.get("ledger", [])),
            cooldown=dict(data.get("cooldown", {})),
        )


@dataclass(frozen=True)
class Decision:
    add: tuple[tuple[str, Offer], ...]  # new machines: (name, offer)
    drain: frozenset[str]  # send these workers the rq shutdown command
    remove: frozenset[str]  # destroy these machines


def machine_cost(start: float, end: float, usd_per_hour: float, per_started_hour: bool) -> float:
    hours = max(0.0, end - start) / HOUR
    if per_started_hour:
        hours = math.ceil(hours)
    return hours * usd_per_hour


def spend_30d(now: float, memory: Memory) -> float:
    """What the machines of the last 30 days cost: the ended ones and the running ones."""
    total = 0.0
    for entry in memory.ledger:
        if entry["end"] >= now - WINDOW:
            total += machine_cost(entry["start"], entry["end"], entry["usd_per_hour"],
                                  entry["per_started_hour"])
    for info in memory.workers.values():
        total += machine_cost(info["created"], now, info["usd_per_hour"], info["per_started_hour"])
    return total


def spend_cap_usd(revenue_30d_usd: float, k: float = 0.30, floor: float = 5.0,
                  ceiling: float = 100.0) -> float:
    """The burst budget of the last 30 days: a share of the net revenue, with a floor
    so that the first buyer gets a burst worker, and a ceiling."""
    return min(ceiling, max(floor, k * revenue_30d_usd))


def decide(
    now: float,
    waiting: int,
    live: dict[str, str],
    memory: Memory,
    config: Config,
    ref: str,
    offers: list[Offer] = (),
    cap_usd: float = math.inf,
) -> tuple[Decision, Memory]:
    """The next set of burst workers.

    `waiting` is the number of paid jobs that no worker has taken. `live` maps
    the host name of each burst worker that rq knows to "busy" or "idle". `ref`
    is the app commit for new workers. `offers` are the machines that the clouds
    can start now. `cap_usd` is the spend cap of the last 30 days.
    """
    keep: set[str] = set()
    remove: set[str] = set()
    drain: set[str] = set()
    registered = set(memory.registered)
    idle_since = dict(memory.idle_since)
    draining = set(memory.draining)
    booting: list[str] = []
    idle_ready: list[str] = []
    busy: list[str] = []

    for name, info in memory.workers.items():
        state = live.get(name)
        stale = info.get("ref") != ref
        if state is None:
            if name in registered or stale:
                remove.add(name)  # it stopped, or it would run an old commit
            elif now - info["created"] >= config.boot_timeout_s:
                remove.add(name)  # it never started
            else:
                keep.add(name)
                booting.append(name)
            continue

        keep.add(name)
        registered.add(name)
        if state == "busy":
            idle_since.pop(name, None)
            if stale and name not in draining:
                drain.add(name)  # it finishes the job, then stops
            elif name not in draining:
                busy.append(name)
            continue

        idle_since.setdefault(name, now)
        if name in draining:
            continue  # it stops soon
        late_in_paid_hour = (not info.get("per_started_hour")
                             or (now - info["created"]) % HOUR >= config.hour_drain_after_s)
        if stale or (waiting == 0 and now - idle_since[name] >= config.idle_grace_s
                     and late_in_paid_hour):
            drain.add(name)
        else:
            idle_ready.append(name)

    # Above the cap on workers (the operator lowered it): stop the cheapest first.
    active = len(booting) + len(idle_ready) + len(busy)
    excess = active - config.max_workers
    while excess > 0 and booting:
        name = booting.pop()
        keep.discard(name)
        remove.add(name)
        excess -= 1
    for group in (idle_ready, busy):
        while excess > 0 and group:
            drain.add(group.pop())
            excess -= 1

    # The machines that end now go to the ledger of the spend cap.
    ledger = [e for e in memory.ledger if e["end"] >= now - WINDOW]
    for name in remove:
        info = memory.workers[name]
        ledger.append({"name": name, "provider": info["provider"], "start": info["created"],
                       "end": now, "usd_per_hour": info["usd_per_hour"],
                       "per_started_hour": info["per_started_hour"]})

    kept = {n: memory.workers[n] for n in keep}
    budget = cap_usd - spend_30d(now, Memory(workers=kept, ledger=ledger))
    reserved = sum(config.reserve_hours * info["usd_per_hour"]
                   for n, info in kept.items() if n not in draining | drain)

    active = len(booting) + len(idle_ready) + len(busy)
    need = waiting - len(idle_ready) - len(booting)
    room = config.max_workers - active
    usable = sorted(
        (o for o in offers if memory.cooldown.get(o.provider, 0.0) <= now and o.capacity > 0),
        key=lambda o: (o.usd_per_book(config), o.provider, o.machine, o.zone),
    )
    left = {o: o.capacity for o in usable}
    taken = set(memory.workers) | set(live)
    add: list[tuple[str, Offer]] = []
    i = 0
    while len(add) < min(need, room):
        offer = next((o for o in usable if left[o] > 0
                      and reserved + config.reserve_hours * o.usd_per_hour <= budget), None)
        if offer is None:
            break  # no cloud can start one, or the spend cap is reached
        name = f"{NAME_PREFIX}{int(now)}-{i}"
        i += 1
        if name in taken:
            continue
        left[offer] -= 1
        reserved += config.reserve_hours * offer.usd_per_hour
        add.append((name, offer))

    workers = dict(kept)
    for name, offer in add:
        workers[name] = {
            "created": now, "ref": ref, "provider": offer.provider, "machine": offer.machine,
            "zone": offer.zone, "usd_per_hour": offer.usd_per_hour,
            "per_started_hour": offer.per_started_hour,
        }
    new_memory = Memory(
        workers=workers,
        registered=registered & keep,
        idle_since={n: t for n, t in idle_since.items() if n in keep and live.get(n) == "idle"},
        draining=(draining | drain) & keep,
        ledger=ledger,
        cooldown={p: t for p, t in memory.cooldown.items() if t > now},
    )
    return Decision(add=tuple(add), drain=frozenset(drain), remove=frozenset(remove)), new_memory
