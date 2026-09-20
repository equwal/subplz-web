"""HTTP API.

Flow: drop files -> POST /api/uploads (stages, pairs, detects language) ->
POST /api/jobs/{id}/start (entitlement check, enqueue) -> poll GET /api/jobs/{id}
-> download from /api/jobs/{id}/files/{kind}.

Upload and start are separate so a wrong language guess costs a click rather
than a re-upload and a wasted multi-hour run.
"""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    APIRouter, Cookie, Depends, File, HTTPException, Response, UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import billing, convert, detect, languages, matching, pricing
from .aligner import aligner
from .db import Account, Artifact, Job, JobStatus, SessionLocal, new_id, utcnow
from .queue import queue
from .runner import (
    Paths, input_prefix, probe_duration, staged_audio_path, staged_part_path,
    staged_text_path,
)
from .settings import settings
from .storage import LocalStorage, storage

router = APIRouter(prefix="/api")

DEVICE_COOKIE = "subplz_device"
_CHUNK = 4 * 1024 * 1024


# --------------------------------------------------------------------------
# session / account
# --------------------------------------------------------------------------

def get_session():
    with SessionLocal() as s:
        yield s


def get_account(
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    subplz_device: Annotated[str | None, Cookie()] = None,
) -> Account:
    """Identify the caller.

    A cookie is the whole identity check, on purpose. Anyone who clears it gets
    another free book, and that is an accepted cost: hard verification would
    mean accounts, email and a signup wall in front of a tool whose pitch is
    "drop two files in". The real protection against abuse is capacity - the
    queue and per-worker limits - not identity.

    Swapping this for real auth later means changing this one function:
    everything downstream just receives an Account.
    """
    token = subplz_device
    account = None
    if token:
        account = session.query(Account).filter(Account.device_token == token).first()

    if account is None:
        token = secrets.token_urlsafe(24)
        account = Account(device_token=token)
        session.add(account)
        session.commit()
        response.set_cookie(
            DEVICE_COOKIE, token,
            max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
        )
    return account


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

class LanguageOut(BaseModel):
    code: str
    name: str
    splitter: str
    # Anything the active backend wants the user to know about this language.
    note: str | None = None


class DetectionOut(BaseModel):
    code: str | None
    name: str | None
    confidence: float
    supported: bool


class ArtifactOut(BaseModel):
    kind: str
    filename: str
    size_bytes: int
    url: str


class JobOut(BaseModel):
    id: str
    status: str
    stage: str
    progress: float
    language: str
    language_name: str
    splitter: str
    model: str
    audio_filename: str
    audio_parts: int
    text_filename: str
    audio_bytes: int
    audio_duration_seconds: float | None
    error: str | None
    created_at: str
    artifacts: list[ArtifactOut] = Field(default_factory=list)


class UploadOut(BaseModel):
    job: JobOut
    detected: DetectionOut
    # How well the book scores against a sample of the audio, using the
    # backend's own rule. None when the check is disabled.
    match: dict | None = None


class StartIn(BaseModel):
    language: str | None = None
    model: str | None = None


class AccountOut(BaseModel):
    id: str
    billing_enabled: bool
    free_allowance: int
    free_window_hours: int
    free_tier_summary: str
    purchased_credits: int
    used: int
    remaining: int
    allowed: bool
    next_free_at: str | None
    reason: str
    queue_depth: int


def _job_out(job: Job, arts: list[Artifact]) -> JobOut:
    lang = languages.get(job.language)
    return JobOut(
        id=job.id,
        status=job.status.value,
        stage=job.stage,
        progress=round(job.progress, 4),
        language=job.language,
        language_name=lang.name if lang else job.language,
        splitter=job.splitter,
        model=job.model,
        audio_filename=job.audio_filename,
        audio_parts=job.audio_parts or 1,
        text_filename=job.text_filename,
        audio_bytes=job.audio_bytes,
        audio_duration_seconds=job.audio_duration_seconds,
        error=job.error,
        created_at=job.created_at.isoformat(),
        artifacts=[
            ArtifactOut(
                kind=a.kind, filename=a.filename, size_bytes=a.size_bytes,
                url=f"/api/jobs/{job.id}/files/{a.kind}",
            )
            for a in arts
        ],
    )


