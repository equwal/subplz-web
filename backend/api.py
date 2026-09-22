"""HTTP API.

Flow: drop files -> POST /api/uploads (stages, pairs, detects language) ->
POST /api/jobs/{id}/start (entitlement check, enqueue) -> poll GET /api/jobs/{id}
-> download from /api/jobs/{id}/files/{kind}.

Upload and start are separate so a wrong language guess costs a click rather
than a re-upload and a wasted multi-hour run.

Around that: /api/auth/* (email sign-in links) and /api/billing/* (Stripe
checkout, its return trip and its webhook).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    APIRouter, Cookie, Depends, File, HTTPException, Request, Response,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import (
    accounts, auth, billing, convert, detect, languages, mailer, matching,
    payments, pricing,
)
from .aligner import aligner
from .db import Account, Artifact, Job, JobStatus, SessionLocal, new_id, utcnow
from .queue import queue
from .runner import (
    Paths, input_prefix, probe_duration, staged_audio_path, staged_cover_path,
    staged_part_path, staged_text_path,
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

    A cookie is the whole identity check for the free tier, on purpose. Anyone
    who clears it gets another free book, and that is an accepted cost: a
    signup wall does not belong in front of a tool whose pitch is "drop two
    files in". The real protection against abuse is capacity - the queue and
    per-worker limits - not identity.

    Signing in does not replace the cookie, it re-points it: the cookie then
    names the signed-in account, on every device that has signed in.
    """
    account = None
    if subplz_device:
        account = (
            session.query(Account)
            .filter(Account.device_token == subplz_device)
            .first()
        )

    if account is None:
        account = accounts.new_account(session)
        session.commit()
        _set_identity(response, account)
        return account

    # This device's anonymous row was folded into a real account while no
    # browser was attached (a payment landing by webhook). Follow it.
    survivor = accounts.resolve(session, account)
    if survivor.id != account.id:
        _set_identity(response, survivor)
    return survivor


def _set_identity(response: Response, account: Account) -> None:
    response.set_cookie(
        DEVICE_COOKIE, account.device_token,
        max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax",
        secure=settings.cookie_secure,
    )


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
    cover_filename: str | None
    text_filename: str
    audio_bytes: int
    audio_duration_seconds: float | None
    error: str | None
    created_at: str
    local: bool = False
    tier: str = billing.FREE
    artifacts: list[ArtifactOut] = Field(default_factory=list)


class UploadOut(BaseModel):
    job: JobOut
    detected: DetectionOut
    # Set when an image was dropped but the visitor is not signed in.
    cover_requires_sign_in: bool = False
    # How well the book scores against a sample of the audio, using the
    # backend's own rule. None when the check is disabled.
    match: dict | None = None


class StartIn(BaseModel):
    language: str | None = None
    model: str | None = None


class AccountOut(BaseModel):
    id: str
    signed_in: bool
    email: str | None
    billing_enabled: bool
    # Whether the server can actually take money / send sign-in email yet.
    payments_available: bool
    email_sign_in_available: bool
    free_tier_summary: str
    # The cloud tier: conversions on this server's hardware.
    cloud_available: bool
    # Bought and free credits together.
    credits: int
    # The part of `credits` that is free.
    free_credits: int
    # The free credits this visitor has after a sign-in. Zero when signed in.
    free_credits_with_account: int
    subscribed: bool
    subscription_ends: str | None
    cloud_allowed: bool
    queue_depth: int


class LocalJobIn(BaseModel):
    """A conversion about to run in the visitor's browser."""
    audio_filename: str = Field(max_length=512)
    audio_parts: int = Field(default=1, ge=1, le=2000)
    audio_bytes: int = Field(default=0, ge=0)
    audio_duration_seconds: float | None = None
    text_filename: str = Field(max_length=512)
    language: str = Field(max_length=16)


class LocalFinishIn(BaseModel):
    # A twenty-hour book is a few megabytes of subtitles.
    srt: str = Field(max_length=8_000_000)
    filename: str = Field(max_length=512)
    metadata: dict = Field(default_factory=dict)


class LocalFailIn(BaseModel):
    error: str = Field(default="", max_length=4000)


class EmailIn(BaseModel):
    email: str


