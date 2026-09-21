"""Configuration. Every scale-out seam is an env var with a localhost-friendly default."""

import shutil
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


def _find_subplz() -> str:
    """Locate the alignment backend on PATH.

    subplz is an ordinary dependency of this project, installed into whatever
    environment is running it - not a sibling checkout. Falling back to the bare
    name keeps the error useful when it is genuinely missing.
    """
    return shutil.which("subplz") or "subplz"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUBPLZ_WEB_", env_file=".env", extra="ignore")

    # --- paths -------------------------------------------------------------
    data_dir: Path = ROOT / "data"
    # Override with SUBPLZ_WEB_SUBPLZ_BIN to point at a specific build.
    subplz_bin: Path | str = _find_subplz()

    # --- alignment ---------------------------------------------------------
    # Which backend does the aligning. See aligner.py to add another.
    aligner: str = "subplz"
    # "tiny" is what upstream recommends for audiobooks: the transcript only has
    # to be good enough to align against text we already have.
    model: str = "tiny"
    device: Literal["cpu", "cuda"] = "cpu"
    # 0 = let the runner pick from CPU count.
    threads: int = 0
    # Wall-clock ceiling per job so a wedged run cannot hold a worker forever.
    job_timeout_seconds: int = 6 * 60 * 60

    # --- scale-out seams ---------------------------------------------------
    # "memory" runs jobs in a thread in this process (localhost).
    # "redis" hands them to external workers (public deployment).
    queue_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    # How many jobs this process will run at once when queue_backend="memory".
    max_concurrent_jobs: int = 1

    # "local" writes under data_dir. "s3" writes to a bucket.
    storage_backend: Literal["local", "s3"] = "local"
    s3_bucket: str = ""
    s3_prefix: str = "jobs/"
    # Presigned-URL lifetime for s3 downloads.
    download_url_ttl_seconds: int = 3600

    # How long the server keeps a visitor's files. See retention.py.
    input_retention_hours: int = 24
    artifact_retention_days: int = 7

    # SQLite locally; set to a postgresql+psycopg:// URL in production.
    database_url: str = ""

    # --- billing -----------------------------------------------------------
    # The public offer: free in the visitor's browser; a conversion on this
    # server's hardware takes a credit. See billing.py.
    # True when fast conversion on this server's hardware is on offer. While it
    # is false the site sells nothing, whatever billing_enabled says: credits
    # that buy nothing must not be for sale.
    cloud_enabled: bool = False
    # Where customers write to: shown on the terms page and in the footer. The
    # payment processor wants one on the site before it lets a shop go live.
    contact_email: str = ""
    # Off on localhost so nothing blocks you; flip on for the public release.
    billing_enabled: bool = False

    # --- accounts & payments -----------------------------------------------
    # What customers see on the Stripe page and in the sign-in email.
    site_name: str = "SubRead"
    # The origin users reach this on. Sign-in links and Stripe's return URLs
    # are built from it, so it must be right in production.
    public_base_url: str = "http://127.0.0.1:8420"
    # Send the identity cookie over HTTPS only. On for any public deployment.
    cookie_secure: bool = False

    # Stripe. Leave empty and checkout reports itself unavailable rather than
    # failing half way through a purchase.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # Sign-in links go out over plain SMTP, so any provider works. With no
    # host set, sign-in is simply unavailable (see dev_login_links below).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "SubRead <login@subread.space>"
    smtp_starttls: bool = True
    login_link_minutes: int = 30
    # Hand the sign-in link back in the API response instead of mailing it.
    # For development ONLY: it signs anyone in as any address. Never inferred
    # from other settings - a public server with billing off is still public.
    dev_login_links: bool = False

    # --- match check -------------------------------------------------------
    # Transcribe a few short samples on upload and score them against the book,
    # so a mismatched pair fails in seconds instead of after a full run.
    match_check: bool = True
    # Clean the book text before alignment to raise the match score.
    text_prep: bool = True

    # --- video -------------------------------------------------------------
    # Render a YouTube-ready MP4 (cover image + audio + soft subtitle track).
    render_video: bool = True
    video_width: int = 1920
    video_height: int = 1080
    # 1 fps is the minimum YouTube accepts and all a still image needs.
    video_fps: int = 1
    # "auto" picks the best H.264 encoder this ffmpeg build actually has.
    # Force one with libx264 / h264_nvenc / h264_amf / h264_qsv / libopenh264.
    video_encoder: str = "auto"
    video_crf: int = 28

    # --- uploads -----------------------------------------------------------
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB

    @property
    def payments_configured(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host)

    @property
    def sign_in_available(self) -> bool:
        return self.email_configured or self.dev_login_links

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'subplz.db').as_posix()}"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
