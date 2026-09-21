"""Application entrypoint.

    python -m uvicorn backend.main:app --host 127.0.0.1 --port 8420

Serves the API and the static frontend from one origin, so localhost needs no
CORS config and no second server.
"""

from __future__ import annotations

import asyncio

import logging
import mimetypes
import subprocess
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


def _version() -> str:
    """The release this checkout is: the nearest git tag, e.g. v2.0.0 or
    v2.0.0-3-gabc1234 when it is ahead of one. Answers "what is deployed?"."""
    try:
        return subprocess.run(
            ["git", "-c", f"safe.directory={ROOT.as_posix()}", "describe", "--tags", "--always", "--dirty"],
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


VERSION = _version()

# Python takes these from the OS, and Windows gets both wrong. A module served
# as text/plain is refused outright, and WebAssembly will not stream-compile.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/wasm", ".wasm")


def _requeue_interrupted() -> None:
    """Recover jobs that were mid-flight when the server last stopped.

    An in-process queue dies with the process, so anything left `running` is
    orphaned. Put it back in the queue rather than leaving a stuck progress bar.
    """
    with SessionLocal() as s:
        stale = (
            s.query(Job)
            .filter(Job.status.in_([JobStatus.running, JobStatus.queued]), Job.local == 0)
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
    sweeper = asyncio.create_task(_housekeeping())
    yield
    sweeper.cancel()
    queue.shutdown()


async def _housekeeping() -> None:
    """Once an hour: let go of abandoned browser jobs, and delete files past their time."""
    from . import retention
    from .api import expire_stale_local_jobs

    while True:
        def once() -> None:
            with SessionLocal() as s:
                expire_stale_local_jobs(s)
                retention.sweep(s)

        try:
            await asyncio.to_thread(once)       # deleting a book's files is slow; do not hold the site up
        except Exception:  # noqa: BLE001 - housekeeping must not take the site down
            log.exception("housekeeping failed")
        await asyncio.sleep(3600)


app = FastAPI(
    title="SubPlz Web",
    summary="Drag an audiobook and an epub in; get split-timed SRT subtitles out.",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.middleware("http")
async def cross_origin_isolation(request, call_next):
    """Let the page use threads.

    The speech model runs in the browser on WebAssembly threads, which need
    SharedArrayBuffer, which browsers only hand to a cross-origin-isolated
    page. "credentialless" rather than "require-corp": the model weights come
    from a third-party host that sends CORS headers but not CORP ones.
    """
    response = await call_next(request)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
    return response


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "version": VERSION,
        "languages": len(all_languages()),
        "queue_backend": settings.queue_backend,
        "queue_depth": queue.depth(),
        "storage_backend": settings.storage_backend,
    }


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND), name="static")