def _load(session: Session, account: Account, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None or job.account_id != account.id:
        raise HTTPException(404, "Job not found")
    return job


def _artifacts(session: Session, job_id: str) -> list[Artifact]:
    return (
        session.query(Artifact)
        .filter(Artifact.job_id == job_id)
        .order_by(Artifact.kind)
        .all()
    )


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@router.get("/languages", response_model=list[LanguageOut])
def list_languages():
    return [
        LanguageOut(
            code=l.code, name=l.name, splitter=l.splitter,
            note=aligner.language_note(l.code),
        )
        for l in languages.all_languages()
    ]


@router.get("/account", response_model=AccountOut)
def get_account_info(
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    ent = billing.check(session, account)
    return AccountOut(
        id=account.id,
        billing_enabled=settings.billing_enabled,
        free_allowance=ent.free_allowance,
        free_window_hours=ent.window_hours,
        free_tier_summary=pricing.free_tier_summary(),
        purchased_credits=ent.purchased_credits,
        used=ent.used,
        remaining=ent.remaining,
        allowed=ent.allowed,
        next_free_at=ent.next_free_at.isoformat() if ent.next_free_at else None,
        reason=ent.reason,
        queue_depth=queue.depth(),
    )


@router.get("/pricing")
def get_pricing():
    """The catalogue, for the paywall. Static - no account needed."""
    return {
        "free_tier": pricing.free_tier_summary(),
        "billing_enabled": settings.billing_enabled,
        "plans": pricing.as_dicts(),
    }


@router.post("/uploads", response_model=UploadOut)
async def create_upload(
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
    files: Annotated[list[UploadFile], File()],
):
    """Stage a dropped pair, work out which is which, and guess the language."""
    names = [f.filename or "" for f in files]
    try:
        pairing = detect.classify(names)
    except detect.DetectionError as exc:
        raise HTTPException(400, str(exc)) from exc

    job_id = new_id("job")
    paths = Paths.for_job(job_id)
    paths.create()

    try:
        order = {name: i for i, name in enumerate(pairing.audio_names)}
        by_name = {(f.filename or ""): f for f in files}

        text_path = staged_text_path(job_id, pairing.text_name)
        await _save(by_name[pairing.text_name], text_path)

        # fb2/mobi/azw3 become something the aligner can read. Do it now, not at
        # run time, so a book we cannot open fails while the user is watching.
        if convert.needs_conversion(pairing.text_name):
            try:
                converted = convert.to_readable(
                    text_path, text_path.with_suffix("")
                )
            except convert.ConversionError as exc:
                raise HTTPException(400, str(exc)) from exc
            if converted != text_path:
                text_path.unlink(missing_ok=True)
                text_path = converted

        audio_paths: list[Path] = []
        for name in pairing.audio_names:
            upload = by_name.get(name)
            if upload is None:
                raise HTTPException(400, f"Upload did not include {name}.")
            dest = (
                staged_part_path(job_id, order[name] + 1, name)
                if pairing.is_multipart
                else staged_audio_path(job_id, name)
            )
            # The staged stem is shared between audio and text, so a single
            # audio file must not land on the text file's path.
            if dest == text_path:
                raise HTTPException(
                    400, "The audiobook and the book must be different formats."
                )
            await _save(upload, dest)
            audio_paths.append(dest)

        if not audio_paths:
            raise HTTPException(400, "Upload did not include any audio.")

        detection = detect.detect_language(detect.extract_text_sample(text_path))
        # Parts are merged at run time, so total the durations here.
        durations = [probe_duration(p) for p in audio_paths]
        duration = sum(d for d in durations if d) if any(durations) else None

        # Does this text actually belong to this audio? Answering now costs a
        # few seconds; finding out during the run costs the whole run.
        match = matching.check(
            audio_paths[0],
            text_path,
            detection.code if detection.supported else "en",
            aligner,
        )

        language = detection.code if detection.supported else "en"
        lang = languages.require(language)

        job = Job(
            id=job_id,
            account_id=account.id,
            status=JobStatus.draft,
            language=lang.code,
            splitter=lang.splitter,
            model=settings.model,
            audio_filename=pairing.display_name,
            text_filename=pairing.text_name,
            audio_parts=len(audio_paths),
            audio_bytes=sum(p.stat().st_size for p in audio_paths),
            audio_duration_seconds=duration,
            stage="Ready to start",
        )
        session.add(job)
        session.commit()

    except HTTPException:
        shutil.rmtree(paths.root, ignore_errors=True)
        raise
    except detect.DetectionError as exc:
        shutil.rmtree(paths.root, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(paths.root, ignore_errors=True)
        raise HTTPException(500, f"Upload failed: {exc}") from exc

    return UploadOut(
        job=_job_out(job, []),
        detected=DetectionOut(
            code=detection.code, name=detection.name,
            confidence=round(detection.confidence, 4),
            supported=detection.supported,
        ),
        match=match.as_dict(),
    )


async def _save(upload: UploadFile, dest: Path) -> None:
    """Stream to disk in chunks - these files run to hundreds of megabytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with dest.open("wb") as out:
        while chunk := await upload.read(_CHUNK):
            written += len(chunk)
            if written > settings.max_upload_bytes:
                raise HTTPException(
                    413,
                    f"{upload.filename} exceeds the "
                    f"{settings.max_upload_bytes // (1024**3)} GiB upload limit.",
                )
            out.write(chunk)


@router.post("/jobs/{job_id}/start", response_model=JobOut)
def start_job(
    job_id: str,
    body: StartIn,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    job = _load(session, account, job_id)
    # A failed job keeps its staged inputs, so it can be retried in place -
    # usually after correcting the language.
    if job.status not in (JobStatus.draft, JobStatus.failed):
        raise HTTPException(409, f"Job is already {job.status.value}.")
    if job.status == JobStatus.failed and not Paths.for_job(job.id).inp.exists():
        raise HTTPException(
            409, "The uploaded files for this job are gone. Upload them again."
        )

    if body.language:
        try:
            lang = languages.require(body.language)
        except languages.UnsupportedLanguage as exc:
            raise HTTPException(400, str(exc)) from exc
        job.language, job.splitter = lang.code, lang.splitter
    if body.model:
        job.model = body.model

    ent = billing.check(session, account)
    if not ent.allowed:
        raise HTTPException(402, ent.reason)

    # With an external queue the worker is probably not this machine, so the
    # staged inputs have to go somewhere both sides can reach before enqueuing.
    if settings.queue_backend != "memory":
        paths = Paths.for_job(job.id)
        prefix = input_prefix(job.id)
        for local in sorted(paths.inp.rglob("*")):
            if local.is_file():
                rel = local.relative_to(paths.inp).as_posix()
                storage.put_file(f"{prefix}/{rel}", local)

    billing.consume(session, job)
    job.status = JobStatus.queued
    job.stage = "Queued"
    job.progress = 0.0
    job.error = None
    session.commit()

    queue.enqueue(job.id)
    return _job_out(job, [])


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    jobs = (
        session.query(Job)
        .filter(Job.account_id == account.id)
        .order_by(Job.created_at.desc())
        .limit(50)
        .all()
    )
    return [_job_out(j, _artifacts(session, j.id)) for j in jobs]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    job = _load(session, account, job_id)
    return _job_out(job, _artifacts(session, job.id))


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(
    job_id: str,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    job = _load(session, account, job_id)
    if job.status in (JobStatus.succeeded, JobStatus.failed, JobStatus.canceled):
        raise HTTPException(409, f"Job is already {job.status.value}.")
    job.status = JobStatus.canceled
    job.stage = "Canceled"
    job.finished_at = utcnow()
    # A canceled job must not eat the free conversion.
    billing.refund(session, job)
    session.commit()
    return _job_out(job, [])


@router.delete("/jobs/{job_id}")
def delete_job(
    job_id: str,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    job = _load(session, account, job_id)
    if job.status in (JobStatus.queued, JobStatus.running):
        raise HTTPException(409, "Cancel the job before deleting it.")
    storage.delete_prefix(job.id)
    shutil.rmtree(Paths.for_job(job.id).root, ignore_errors=True)
    session.delete(job)
    session.commit()
    return {"deleted": job_id}


@router.get("/jobs/{job_id}/files/{kind}")
def download(
    job_id: str,
    kind: Literal["srt", "video", "metadata", "log"],
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    job = _load(session, account, job_id)
    art = (
        session.query(Artifact)
        .filter(Artifact.job_id == job.id, Artifact.kind == kind)
        .first()
    )
    if art is None:
        raise HTTPException(404, f"No {kind} for this job.")

    # S3 hands the browser a presigned URL; local storage serves the file.
    url = storage.presigned_url(art.storage_key, art.filename)
    if url:
        return RedirectResponse(url, status_code=307)

    assert isinstance(storage, LocalStorage)
    return FileResponse(
        storage.path_for(art.storage_key),
        filename=art.filename,
        media_type="application/octet-stream",
    )