class TokenIn(BaseModel):
    token: str


class CheckoutIn(BaseModel):
    plan_id: str


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
        cover_filename=job.cover_filename,
        text_filename=job.text_filename,
        audio_bytes=job.audio_bytes,
        audio_duration_seconds=job.audio_duration_seconds,
        error=job.error,
        created_at=job.created_at.isoformat(),
        local=bool(job.local),
        tier=job.tier or billing.FREE,
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
    return _account_out(session, account)


def _account_out(session: Session, account: Account) -> AccountOut:
    ent = billing.check(account)
    return AccountOut(
        id=account.id,
        signed_in=account.signed_in,
        email=account.email,
        billing_enabled=settings.billing_enabled,
        payments_available=settings.payments_configured,
        email_sign_in_available=settings.sign_in_available,
        free_tier_summary=pricing.free_tier_summary(),
        credits=ent.credits,
        free_credits=ent.free_credits,
        free_credits_with_account=(
            0 if account.signed_in
            else billing.free_credits_left(account, signed_in=True)
        ),
        subscribed=ent.subscribed,
        subscription_ends=(
            ent.subscription_ends.isoformat() if ent.subscription_ends else None
        ),
        cloud_available=settings.cloud_enabled,
        cloud_allowed=ent.cloud_allowed or not settings.billing_enabled,
        queue_depth=queue.depth(),
    )


@router.get("/pricing")
def get_pricing():
    """The catalogue, for the paywall. Static - no account needed."""
    return {
        "free_tier": pricing.free_tier_summary(),
        "billing_enabled": settings.billing_enabled,
        "payments_available": settings.payments_configured,
        "tiers": pricing.TIER_OUTPUTS,
        "contact_email": settings.contact_email,
        "plans": pricing.as_dicts(),
    }


# --------------------------------------------------------------------------
# sign-in
# --------------------------------------------------------------------------

