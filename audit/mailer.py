"""Real transactional email via Resend's REST API -- currently just the "we received your
issue" confirmation sent when a customer files a support request (audit/support_log.py).

Uses Resend's sandbox sender (onboarding@resend.dev), not a verified custom domain -- Resend
only actually delivers a sandbox-sender email to the address the Resend account itself is
registered with; anything else comes back as a real, reportable failure from their API. That's
surfaced honestly in the returned result, never swallowed into a fake "sent" -- a support
request must still be recorded even when the email leg fails (Resend down, unverified
recipient), so send_email() never raises.
"""
import os

import requests

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_URL = "https://api.resend.com/emails"
FROM_ADDRESS = "TechBazaar Support <onboarding@resend.dev>"
_PLACEHOLDER_KEY = "re_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"


def _is_real_key(key: str) -> bool:
    """True only for a key that looks like a genuine Resend key -- not empty, not the literal
    placeholder value from .env.example. Factored out (rather than one inline expression at
    import time, the pattern this codebase's other two "_AVAILABLE" flags used) specifically so
    this exact logic is unit-testable: this used to check only the "re_" prefix, which the
    placeholder itself also matches, so anyone who copied .env.example to .env without replacing
    RESEND_API_KEY would have EMAIL_AVAILABLE silently report "configured" and send_email() would
    go on to make a real, doomed API call to Resend with a fake key on every support request,
    instead of cleanly skipping the same way an actually-absent key does. That gap had no test
    covering the detection logic itself (every existing test monkeypatched EMAIL_AVAILABLE
    directly) -- see test_mailer.py's tests for this function."""
    return bool(key) and key != _PLACEHOLDER_KEY and key.startswith("re_")


EMAIL_AVAILABLE = _is_real_key(RESEND_API_KEY)


def send_email(to_email: str, subject: str, body_text: str) -> dict:
    """Returns {"sent": bool, "provider_id": str|None, "detail": str}."""
    if not EMAIL_AVAILABLE:
        return {"sent": False, "provider_id": None, "detail": "Email not configured (no RESEND_API_KEY)."}
    if not to_email:
        return {"sent": False, "provider_id": None, "detail": "No email address on file for this customer."}
    try:
        resp = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": FROM_ADDRESS, "to": [to_email], "subject": subject, "text": body_text},
            timeout=15,
        )
        if not resp.ok:
            return {"sent": False, "provider_id": None, "detail": f"Resend rejected the send ({resp.status_code}): {resp.text[:300]}"}
        return {"sent": True, "provider_id": resp.json().get("id"), "detail": "Sent via Resend."}
    except requests.RequestException as e:
        return {"sent": False, "provider_id": None, "detail": f"Couldn't reach Resend: {e}"}


def support_request_confirmation(request_id: str, message: str) -> tuple:
    """(subject, body) for the "we received your issue" email -- factored out so the API route
    and any future caller (e.g. a resend-confirmation admin action) build the exact same text."""
    subject = "We've received your support request -- TechBazaar"
    body = (
        f"Hi,\n\nThanks for reaching out. We've logged your request (reference {request_id}) "
        f"and our team will follow up.\n\nWhat you told us:\n\"{message}\"\n\n"
        f"-- TechBazaar Support"
    )
    return subject, body


def support_request_resolved(request_id: str, original_message: str) -> tuple:
    """(subject, body) for the follow-up sent when admin marks a request resolved -- closes the
    loop the confirmation email opened, so a customer isn't left wondering whether anything
    happened after they reported an issue."""
    subject = "Your support request has been resolved -- TechBazaar"
    body = (
        f"Hi,\n\nGood news -- your support request (reference {request_id}) has been resolved.\n\n"
        f"What you originally told us:\n\"{original_message}\"\n\n"
        f"If this doesn't look right or the issue comes back, just reply to this email or file "
        f"a new request and reference {request_id}.\n\n-- TechBazaar Support"
    )
    return subject, body
