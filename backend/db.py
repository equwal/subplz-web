"""Persistence. SQLite on localhost, Postgres in production - same models."""

import enum
import secrets
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
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

    Every visitor starts as an anonymous row keyed by a cookie. It becomes a
    real account the moment an email is attached - by following a sign-in link
    or by paying, since Stripe collects one at checkout.
    """

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("acct")
    )
    # Opaque token the browser stores; becomes a real session subject later.
    device_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Lowercased. Unique, so an email always resolves to exactly one account.
    email: Mapped[str | None] = mapped_column(
        String(320), nullable=True, unique=True, index=True
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # Paid conversions in hand. One credit = one book with every output.
    purchased_credits: Mapped[int] = mapped_column(Integer, default=0)
    # Free credits spent so far. The free credits left are the allowance
    # (billing.free_allowance) minus this count. Thus a sign-in raises the
    # allowance, and a merge adds the two counts together.
    free_credits_used: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    # 1 once the owner of the email opened a sign-in link that we sent to it.
    # An email that came only from a payment is not verified.
    email_verified: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    # The last day (UTC) on which the account spent its daily free credit.
    daily_credit_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    # The unlimited plan. Status is Stripe's own word for it (active, past_due,
    # canceled...); period_end is when the paid-for time runs out.
    subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    subscription_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set when this anonymous row was folded into a signed-in account. A
    # payment can finish without a browser attached (the webhook), so the
    # cookie is re-pointed lazily, the next time this device shows up.
    merged_into: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    jobs: Mapped[list["Job"]] = relationship(back_populates="account")

    @property
    def signed_in(self) -> bool:
        """Whether this is a real account rather than an anonymous cookie.

        Gates the features that cost us money or need someone to bill. An
        anonymous visitor still gets their free book; they just cannot attach
        a custom cover.
        """
        return bool(self.email)


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
    # Custom cover art for the rendered video. Requires a signed-in account;
    # anonymous jobs fall back to the epub's own cover.
    cover_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    audio_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    audio_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    stage: Mapped[str] = mapped_column(String(128), default="queued")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Not used from 2.2 on. It counted jobs in the free window of earlier versions.
    billed: Mapped[int] = mapped_column(Integer, default=0)
    # "free": the job ran in the visitor's browser. "cloud": it ran on this
    # server and took a credit. (Rows from before 2.2 can say "youtube".)
    tier: Mapped[str] = mapped_column(
        String(16), default="free", server_default=text("'free'")
    )
    # 1 when the work happens in the visitor's browser and the server only
    # keeps the books: nothing to queue, nothing to resume after a restart.
    local: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # 1 if a bought credit was spent on this job, so a failed run can hand it back.
    credit_spent: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    # 1 if a free credit was spent on this job. A failed run gives it back to
    # the free credits, not to the bought ones.
    free_credit_spent: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    # Set if this job spent a daily free credit: the day of that credit.
    daily_credit_on: Mapped[date | None] = mapped_column(Date, nullable=True)

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


class LoginToken(Base):
    """A one-time sign-in link. Only the hash is stored, so a leaked database
    cannot be replayed into anyone's account."""

    __tablename__ = "login_tokens"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("login")
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    # The device that asked, so its anonymous jobs follow it into the account.
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Purchase(Base):
    """Ledger of completed checkouts.

    The unique session id is what makes fulfilment idempotent: Stripe delivers
    a payment twice (the webhook and the browser's return trip) and retries
    webhooks freely, and each of those must credit the account exactly once.
    """

    __tablename__ = "purchases"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: new_id("buy")
    )
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    plan_id: Mapped[str] = mapped_column(String(32))
    credits: Mapped[int] = mapped_column(Integer, default=0)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    stripe_session_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


_is_sqlite = settings.resolved_database_url.startswith("sqlite")

_engine = create_engine(
    settings.resolved_database_url,
    # check_same_thread only matters for SQLite plus our worker threads.
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def _add_missing_columns() -> None:
    """Bring an existing database up to the current models, additively.

    create_all() makes missing tables but never touches one that exists, so a
    deploy that adds a column would otherwise need hand-run SQL on the server.
    This only ever ADDs a column - anything destructive is still a deliberate,
    manual migration.
    """
    insp = inspect(_engine)
    with _engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = (
                    f"ALTER TABLE {table.name} ADD COLUMN {col.name} "
                    f"{col.type.compile(dialect=_engine.dialect)}"
                )
                if col.server_default is not None:
                    ddl += f" DEFAULT {col.server_default.arg.text}"
                conn.execute(text(ddl))

            # A column added above cannot carry its index along with it.
            existing = {i["name"] for i in insp.get_indexes(table.name)}
            for index in table.indexes:
                if index.name not in existing:
                    index.create(conn)


def init_db() -> None:
    Base.metadata.create_all(_engine)
    _add_missing_columns()
