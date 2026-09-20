"""Application entrypoint.

    python -m uvicorn backend.main:app --host 127.0.0.1 --port 8420

Serves the API and the static frontend from one origin, so localhost needs no
CORS config and no second server.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .db import Job, JobStatus, SessionLocal, init_db
from .languages import all_languages
from .queue import queue
from .settings import ROOT, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("subplz.web")

FRONTEND = ROOT / "frontend"


def _requeue_interrupted() -> None:
    """Recover jobs that were mid-flight when the server last stopped.

    An in-process queue dies with the process, so anything left `running` is
    orphaned. Put it back in the queue rather than leaving a stuck progress bar.
    """
    with SessionLocal() as s:
        stale = (
            s.query(Job)
            .filter(Job.status.in_([JobStatus.running, JobStatus.queued]))
            .all()
        )
        for job in stale:
            job.status = JobStatus.queued
            job.stage = "Queued (resumed after restart)"
            job.progress = 0.0
        s.commit()
        ids = [j.id for j in stale]

    for job_id in ids:
        queue.enqueue(job_id)
    if ids:
        log.info("re-queued %d interrupted job(s)", len(ids))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    log.info(
        "subplz-web ready | %d languages | model=%s device=%s | "
        "queue=%s storage=%s | billing=%s",
        len(all_languages()), settings.model, settings.device,
        settings.queue_backend, settings.storage_backend,
        "on" if settings.billing_enabled else "off",
    )
    _requeue_interrupted()
    yield
    queue.shutdown()


app = FastAPI(
    title="SubPlz Web",
    summary="Drag an audiobook and an epub in; get split-timed SRT subtitles out.",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "languages": len(all_languages()),
        "queue_backend": settings.queue_backend,
        "queue_depth": queue.depth(),
        "storage_backend": settings.storage_backend,
    }


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND), name="static")
