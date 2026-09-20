"""Outbound email. Plain SMTP, so any provider works; nothing else is sent."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .settings import settings

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    pass


def send_login_link(to: str, url: str) -> bool:
    """Email a sign-in link. Returns False when there is no mail server.

    That only happens in development (dev_login_links), where the API hands
    the link back itself. It is deliberately not logged: a sign-in link in a
    log file is a credential in a log file.
    """
    if not settings.email_configured:
        return False

    minutes = settings.login_link_minutes
    msg = EmailMessage()
    msg["Subject"] = "Your sign-in link"
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg.set_content(
        f"Open this link to sign in:\n\n{url}\n\n"
        f"It works once and expires in {minutes} minutes. "
        "If you did not ask for it, ignore this email - nothing happens "
        "unless the link is opened.\n"
    )
    msg.add_alternative(
        f"<p>Open this link to sign in:</p>"
        f'<p><a href="{url}">Sign in</a></p>'
        f'<p style="color:#666;font-size:13px">It works once and expires in '
        f"{minutes} minutes. If you did not ask for it, ignore this email - "
        f"nothing happens unless the link is opened.</p>",
        subtype="html",
    )

    try:
        if settings.smtp_port == 465:
            smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
        else:
            smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        with smtp:
            if settings.smtp_port != 465 and settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("could not send sign-in link to %s: %s", to, exc)
        raise MailError(
            "The sign-in email could not be sent. Try again shortly."
        ) from exc
    return True
