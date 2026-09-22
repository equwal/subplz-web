"""Burst workers: add them when jobs wait, remove them when they are idle.

The web server runs this once a minute (a systemd timer, see
infra/burst/README.md). One run does this:

1. Read the Redis queue: how many jobs wait, and which burst workers are busy
   or idle.
2. Decide the set of burst workers (`decide`).
3. Send the rq shutdown command to each worker that is no longer needed. rq
   lets a worker finish its job before it stops, so a scale-in never kills a
   paid job.
4. Write the set to the Terraform variables of infra/burst/workers and run
   `terraform apply` when the set changed. Terraform makes the new droplets,
   and destroys each droplet whose worker has stopped.

The host name of a burst worker is its droplet name, and rq records the host
name of each worker. That is how a droplet and its rq worker are matched.
Workers run the app at the git commit of this server (`ref`). After a deploy,
the workers of the old commit stop and new ones take their place.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("subplz.burst")

NAME_PREFIX = "subplz-burst-"
QUEUE_NAME = "subplz-jobs-paid"  # backend.queue.PAID_QUEUE: burst workers take paid jobs only
RELEASE_PREFIX = "releases/"


@dataclass(frozen=True)
class Config:
    max_workers: int
    # A new droplet installs the speech stack before its worker starts.
    boot_timeout_s: float = 20 * 60
    # An idle worker waits this long for a new job before it stops.
    idle_grace_s: float = 10 * 60


@dataclass
class Memory:
    """What the controller keeps between runs, in a JSON file."""

    # name -> {"created": unix time, "ref": git commit of the app}
    workers: dict[str, dict] = field(default_factory=dict)
    # Names that had an rq worker at least once.
    registered: set[str] = field(default_factory=set)
    # name -> unix time from which the worker was idle without a break.
    idle_since: dict[str, float] = field(default_factory=dict)
    # Names that received the rq shutdown command.
    draining: set[str] = field(default_factory=set)

    def to_json(self) -> dict:
        return {
            "workers": self.workers,
            "registered": sorted(self.registered),
            "idle_since": self.idle_since,
            "draining": sorted(self.draining),
        }

    @classmethod
    def from_json(cls, data: dict) -> Memory:
        return cls(
            workers=dict(data.get("workers", {})),
            registered=set(data.get("registered", [])),
            idle_since=dict(data.get("idle_since", {})),
            draining=set(data.get("draining", [])),
        )


@dataclass(frozen=True)
class Decision:
    add: tuple[str, ...]  # new droplets
    drain: frozenset[str]  # send these workers the rq shutdown command
    remove: frozenset[str]  # destroy these droplets

    @property
    def changes_droplets(self) -> bool:
        return bool(self.add or self.remove)


def decide(
    now: float,
    waiting: int,
    live: dict[str, str],
    memory: Memory,
    config: Config,
    ref: str,
) -> tuple[Decision, Memory]:
    """The next set of burst workers. Pure: no I/O.

    `waiting` is the number of queued jobs that no worker has taken. `live`
    maps the host name of each burst worker that rq knows to "busy" or "idle".
    `ref` is the app commit for new workers.
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
        if stale or (waiting == 0 and now - idle_since[name] >= config.idle_grace_s):
            drain.add(name)
        else:
            idle_ready.append(name)

    # Above the cap (the operator lowered it): stop the cheapest workers first.
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

    active = len(booting) + len(idle_ready) + len(busy)
    need = waiting - len(idle_ready) - len(booting)
    room = config.max_workers - active
    count = max(0, min(need, room))
    taken = set(memory.workers) | set(live)
    new_names: list[str] = []
    i = 0
    while len(new_names) < count:
        name = f"{NAME_PREFIX}{int(now)}-{i}"
        i += 1
        if name not in taken:
            new_names.append(name)
    add = tuple(new_names)

    workers = {n: memory.workers[n] for n in keep}
    workers.update({n: {"created": now, "ref": ref} for n in add})
    draining = (draining | drain) & keep
    new_memory = Memory(
        workers=workers,
        registered=registered & keep,
        idle_since={n: t for n, t in idle_since.items() if n in keep and live.get(n) == "idle"},
        draining=draining,
    )
    return Decision(add=add, drain=frozenset(drain), remove=frozenset(remove)), new_memory


# --- I/O ---------------------------------------------------------------------

