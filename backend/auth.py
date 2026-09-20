"""Email sign-in links.

No passwords: a link is mailed, opening it signs the browser in. The link
carries a random token; only its hash is stored, it works once, and it expires.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import accounts
from .db import Account, LoginToken, utcnow
from .settings import settings

# Per address and per device, per hour. Enough for someone fumbling a typo,
# not enough to use us as a mail cannon.
_MAX_LINKS_PER_HOUR = 5


class TooManyRequests(RuntimeError):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(session: Session, account: Account, email: str) -> str:
    """Create a sign-in token for `email` and return the link to send."""
    since = utcnow() - timedelta(hours=1)
    recent = (
        session.query(func.count(LoginToken.id))
        .filter(
            LoginToken.created_at >= since,
            (LoginToken.email == email) | (LoginToken.account_id == account.id),
        )
        .scalar()
        or 0
    )
    if recent >= _MAX_LINKS_PER_HOUR:
        raise TooManyRequests(
            "Too many sign-in links requested. Wait a little and try again."
        )

    token = secrets.token_urlsafe(32)
    session.add(
        LoginToken(
            token_hash=_hash(token),
            email=email,
            account_id=account.id,
            expires_at=utcnow() + timedelta(minutes=settings.login_link_minutes),
        )
    )
    session.commit()
    # Lands on the page, which then POSTs the token back. Mail scanners follow
    # links but do not run scripts, so they cannot burn a one-time token.
    return f"{settings.public_base_url.rstrip('/')}/?login={token}"


def redeem(session: Session, token: str, device: Account) -> Account | None:
    """Spend a token. Returns the account to sign this browser into."""
    row = (
        session.query(LoginToken)
        .filter(LoginToken.token_hash == _hash(token or ""))
        .first()
    )
    if row is None or row.used_at is not None:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:  # SQLite hands back naive datetimes
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < utcnow():
        return None

    row.used_at = utcnow()
    # Fold in the device that opened the link. Usually that is the one that
    # asked for it; when it is not (link opened on a phone), this still does
    # the right thing - the phone's anonymous work joins the account.
    owner = accounts.adopt_email(session, device, row.email)
    session.commit()
    return owner
