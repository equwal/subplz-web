"""Persistence. SQLite on localhost, Postgres in production - same models."""

import enum
import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from .settings import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    # Uploaded and analysed, but not started: the user still gets to correct the
    # detected language before committing to an hours-long alignment.
    draft = "draft"
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


class Account(Base):
    """One row per identified user.

    On localhost everyone shares a single anonymous account. For the public
    release this gains an auth provider id, an email and a Stripe customer id -
    billing.py already reads its allowance from here.
    """

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("acct")
    )
    # Opaque token the browser stores; becomes a real session subject later.
    device_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Conversions bought beyond the free allowance.
    purchased_credits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    jobs: Mapped[list["Job"]] = relationship(back_populates="account")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("job")
    )
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.queued, index=True
    )
    language: Mapped[str] = mapped_column(String(16))
    splitter: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(32))

    # Display name. For a per-chapter audiobook this is "01.mp3 + 43 more".
    audio_filename: Mapped[str] = mapped_column(String(512))
    text_filename: Mapped[str] = mapped_column(String(512))
    # 1 for a single file; higher when the book arrived as per-chapter parts
    # that get merged before alignment.
    audio_parts: Mapped[int] = mapped_column(Integer, default=1)
    audio_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    audio_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    stage: Mapped[str] = mapped_column(String(128), default="queued")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 1 once the job has consumed the free/paid allowance.
    billed: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    account: Mapped[Account] = relationship(back_populates="jobs")
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class Artifact(Base):
    """A downloadable output of a job: the subtitles, metadata and run log."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("art")
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # srt | metadata | log
    filename: Mapped[str] = mapped_column(String(512))
    # Opaque storage key, not a filesystem path.
    storage_key: Mapped[str] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    job: Mapped[Job] = relationship(back_populates="artifacts")


_is_sqlite = settings.resolved_database_url.startswith("sqlite")

_engine = create_engine(
    settings.resolved_database_url,
    # check_same_thread only matters for SQLite plus our worker threads.
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(_engine)
