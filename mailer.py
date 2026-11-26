"""Send email through an HTTPS email API (Render's free plan blocks normal SMTP).
Works with Resend or Brevo. If nothing is configured, enabled() is False and the site simply skips emails."""
import logging

import requests

from config import MAIL_API_KEY, MAIL_FROM, MAIL_FROM_NAME, MAIL_PROVIDER

log = logging.getLogger(__name__)


def enabled() -> bool:
    return MAIL_PROVIDER in ("resend", "brevo") and bool(MAIL_API_KEY and MAIL_FROM)


def send(to: str, subject: str, html: str) -> bool:
    if not enabled():
        return False
    try:
        if MAIL_PROVIDER == "resend":
            r = requests.post("https://api.resend.com/emails", timeout=15,
                              headers={"Authorization": f"Bearer {MAIL_API_KEY}"},
                              json={"from": f"{MAIL_FROM_NAME} <{MAIL_FROM}>", "to": [to], "subject": subject, "html": html})
        else:
            r = requests.post("https://api.brevo.com/v3/smtp/email", timeout=15,
                              headers={"api-key": MAIL_API_KEY},
                              json={"sender": {"name": MAIL_FROM_NAME, "email": MAIL_FROM}, "to": [{"email": to}],
                                    "subject": subject, "htmlContent": html})
        ok = r.status_code in (200, 201, 202)
        if not ok:
            log.warning("email send failed: provider=%s status=%s", MAIL_PROVIDER, r.status_code)
        return ok
    except Exception as exc:
        log.warning("email send error: %s", exc.__class__.__name__)
        return False