@router.post("/auth/request")
def request_sign_in(
    body: EmailIn,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    """Mail a one-time sign-in link."""
    try:
        email = accounts.normalize_email(body.email)
    except accounts.InvalidEmail as exc:
        raise HTTPException(400, str(exc)) from exc

    # A server that cannot send mail has no safe way to do this: the only
    # fallback is showing the link, which would sign anyone in as anyone.
    if not settings.sign_in_available:
        raise HTTPException(
            503, "Email sign-in is not set up on this server yet."
        )

    try:
        link = auth.issue(session, account, email)
    except auth.TooManyRequests as exc:
        raise HTTPException(429, str(exc)) from exc

    try:
        sent = mailer.send_login_link(email, link)
    except mailer.MailError as exc:
        raise HTTPException(502, str(exc)) from exc

    out = {"sent": sent, "email": email}
    if not sent and settings.dev_login_links:
        out["dev_link"] = link
    return out


@router.post("/auth/verify", response_model=AccountOut)
def verify_sign_in(
    body: TokenIn,
    response: Response,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    owner = auth.redeem(session, body.token, account)
    if owner is None:
        raise HTTPException(
            400, "That sign-in link has expired or was already used. "
                 "Request a new one."
        )
    _set_identity(response, owner)
    return _account_out(session, owner)


@router.post("/auth/signout")
def sign_out(response: Response):
    """Forget this browser. The account and everything in it stay put."""
    response.delete_cookie(DEVICE_COOKIE)
    return {"signed_out": True}


# --------------------------------------------------------------------------
# billing
# --------------------------------------------------------------------------

@router.post("/billing/checkout")
def create_checkout(
    body: CheckoutIn,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    plan = pricing.get(body.plan_id)
    if plan is None:
        raise HTTPException(404, "No such plan.")
    try:
        url = payments.start_checkout(session, account, plan)
    except payments.PaymentsUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except payments.PaymentError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"url": url}


@router.get("/billing/return")
def checkout_return(
    session_id: str,
    session: Annotated[Session, Depends(get_session)],
):
    """Where Stripe sends the browser after paying.

    Fulfils from here as well as from the webhook, so credits are there by the
    time the page loads rather than whenever the webhook gets round to it.
    Identity needs no handling: if paying folded this device into an existing
    account, get_account re-points the cookie on the very next request.
    """
    try:
        paid = payments.fulfil_by_id(session, session_id) is not None
    except payments.PaymentsUnavailable:
        paid = False
    state = "paid" if paid else "pending"
    return RedirectResponse(f"/?checkout={state}", status_code=303)


@router.post("/billing/webhook")
async def stripe_webhook(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
):
    payload = await request.body()
    try:
        event = payments.verify_webhook(
            payload, request.headers.get("stripe-signature")
        )
    except payments.PaymentsUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except payments.PaymentError as exc:
        raise HTTPException(400, str(exc)) from exc
    payments.handle_event(session, event)
    return {"received": True}


@router.post("/billing/portal")
def billing_portal(
    account: Annotated[Account, Depends(get_account)],
):
    try:
        return {"url": payments.portal_url(account)}
    except payments.PaymentsUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except payments.PaymentError as exc:
        raise HTTPException(400, str(exc)) from exc


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

        # Custom cover art is a signed-in feature. Say so plainly rather than
        # accepting the file and quietly ignoring it: the UI labels the drop
        # zone before anyone picks an image, and this is the backstop.
        cover_name = None
        cover_blocked = False
        if pairing.cover_name:
            if account.signed_in:
                upload = by_name.get(pairing.cover_name)
                if upload is not None:
                    await _save(upload, staged_cover_path(job_id, pairing.cover_name))
                    cover_name = pairing.cover_name
            else:
                cover_blocked = True

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
            cover_filename=cover_name,
            audio_bytes=sum(p.stat().st_size for p in audio_paths),
            audio_duration_seconds=duration,
            stage="Ready to start",
        )
        session.add(job)
        session.commit()

    # A refused upload keeps what arrived, for debugging. The operator deletes
    # it by hand.
    except HTTPException:
        raise
    except detect.DetectionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Upload failed: {exc}") from exc

    return UploadOut(
        job=_job_out(job, []),
        detected=DetectionOut(
            code=detection.code, name=detection.name,
            confidence=round(detection.confidence, 4),
            supported=detection.supported,
        ),
        match=match.as_dict(),
        cover_requires_sign_in=cover_blocked,
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

    try:
        billing.authorize_start(session, account, job)
    except billing.PaymentRequired as exc:
        session.rollback()
        raise HTTPException(402, str(exc)) from exc

    # With an external queue the worker is probably not this machine, so the
    # staged inputs have to go somewhere both sides can reach before enqueuing.
    if settings.queue_backend != "memory":
        paths = Paths.for_job(job.id)
        prefix = input_prefix(job.id)
        for local in sorted(paths.inp.rglob("*")):
            if local.is_file():
                rel = local.relative_to(paths.inp).as_posix()
                storage.put_file(f"{prefix}/{rel}", local)

    job.status = JobStatus.queued
    job.stage = "Queued"
    job.progress = 0.0
    job.error = None
    session.commit()

    queue.enqueue(job.id)
    return _job_out(job, [])


# --------------------------------------------------------------------------
# jobs that run in the browser
# --------------------------------------------------------------------------
#
# The audio never reaches us. The server's part is to say whether the job may
# start (the free window, or a credit), and to keep the finished subtitles so
# they are still there on another device. Nothing here can be enforced against
# someone who edits the page's JavaScript, and that is accepted: see billing.py.

_LOCAL_STALE_HOURS = 48


@router.post("/local/jobs", response_model=JobOut)
def start_local_job(
    body: LocalJobIn,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    # The same book started again - a closed tab, a reload - carries on under
    # the job it already paid for rather than being charged a second time.
    running = (
        session.query(Job)
        .filter(
            Job.account_id == account.id, Job.local == 1,
            Job.status == JobStatus.running,
            Job.audio_filename == body.audio_filename,
            Job.audio_bytes == body.audio_bytes,
        )
        .order_by(Job.created_at.desc())
        .first()
    )
    if running is not None:
        running.language = body.language
        session.commit()
        return _job_out(running, [])

    job = Job(
        account_id=account.id, status=JobStatus.running, local=1,
        language=body.language, splitter="browser", model="whisper-tiny",
        audio_filename=body.audio_filename, text_filename=body.text_filename,
        audio_parts=body.audio_parts, audio_bytes=body.audio_bytes,
        audio_duration_seconds=body.audio_duration_seconds,
        stage="Running in your browser", started_at=utcnow(),
    )
    session.add(job)
    try:
        billing.authorize_start(session, account, job)
    except billing.PaymentRequired as exc:
        session.rollback()
        raise HTTPException(402, str(exc)) from exc
    session.commit()
    return _job_out(job, [])


@router.post("/local/jobs/{job_id}/finish", response_model=JobOut)
def finish_local_job(
    job_id: str,
    body: LocalFinishIn,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    job = _load(session, account, job_id)
    if not job.local or job.status != JobStatus.running:
        raise HTTPException(409, f"Job is already {job.status.value}.")

    import json
    import tempfile

    name = Path(body.filename).name or "subtitles.srt"
    with tempfile.TemporaryDirectory() as tmp:
        for kind, filename, content in (
            ("srt", name, body.srt),
            ("metadata", "metadata.json",
             json.dumps({"job_id": job.id, **body.metadata}, ensure_ascii=False, indent=2)),
        ):
            src = Path(tmp) / filename
            # Bytes, not text: on Windows write_text would turn every line ending into CRLF.
            src.write_bytes(content.encode("utf-8"))
            key = f"{job.id}/{filename}"
            size = storage.put_file(key, src)
            session.add(Artifact(job_id=job.id, kind=kind, filename=filename,
                                 storage_key=key, size_bytes=size))

    job.status = JobStatus.succeeded
    job.stage, job.progress, job.finished_at = "Done", 1.0, utcnow()
    session.commit()
    return _job_out(job, _artifacts(session, job.id))


@router.post("/local/jobs/{job_id}/fail", response_model=JobOut)
def fail_local_job(
    job_id: str,
    body: LocalFailIn,
    account: Annotated[Account, Depends(get_account)],
    session: Annotated[Session, Depends(get_session)],
):
    job = _load(session, account, job_id)
    if not job.local or job.status != JobStatus.running:
        raise HTTPException(409, f"Job is already {job.status.value}.")
    job.status = JobStatus.failed
    job.stage, job.error, job.finished_at = "Failed", body.error or "Failed in the browser.", utcnow()
    # Whatever went wrong, they got nothing: the free slot or the credit goes back.
    billing.refund(session, job)
    session.commit()
    return _job_out(job, [])


def expire_stale_local_jobs(session: Session) -> int:
    """A tab that was closed for good never reports back. Release what it held."""
    from datetime import timedelta

    stale = (
        session.query(Job)
        .filter(Job.local == 1, Job.status == JobStatus.running,
                Job.created_at < utcnow() - timedelta(hours=_LOCAL_STALE_HOURS))
        .all()
    )
    for job in stale:
        job.status, job.stage, job.finished_at = JobStatus.canceled, "Abandoned", utcnow()
        billing.refund(session, job)
    session.commit()
    return len(stale)


@router.post("/convert")
async def convert_book(file: Annotated[UploadFile, File()]):
    """mobi / azw3 to epub. The one thing the browser cannot do for itself:
    those formats need a real parser, and a book is small enough to send."""
    name = Path(file.filename or "book").name
    if not convert.needs_conversion(name):
        raise HTTPException(400, "That format does not need converting.")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / name
        await _save(file, src)
        try:
            out = convert.to_readable(src, Path(tmp) / "converted")
        except convert.ConversionError as exc:
            raise HTTPException(400, str(exc)) from exc
        data = out.read_bytes()
    return Response(data, media_type="application/epub+zip",
                    headers={"X-Filename": Path(name).stem + ".epub"})


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
    """Take the job off the visitor's list. Its files stay on the server for
    debugging, and the operator deletes them by hand."""
    job = _load(session, account, job_id)
    if job.status in (JobStatus.queued, JobStatus.running):
        raise HTTPException(409, "Cancel the job before deleting it.")
    session.delete(job)
    session.commit()
    return {"deleted": job_id}


@router.get("/jobs/{job_id}/files/{kind}")
def download(
    job_id: str,
    kind: Literal["srt", "video", "video_embedded", "metadata", "log"],
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
