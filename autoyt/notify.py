"""Email notifications through the user's SMTP server (smtplib only)."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import Settings

log = logging.getLogger("autoyt.notify")


def smtp_configured(s: Settings) -> bool:
    return bool(s.smtp_host and s.smtp_from and s.notify_email)


def send_email(s: Settings, subject: str, body: str) -> bool:
    """Send one email. Never raises: SMTP trouble goes to the log only."""
    if not smtp_configured(s):
        log.warning("SMTP not configured; email skipped: %s", subject)
        return False
    msg = EmailMessage()
    msg["Subject"] = f"[AutoYT] {subject}"
    msg["From"] = s.smtp_from
    msg["To"] = s.notify_email
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        if s.smtp_port == 465:
            with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30, context=ctx) as srv:
                if s.smtp_username:
                    srv.login(s.smtp_username, s.smtp_password)
                srv.send_message(msg)
        else:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as srv:
                srv.ehlo()
                try:
                    srv.starttls(context=ctx)
                    srv.ehlo()
                except smtplib.SMTPNotSupportedError:
                    log.warning("SMTP server has no STARTTLS; sending without it")
                if s.smtp_username:
                    srv.login(s.smtp_username, s.smtp_password)
                srv.send_message(msg)
        log.info("email sent: %s", subject)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("email failed (%s): %s", subject, e)
        return False


def notify_once(s: Settings, db, day: str, kind: str, subject: str, body: str) -> None:
    """At most one email per (day, kind)."""
    if db.was_notified(day, kind):
        return
    if send_email(s, subject, body):
        db.mark_notified(day, kind)