def read_queue(conn) -> tuple[int, dict[str, str], dict[str, str]]:
    """Queued jobs, the state of each burst worker, and its rq worker name."""
    from rq import Queue, Worker

    waiting = Queue(QUEUE_NAME, connection=conn).count
    live: dict[str, str] = {}
    rq_names: dict[str, str] = {}
    for worker in Worker.all(connection=conn):
        host = worker.hostname or ""
        if not host.startswith(NAME_PREFIX):
            continue  # the web server's own worker, or one this code does not manage
        live[host] = "busy" if worker.get_state() == "busy" else "idle"
        rq_names[host] = worker.name
    return waiting, live, rq_names


def send_drain(conn, rq_names: dict[str, str], names: frozenset[str]) -> None:
    from rq.command import send_shutdown_command

    for name in sorted(names):
        if name in rq_names:
            send_shutdown_command(conn, rq_names[name])
            log.info("told %s to stop after its current job", name)


def load_memory(path: Path) -> Memory:
    if not path.exists():
        return Memory()
    return Memory.from_json(json.loads(path.read_text(encoding="utf-8")))


def save_json(path: Path, data: dict) -> None:
    """Write `data` so that a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, indent=2, sort_keys=True)
    os.replace(tmp, path)


def terraform_vars(memory: Memory) -> dict:
    return {"workers": {n: {"ref": info["ref"]} for n, info in sorted(memory.workers.items())}}


def terraform_apply(terraform: list[str], module_dir: Path, var_file: Path) -> bool:
    cmd = [*terraform, f"-chdir={module_dir}", "apply", "-auto-approve",
           "-input=false", "-no-color", f"-var-file={var_file}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("terraform apply failed (%s):\n%s", result.returncode, result.stderr[-4000:])
        return False
    return True


def ensure_release(app_dir: Path, ref: str, bucket: str) -> None:
    """Put the app at `ref` in the bucket, where new workers download it."""
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3")
    key = f"{RELEASE_PREFIX}{ref}.tar.gz"
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
            raise
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "app.tar.gz"
        subprocess.run(["git", "-C", str(app_dir), "archive", "--format=tar.gz",
                        "-o", str(archive), ref], check=True)
        s3.upload_file(str(archive), bucket, key)
    log.info("uploaded %s", key)


def run(args: argparse.Namespace) -> int:
    from redis import Redis

    config = Config(max_workers=args.max_workers)
    ref = subprocess.run(["git", "-C", str(args.app_dir), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    conn = Redis.from_url(args.redis_url)
    waiting, live, rq_names = read_queue(conn)
    memory = load_memory(args.state)
    decision, new_memory = decide(time.time(), waiting, live, memory, config, ref)
    log.info("waiting=%d live=%s add=%s drain=%s remove=%s", waiting, live,
             list(decision.add), sorted(decision.drain), sorted(decision.remove))
    if args.dry_run:
        return 0

    send_drain(conn, rq_names, decision.drain)
    if decision.changes_droplets:
        if decision.add:
            ensure_release(args.app_dir, ref, args.bucket)
        save_json(args.var_file, terraform_vars(new_memory))
        if not terraform_apply(args.terraform, args.module_dir, args.var_file):
            # Keep the old memory: the next run decides again from the facts.
            save_json(args.var_file, terraform_vars(memory))
            return 1
    save_json(args.state, new_memory.to_json())
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--max-workers", type=int,
                   default=int(os.environ.get("SUBPLZ_BURST_MAX_WORKERS", "3")))
    p.add_argument("--redis-url", default=os.environ.get("SUBPLZ_WEB_REDIS_URL", ""))
    p.add_argument("--bucket", default=os.environ.get("SUBPLZ_WEB_S3_BUCKET", ""))
    p.add_argument("--app-dir", type=Path, default=Path("/opt/subplz-web"))
    p.add_argument("--module-dir", type=Path,
                   default=Path("/opt/subplz-web/infra/burst/workers"))
    p.add_argument("--state", type=Path, default=Path("/var/lib/subplz-burst/controller.json"))
    p.add_argument("--var-file", type=Path, default=Path("/var/lib/subplz-burst/workers.tfvars.json"))
    p.add_argument("--terraform", nargs="+", default=["terraform"])
    p.add_argument("--dry-run", action="store_true", help="decide and log, change nothing")
    args = p.parse_args(argv)
    if not args.redis_url or not args.bucket:
        p.error("set SUBPLZ_WEB_REDIS_URL and SUBPLZ_WEB_S3_BUCKET (or --redis-url, --bucket)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
