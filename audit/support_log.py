"""Customer support requests -- lets a shopper flag "I need help with this" on a specific
blocked/flagged/failed action, and lets admin see, per feature, whether real customers are
actually hitting trouble with it (not just whether the batch proof passes). Same append-only
JSONL pattern as audit/audit_log.py, kept as its own file/module since a support request is a
customer's own input, not a system-generated audit event.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timezone

_LOCK = threading.Lock()
_LOG_PATH = os.path.join(os.path.dirname(__file__), "support_requests.jsonl")


def log_support_request(customer_id: str, component: str, related_summary: str, message: str, email_result: dict = None) -> dict:
    """component: which feature the customer needs help with (shelf/parity/guardrail/other).
    related_summary: a short, honest description of the action that prompted this (e.g. the
    exact block/flag reason) -- captured at request time so admin doesn't have to go
    cross-reference the audit log separately to see what the customer was reacting to.
    email_result: the real outcome of trying to send the "we received your issue" confirmation
    (see audit/mailer.py) -- recorded on the request itself so admin can see, per request,
    whether the customer actually got a confirmation or the send failed, rather than assuming
    silently. None means email sending wasn't attempted at all (kept distinct from "attempted
    and failed")."""
    entry = {
        "request_id": uuid.uuid4().hex[:12],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "customer_id": customer_id,
        "component": component,
        "related_summary": related_summary,
        "message": message,
        "status": "open",
        "confirmation_email": email_result,
        "resolution_email": None,  # set once admin resolves this and the follow-up send is attempted
    }
    with _LOCK:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return entry


def read_all() -> list:
    if not os.path.exists(_LOG_PATH):
        return []
    entries = []
    with open(_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _rewrite(entries: list) -> None:
    tmp_path = f"{_LOG_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    os.replace(tmp_path, _LOG_PATH)


def mark_resolved(request_id: str) -> dict | None:
    """Returns the updated entry, or None if request_id doesn't exist -- caller (the API route)
    turns None into a 404 rather than silently succeeding on a typo'd id. Returns the full entry
    (not just True/False) so the caller has the customer_id and original message on hand to send
    the "your issue was resolved" follow-up without a second lookup."""
    with _LOCK:
        entries = read_all()
        updated = None
        for e in entries:
            if e["request_id"] == request_id:
                e["status"] = "resolved"
                updated = e
                break
        if updated is not None:
            _rewrite(entries)
        return updated


def _update_field(request_id: str, field: str, value) -> bool:
    with _LOCK:
        entries = read_all()
        found = False
        for e in entries:
            if e["request_id"] == request_id:
                e[field] = value
                found = True
                break
        if found:
            _rewrite(entries)
        return found


def update_confirmation_email(request_id: str, email_result: dict) -> bool:
    """Records the real outcome of the "we received your issue" send against the request it
    belongs to -- called right after log_support_request, once the email attempt (which needs
    the customer's looked-up email address, a concern support_log itself doesn't own) has
    actually happened."""
    return _update_field(request_id, "confirmation_email", email_result)


def update_resolution_email(request_id: str, email_result: dict) -> bool:
    """Same idea as update_confirmation_email, for the follow-up sent when admin marks the
    request resolved -- kept as a separate field so the dashboard can show both "did they get
    notified we received it" and "did they get notified it's fixed" independently; one send
    failing doesn't say anything about the other."""
    return _update_field(request_id, "resolution_email", email_result)
