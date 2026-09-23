"""Job dispatch.

Localhost runs jobs on a small thread pool inside the API process. The public
deployment sets SUBPLZ_WEB_QUEUE_BACKEND=redis and runs `worker.py` on separate
machines; the API side then only enqueues. Same `enqueue(job_id)` call either way.

A paid job and a free job go to different Redis queues. The web server's own
worker takes both, paid jobs first. A burst worker takes paid jobs only (see
infra/burst/README.md), so a free credit never starts a machine that costs money.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

from .settings import settings

log = logging.getLogger(__name__)

PAID_QUEUE = "subplz-jobs-paid"
FREE_QUEUE = "subplz-jobs"


class JobQueue(ABC):
    @abstractmethod
    def enqueue(self, job_id: str, paid: bool = True) -> None:
        """Run `job_id`. `paid` is False for a job that a free credit pays for."""

    @abstractmethod
    def depth(self) -> int:
        """Jobs waiting or running. Drives the 'N ahead of you' hint in the UI."""

    def shutdown(self) -> None:
        ...


class InProcessQueue(JobQueue):
    """Thread pool in the API process. Fine for one person on one machine.

    Alignment is CPU-bound and subplz is a subprocess, so the GIL is not the
    limit here - max_concurrent_jobs is.
    """

    def __init__(self, workers: int):
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="subplz-job"
        )
        self._lock = threading.Lock()
        self._pending: set[str] = set()

    def enqueue(self, job_id: str, paid: bool = True) -> None:
        with self._lock:
            if job_id in self._pending:
                return
            self._pending.add(job_id)
        self._pool.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        # Imported here to avoid a circular import at module load.
        from .runner import run_job

        try:
            run_job(job_id)
        except Exception:
            log.exception("job %s crashed outside the runner", job_id)
        finally:
            with self._lock:
                self._pending.discard(job_id)

    def depth(self) -> int:
        with self._lock:
            return len(self._pending)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class RedisQueue(JobQueue):
    """Public-release backend. Needs `rq` and a reachable Redis."""

    def __init__(self, url: str):
        from redis import Redis  # lazy: localhost needs neither package
        from rq import Queue as RQQueue

        self._conn = Redis.from_url(url)
        self._paid = RQQueue(PAID_QUEUE, connection=self._conn)
        self._free = RQQueue(FREE_QUEUE, connection=self._conn)

    def enqueue(self, job_id: str, paid: bool = True) -> None:
        from rq import Retry

        (self._paid if paid else self._free).enqueue(
            "backend.runner.run_job",
            job_id,
            job_timeout=settings.job_timeout_seconds,
            result_ttl=86400,
            # A worker can vanish during a job (a spot machine is taken back).
            # rq then puts the job back on its queue once: two attempts at most.
            # run_job never raises, so a failed book is not tried again.
            retry=Retry(max=1),
        )

    def depth(self) -> int:
        return sum(q.count + q.started_job_registry.count for q in (self._paid, self._free))


def build_queue() -> JobQueue:
    if settings.queue_backend == "redis":
        return RedisQueue(settings.redis_url)
    return InProcessQueue(settings.max_concurrent_jobs)


queue: JobQueue = build_queue()
