"""Burst controller: add workers on the cheapest cloud when paid jobs wait.

The web server runs `python -m tools.burst` once a minute (a systemd timer,
see infra/burst/README.md). One run does this:

1. Read the paid queue in Redis, and the state of each burst worker.
2. Read the live offers of each cloud that has credentials (AWS spot prices,
   Hetzner prices and stock), and the net revenue of the last 30 days.
3. Decide (core.decide): new workers go to the offer with the lowest cost per
   book, inside the spend cap. Idle workers get the rq shutdown command: rq
   lets a worker finish its job first, so a scale-in never kills a paid job.
4. For each cloud whose set of workers changed: write its Terraform variable
   file and run `terraform apply` on infra/burst/workers/<cloud>. If the apply
   fails, that cloud rests for 30 minutes, and the next run uses the next
   cheapest offer.

The host name of a worker is its machine name, and rq records the host name of
each worker. That is how a machine and its rq worker are matched.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import NAME_PREFIX, Config, Memory, Offer, decide, spend_30d, spend_cap_usd

log = logging.getLogger("subplz.burst")

QUEUE_NAME = "subplz-jobs-paid"  # backend.queue.PAID_QUEUE: burst workers take paid jobs only
RELEASE_PREFIX = "releases/"
PROVIDERS = ("aws", "hetzner")
COOLDOWN_S = 30 * 60


# --- Redis ---------------------------------------------------------------------

def read_queue(conn) -> tuple[int, dict[str, str], dict[str, str]]:
    """Waiting paid jobs, the state of each burst worker, and its rq worker name."""
    from rq import Queue, Worker

    waiting = Queue(QUEUE_NAME, connection=conn).count
    live: dict[str, str] = {}
    rq_names: dict[str, str] = {}
    for worker in Worker.all(connection=conn):
        host = worker.hostname or ""
        if not host.startswith(NAME_PREFIX):
            continue  # the web server's own worker, or one that this code does not manage
        live[host] = "busy" if worker.get_state() == "busy" else "idle"
        rq_names[host] = worker.name
    return waiting, live, rq_names


def send_drain(conn, rq_names: dict[str, str], names: frozenset[str]) -> None:
    from rq.command import send_shutdown_command

    for name in sorted(names):
        if name in rq_names:
            send_shutdown_command(conn, rq_names[name])
            log.info("told %s to stop after its current job", name)


# --- files ---------------------------------------------------------------------

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


def provider_vars(memory: Memory, provider: str) -> dict:
    """The Terraform variables of one cloud: its workers."""
    return {"workers": {
        name: {"ref": w["ref"], "machine": w["machine"], "zone": w["zone"]}
        for name, w in sorted(memory.workers.items()) if w["provider"] == provider
    }}


# --- clouds --------------------------------------------------------------------

def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def env_list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


def vcpus_in_use(memory: Memory, provider: str) -> int:
    from .offers import AWS_VCPUS

    return sum(AWS_VCPUS.get(w["machine"], 4) for w in memory.workers.values()
               if w["provider"] == provider)


def gather_offers(memory: Memory) -> list[Offer]:
    """The live offers of each cloud that has credentials. A cloud whose API
    fails gives no offers this run; the others still do."""
    from . import offers

    found: list[Offer] = []
    if os.environ.get("SUBPLZ_BURST_AWS_ACCESS_KEY_ID"):
        try:
            import boto3

            ec2 = boto3.client(
                "ec2", region_name=os.environ.get("SUBPLZ_BURST_AWS_REGION", "us-west-2"),
                aws_access_key_id=os.environ["SUBPLZ_BURST_AWS_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["SUBPLZ_BURST_AWS_SECRET_ACCESS_KEY"],
            )
            machines = env_list("SUBPLZ_BURST_AWS_TYPES", "c6a.xlarge,c7a.xlarge,m6a.xlarge")
            since = datetime.now(timezone.utc) - timedelta(hours=1)
            limit = int(os.environ.get("SUBPLZ_BURST_AWS_VCPU_LIMIT", "5"))
            capacity = max(0, limit - vcpus_in_use(memory, "aws")) // 4
            found += offers.aws_offers(
                offers.fetch_aws_spot_prices(ec2, machines, since),
                max_price=env_float("SUBPLZ_BURST_AWS_MAX_PRICE", 0.0625),
                # A public IPv4 address ($0.005/h) and a 20 GB disk ($0.0022/h).
                extra_usd_per_hour=env_float("SUBPLZ_BURST_AWS_EXTRA_USD_PER_HOUR", 0.0072),
                # AWS gives 100 GB of transfer out each month for free. Above it,
                # each result that a worker sends to R2 costs $0.09/GB.
                extra_usd_per_book=env_float("SUBPLZ_BURST_AWS_EGRESS_USD_PER_BOOK", 0.0),
                capacity=capacity,
            )
        except Exception:  # noqa: BLE001 - one cloud failing must not stop the others
            log.exception("AWS offers failed")
    if os.environ.get("HCLOUD_TOKEN"):
        try:
            import requests

            def get_json(path: str) -> dict:
                r = requests.get(f"https://api.hetzner.cloud/v1{path}", timeout=20,
                                 headers={"Authorization": f"Bearer {os.environ['HCLOUD_TOKEN']}"})
                r.raise_for_status()
                return r.json()

            limit = int(os.environ.get("SUBPLZ_BURST_HETZNER_SERVER_LIMIT", "5"))
            in_use = sum(1 for w in memory.workers.values() if w["provider"] == "hetzner")
            found += offers.hetzner_offers(
                get_json,
                machines=set(env_list("SUBPLZ_BURST_HETZNER_TYPES", "cx33,cx43,cpx31,cpx32,ccx13")),
                usd_per_eur=env_float("SUBPLZ_BURST_USD_PER_EUR", 1.15),
                # A primary IPv4 address: 0.50 EUR a month.
                ipv4_eur_per_hour=env_float("SUBPLZ_BURST_HETZNER_IPV4_EUR_PER_HOUR", 0.0007),
                capacity=max(0, limit - in_use),
            )
        except Exception:  # noqa: BLE001
            log.exception("Hetzner offers failed")
    return found


def revenue_30d() -> float:
    from backend.db import SessionLocal

    from .revenue import revenue_30d_usd

    with SessionLocal() as session:
        return revenue_30d_usd(session, datetime.now(timezone.utc))


def terraform_env(provider: str) -> dict[str, str]:
    """The environment of one `terraform apply`. The storage keys of the app are
    also AWS_* variables (R2 speaks the S3 protocol), so they must not reach the
    AWS provider: it gets the burst keys instead."""
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("AWS_") or k.startswith("SUBPLZ_WEB_"))}
    if provider == "aws":
        env["AWS_ACCESS_KEY_ID"] = os.environ.get("SUBPLZ_BURST_AWS_ACCESS_KEY_ID", "")
        env["AWS_SECRET_ACCESS_KEY"] = os.environ.get("SUBPLZ_BURST_AWS_SECRET_ACCESS_KEY", "")
        env["AWS_REGION"] = os.environ.get("SUBPLZ_BURST_AWS_REGION", "us-west-2")
    return env


def terraform_apply(terraform: list[str], module_dir: Path, var_file: Path, env: dict) -> bool:
    cmd = [*terraform, f"-chdir={module_dir}", "apply", "-auto-approve",
           "-input=false", "-no-color", f"-var-file={var_file}"]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        log.error("terraform apply failed in %s (%s):\n%s", module_dir, result.returncode,
                  result.stderr[-4000:])
        return False
    return True


def ensure_release(app_dir: Path, ref: str, bucket: str) -> None:
    """Put the app at `ref` in the bucket, where new workers download it: the
    repository is private, and a worker must run the web server's commit."""
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


