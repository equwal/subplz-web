"""Standalone job worker for the public deployment.

On localhost the API process runs jobs itself and you never need this. When you
set SUBPLZ_WEB_QUEUE_BACKEND=redis, the API only enqueues, and these processes
do the work:

    # one per GPU box, or several per box if you have the RAM
    SUBPLZ_WEB_QUEUE_BACKEND=redis \
    SUBPLZ_WEB_REDIS_URL=redis://redis:6379/0 \
    SUBPLZ_WEB_DATABASE_URL=postgresql+psycopg://... \
    SUBPLZ_WEB_STORAGE_BACKEND=s3 SUBPLZ_WEB_S3_BUCKET=... \
    SUBPLZ_WEB_DEVICE=cuda SUBPLZ_WEB_MODEL=tiny \
    python worker.py

Workers and the API must share the database and the storage bucket. They do not
need to share a filesystem: staged uploads are the one thing that is local to
whoever received them, which is why the API writes uploads to shared storage
before enqueuing when the queue backend is redis.
"""

from __future__ import annotations

import logging
import sys

from backend.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("subplz.worker")


def main() -> int:
    if settings.queue_backend != "redis":
        log.error(
            "queue_backend is %r. worker.py is only for the redis backend - "
            "with the memory backend the API process runs jobs itself.",
            settings.queue_backend,
        )
        return 2

    try:
        from redis import Redis
        from rq import Queue, Worker
    except ImportError:
        log.error("missing dependencies: pip install redis rq")
        return 2

    conn = Redis.from_url(settings.redis_url)
    queue = Queue("subplz-jobs", connection=conn)

    log.info(
        "worker starting | redis=%s | device=%s model=%s | storage=%s",
        settings.redis_url, settings.device, settings.model,
        settings.storage_backend,
    )
    # burst=False: stay alive and keep taking jobs.
    Worker([queue], connection=conn).work(with_scheduler=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
