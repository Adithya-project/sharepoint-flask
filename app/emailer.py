"""
app/mail.py — outgoing email (welcome emails, site/file share invites).

Uses Flask-Mail configured for Gmail SMTP with an app password. If mail
isn't configured, every send_* function logs a warning and returns False
instead of raising — sending mail is a nice-to-have, it should never break
a signup or a share action.

Setup (Gmail):
  1. Turn on 2-Step Verification on the Gmail account you want to send from.
  2. Create an App Password: https://myaccount.google.com/apppasswords
     (choose "Mail" as the app). Google gives you a 16-character password.
  3. In .env, set:
       MAIL_USERNAME=youraddress@gmail.com
       MAIL_PASSWORD=<the 16-char app password, no spaces>
       MAIL_DEFAULT_SENDER=youraddress@gmail.com
"""

from __future__ import annotations

import logging
import os

from flask import current_app
from flask_mail import Message

log = logging.getLogger(__name__)


def _mail_configured() -> bool:
    return bool(
        current_app.config.get("MAIL_USERNAME")
        and current_app.config.get("MAIL_PASSWORD")
    )


def send_email(
    to: str,
    subject: str,
    body: str,
    attachment_path: str | None = None,
    attachment_name: str | None = None,
) -> bool:
    """Send a plain-text email, optionally with one file attached.
    Returns True on success, False otherwise. Never raises — callers
    shouldn't have to wrap this in try/except."""
    if not to:
        return False
    if not _mail_configured():
        log.warning(
            "Email not sent (MAIL_USERNAME/MAIL_PASSWORD not set in .env): "
            "to=%s subject=%r",
            to, subject,
        )
        return False

    from app import mail  # local import avoids circular import at app init

    try:
        msg = Message(subject=subject, recipients=[to], body=body)
        if attachment_path:
            import mimetypes
            content_type = mimetypes.guess_type(attachment_path)[0] or "application/octet-stream"
            with open(attachment_path, "rb") as f:
                msg.attach(
                    filename=attachment_name or os.path.basename(attachment_path),
                    content_type=content_type,
                    data=f.read(),
                )
        mail.send(msg)
        return True
    except Exception:
        log.exception("Failed to send email to %s", to)
        return False


def send_welcome_email(user) -> bool:
    subject = "Welcome to TeamSpace"
    body = (
        f"Hi {user.name},\n\n"
        "Your TeamSpace account has been created. You can now log in, "
        "create sites, and start sharing files with your team.\n\n"
        "— TeamSpace"
    )
    return send_email(user.email, subject, body)


def send_invite_email(
    to_email: str,
    *,
    site_name: str,
    role: str,
    inviter_name: str,
    file_name: str | None = None,
    attachment_path: str | None = None,
) -> bool:
    subject = f"You've been added to {site_name} on TeamSpace"
    if file_name:
        body = (
            f"Hi,\n\n"
            f"{inviter_name} gave you {role} access to \"{file_name}\" "
            f"in the {site_name} site on TeamSpace.\n\n"
            + ("The file is attached.\n\n" if attachment_path else "Log in to TeamSpace to view it.\n\n")
            + "— TeamSpace"
        )
    else:
        body = (
            f"Hi,\n\n"
            f"{inviter_name} added you to the {site_name} site on TeamSpace "
            f"as {role}.\n\n"
            "Log in to TeamSpace to check it out.\n\n"
            "— TeamSpace"
        )
    return send_email(
        to_email, subject, body,
        attachment_path=attachment_path, attachment_name=file_name,
    )
