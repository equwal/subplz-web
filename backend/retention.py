"""What the server keeps, and for how long.

A server job brings an audiobook and a book onto this machine. They are the
visitor's files, often bought ones, and the disk is small. So nothing stays
longer than it is of use:

  uploaded files   deleted when the job succeeds (runner.py). A job that was
                   never started, failed or was cancelled keeps them for
                   `input_retention_hours`, so the visitor can fix the language
                   and try again; then they go too.
  results          subtitles and videos stay for `artifact_retention_days`,
                   then they are deleted. The row of the job stays, so the list
                   still says what was converted.

Jobs that ran in the visitor's browser have no files here, only the subtitles
the browser sent back; those follow the rule for results.
"""

from __future__ import annotations

import logging
import shutil
from datetime import timedelta

from sqlalchemy.orm import Session

from .db import Artifact, Job, JobStatus, utcnow
from .runner import Paths
from .settings import settings
from .storage import storage

log = logging.getLogger("subplz-web.retention")

_IDLE = [JobStatus.draft, JobStatus.failed, JobStatus.canceled]


def sweep(session: Session) -> dict[str, int]:
    """Delete what is past its time. Safe to run at any moment, and again."""
    now = utcnow()
    done = {"inputs": 0, "drafts": 0, "results": 0}

    old_idle = (
        session.query(Job)
        .filter(Job.local == 0, Job.status.in_(_IDLE),
                Job.created_at < now - timedelta(hours=settings.input_retention_hours))
        .all()
    )
    for job in old_idle:
        root = Paths.for_job(job.id).root
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
            done["inputs"] += 1
        _delete_stored(job.id)
        if job.status == JobStatus.draft:
            # Never started: nothing to show for it.
            session.delete(job)
            done["drafts"] += 1

    old_results = (
        session.query(Job)
        .join(Artifact, Artifact.job_id == Job.id)
        .filter(Job.status == JobStatus.succeeded,
                Job.finished_at < now - timedelta(days=settings.artifact_retention_days))
        .distinct()
        .all()
    )
    for job in old_results:
        _delete_stored(job.id)
        shutil.rmtree(Paths.for_job(job.id).root, ignore_errors=True)
        session.query(Artifact).filter(Artifact.job_id == job.id).delete()
        job.stage = f"Files deleted after {settings.artifact_retention_days} days"
        done["results"] += 1

    session.commit()
    if any(done.values()):
        log.info("retention sweep: %s", done)
    return done


def _delete_stored(job_id: str) -> None:
    try:
        storage.delete_prefix(job_id)
    except Exception as exc:  # noqa: BLE001 - the next sweep tries again
        log.warning("could not delete stored files of %s: %s", job_id, exc)