def rollback(provider: str, old: Memory, new: Memory, now: float) -> Memory:
    """Undo one cloud's part of a decision after its apply failed, and rest it."""
    workers = {n: w for n, w in new.workers.items() if w["provider"] != provider}
    workers.update({n: w for n, w in old.workers.items() if w["provider"] == provider})
    new.workers = workers
    new.ledger = [e for e in new.ledger
                  if not (e.get("provider") == provider and e["end"] == now)]
    new.draining &= set(workers)
    new.cooldown = {**new.cooldown, provider: now + COOLDOWN_S}
    return new


# --- one run ---------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    from redis import Redis

    now = time.time()
    config = Config(max_workers=args.max_workers)
    ref = subprocess.run(["git", "-C", str(args.app_dir), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    conn = Redis.from_url(args.redis_url)
    waiting, live, rq_names = read_queue(conn)
    state_file = args.state_dir / "controller.json"
    memory = load_memory(state_file)
    offers = gather_offers(memory) if waiting else []
    cap = spend_cap_usd(revenue_30d(), k=args.cap_share, floor=args.cap_floor, ceiling=args.cap_ceiling)
    decision, new_memory = decide(now, waiting, live, memory, config, ref, offers, cap)
    log.info("waiting=%d live=%s spend30=$%.2f cap=$%.2f add=%s drain=%s remove=%s",
             waiting, live, spend_30d(now, memory), cap,
             [(n, o.provider, o.machine, o.zone, round(o.usd_per_book(config), 3))
              for n, o in decision.add],
             sorted(decision.drain), sorted(decision.remove))
    if waiting and not decision.add and not live:
        log.warning("paid jobs wait, and no burst worker can start (spend cap, or no offer)")
    if args.dry_run:
        return 0

    send_drain(conn, rq_names, decision.drain)
    if decision.add:
        ensure_release(args.app_dir, ref, args.bucket)
    status = 0
    for provider in PROVIDERS:
        if provider_vars(memory, provider) == provider_vars(new_memory, provider):
            continue
        var_file = args.state_dir / f"workers-{provider}.tfvars.json"
        save_json(var_file, provider_vars(new_memory, provider))
        if not terraform_apply(args.terraform, args.modules_dir / provider, var_file,
                               terraform_env(provider)):
            new_memory = rollback(provider, memory, new_memory, now)
            save_json(var_file, provider_vars(new_memory, provider))
            status = 1
    save_json(state_file, new_memory.to_json())
    return status


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m tools.burst", description=__doc__.splitlines()[0])
    p.add_argument("--max-workers", type=int,
                   default=int(os.environ.get("SUBPLZ_BURST_MAX_WORKERS", "1")))
    p.add_argument("--cap-share", type=float, default=env_float("SUBPLZ_BURST_CAP_SHARE", 0.30),
                   help="share of the net revenue of 30 days that burst workers may spend")
    p.add_argument("--cap-floor", type=float, default=env_float("SUBPLZ_BURST_CAP_FLOOR_USD", 5.0))
    p.add_argument("--cap-ceiling", type=float, default=env_float("SUBPLZ_BURST_CAP_CEILING_USD", 100.0))
    p.add_argument("--redis-url", default=os.environ.get("SUBPLZ_WEB_REDIS_URL", ""))
    p.add_argument("--bucket", default=os.environ.get("SUBPLZ_WEB_S3_BUCKET", ""))
    p.add_argument("--app-dir", type=Path, default=Path("/opt/subplz-web"))
    p.add_argument("--modules-dir", type=Path, default=Path("/opt/subplz-web/infra/burst/workers"))
    p.add_argument("--state-dir", type=Path, default=Path("/var/lib/subplz-burst"))
    p.add_argument("--terraform", nargs="+", default=["terraform"])
    p.add_argument("--dry-run", action="store_true", help="decide and log, change nothing")
    args = p.parse_args(argv)
    if not args.redis_url or not args.bucket:
        p.error("set SUBPLZ_WEB_REDIS_URL and SUBPLZ_WEB_S3_BUCKET (or --redis-url, --bucket)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
