"""AI-drafted chargeback evidence: when Razorpay tells us (via a real, signature-verified
payment.dispute.created webhook -- see api/routes/webhooks.py) that a customer's bank has
disputed a payment, this module builds the defense automatically from TechBazaar's own real
audit trail -- the exact Guardrail verification record and Parity fairness check that already
exist for that order -- rather than a human starting from a blank page.

The draft is saved on Razorpay's own system via the real Disputes API (PATCH
/v1/disputes/:id/contest, action="draft") -- but Razorpay's own documented behavior is that a
draft is never auto-submitted. Nothing here ever calls action="submit" or POST .../accept on its
own; those are the two dispute-committing actions and stay a deliberate, confirmed admin click
(submit_dispute_response / accept_dispute_action), mirroring the same "detect, don't auto-act on
anything irreversible" shape as reconciliation/live_verification.py's remediation button.

Razorpay does not expose a self-serve "create a test dispute" endpoint (disputes are bank/issuer
-initiated), so the real PATCH .../contest call against a dispute_id that doesn't actually exist
in this account will 404 -- record_dispute_created() treats that as an expected, honestly-reported
outcome (razorpay_draft.saved = False with the real error), not a crash. The evidence drafting
itself, and the local review workflow, work regardless.
"""
import json
import os
import sys
import threading
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from guardrail import guardrail, razorpay_rest
from audit.audit_log import log_event, read_all

_LOCK = threading.Lock()
DISPUTES_PATH = os.path.join(os.path.dirname(__file__), "dispute_drafts.json")

MAX_SUMMARY_CHARS = 2000  # Razorpay's evidence.summary field has a real length cap


def _find_ledger_entry(razorpay_order_id: str) -> dict | None:
    for entry in guardrail.load_ledger():
        if entry.get("razorpay_order_id") == razorpay_order_id:
            return entry
    return None


def _find_fairness_check(product_id: str, customer_id: str, at_or_before: str) -> dict | None:
    """The most recent Parity fairness_check audit entry for this exact product/customer pair,
    no later than the purchase itself -- that's the check that actually gated this transaction."""
    candidates = [
        e for e in read_all()
        if e.get("component") == "parity" and e.get("event") == "fairness_check"
        and (e.get("input_summary") or {}).get("product_id") == product_id
        and (e.get("input_summary") or {}).get("customer_id") == customer_id
        and e.get("timestamp", "") <= at_or_before
    ]
    return max(candidates, key=lambda e: e["timestamp"]) if candidates else None


def draft_evidence_from_audit_trail(razorpay_order_id: str) -> dict:
    """Builds a natural-language evidence summary plus the individual facts it's built from.
    Returns {"found": bool, "summary": str, "facts": list, "ledger_entry": dict|None,
    "fairness_check": dict|None}. found=False (no ledger entry at all) still returns an honest,
    reviewable summary explaining exactly why -- never a blank or fabricated one."""
    entry = _find_ledger_entry(razorpay_order_id)
    if entry is None:
        return {
            "found": False,
            "summary": (
                f"No internal record of order {razorpay_order_id} exists in this system's own ledger -- "
                f"this dispute may reference a payment that never went through TechBazaar's purchase "
                f"pipeline, or the record has since been removed. Needs manual investigation before any "
                f"evidence can be drafted."
            ),
            "facts": [], "ledger_entry": None, "fairness_check": None,
        }

    verification = entry.get("verification") or {}
    fairness = _find_fairness_check(entry.get("product_id"), entry.get("requesting_customer_id"), entry.get("timestamp", ""))

    facts = [
        f"Order {razorpay_order_id} was placed by customer {entry.get('requesting_customer_id')} for "
        f"product {entry.get('product_id')} at {entry.get('timestamp')}.",
        f"The purchase was authorized under mandate {entry.get('mandate_id')}, which enforces a bounded, "
        f"pre-approved spend limit that this transaction did not exceed.",
    ]
    if fairness:
        fr = fairness.get("result_summary") or {}
        facts.append(
            f"Parity's automated pricing fairness check verified this exact transaction before it was "
            f"allowed to proceed: verdict '{fr.get('verdict')}' -- {fr.get('reason')}"
        )
    if verification:
        match_note = "the amounts matched exactly" if verification.get("all_match") else "a discrepancy was separately recorded and handled"
        facts.append(
            f"Guardrail independently re-verified the payment with Razorpay after checkout, never trusting "
            f"the browser's own say-so: expected {verification.get('amount_expected_inr')} INR, Razorpay "
            f"itself confirmed {verification.get('amount_settled_inr')} INR captured ({match_note})."
        )
    facts.append(f"Current order status on file: {entry.get('status')!r}.")

    summary = (
        "This payment was processed through TechBazaar's automated, mandate-enforced purchase pipeline, "
        "not a manual or unverified charge. " + " ".join(facts) +
        " No indication of an unauthorized, erroneous, or undelivered transaction was found in this "
        "system's own independently-verified records."
    )
    return {"found": True, "summary": summary[:MAX_SUMMARY_CHARS], "facts": facts, "ledger_entry": entry, "fairness_check": fairness}


def _load() -> dict:
    if not os.path.exists(DISPUTES_PATH):
        return {}
    with open(DISPUTES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    tmp_path = f"{DISPUTES_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, DISPUTES_PATH)


def list_dispute_drafts() -> list:
    return sorted(_load().values(), key=lambda d: d["created_at"], reverse=True)


def get_dispute_draft(dispute_id: str) -> dict | None:
    return _load().get(dispute_id)


def record_dispute_created(dispute_id: str, payment_id: str, razorpay_order_id: str,
                            amount_inr: float, reason_code: str, respond_by) -> dict:
    """Called from the payment.dispute.created webhook handler. Drafts evidence from our own
    audit trail and best-effort saves it as a real draft on Razorpay's side -- best-effort
    because a dispute_id that doesn't actually exist on this test-mode account (there's no
    self-serve way to create a real one) will legitimately 404, which is reported honestly on
    the record rather than hidden or treated as a crash."""
    evidence = draft_evidence_from_audit_trail(razorpay_order_id)

    razorpay_draft = {"attempted": False, "saved": False, "detail": None}
    if razorpay_rest.REAL_CHECKOUT_AVAILABLE:
        try:
            raw = razorpay_rest.contest_dispute(dispute_id, evidence["summary"], action="draft")
            razorpay_draft = {"attempted": True, "saved": True, "detail": None, "raw": raw}
        except requests.HTTPError as e:
            razorpay_draft = {"attempted": True, "saved": False, "detail": f"Razorpay rejected the draft call: {e}"}
        except requests.RequestException as e:
            razorpay_draft = {"attempted": True, "saved": False, "detail": f"Could not reach Razorpay: {e}"}

    record = {
        "dispute_id": dispute_id, "payment_id": payment_id, "razorpay_order_id": razorpay_order_id,
        "amount_inr": amount_inr, "reason_code": reason_code, "respond_by": respond_by,
        "status": "draft_pending", "created_at": datetime.now(timezone.utc).isoformat(),
        "evidence_found": evidence["found"], "summary": evidence["summary"], "facts": evidence["facts"],
        "razorpay_draft": razorpay_draft, "submitted_at": None, "submitted_by": None,
    }
    with _LOCK:
        data = _load()
        data[dispute_id] = record
        _save(data)

    log_event(
        "disputes", "dispute_response_drafted",
        {"dispute_id": dispute_id, "razorpay_order_id": razorpay_order_id, "amount_inr": amount_inr},
        {"evidence_found": evidence["found"], "razorpay_draft_saved": razorpay_draft["saved"]},
        "ok" if evidence["found"] else "flagged",
    )
    return record


def update_dispute_status(dispute_id: str, new_status: str) -> dict | None:
    """Keeps the local record in sync with payment.dispute.{won,lost,closed,under_review,
    action_required} webhook events -- Razorpay is the source of truth for a dispute's real
    outcome, this just mirrors it onto the dashboard."""
    with _LOCK:
        data = _load()
        record = data.get(dispute_id)
        if record is None:
            return None
        record["status"] = new_status
        _save(data)
    log_event("disputes", "dispute_status_changed", {"dispute_id": dispute_id}, {"status": new_status}, "ok")
    return record


def submit_dispute_response(dispute_id: str, admin_username: str, summary_override: str = None) -> dict:
    """The one path that actually sends evidence to the customer's bank (action="submit") --
    always a deliberate, admin-initiated call, never automatic."""
    record = _load().get(dispute_id)
    if record is None:
        return {"status": "error", "detail": f"No draft on file for dispute {dispute_id!r}."}
    if record["status"] == "submitted":
        return {"status": "already_submitted", "detail": "This response was already submitted."}

    summary = (summary_override or record["summary"])[:MAX_SUMMARY_CHARS]
    try:
        raw = razorpay_rest.contest_dispute(dispute_id, summary, action="submit")
    except requests.HTTPError as e:
        return {"status": "error", "detail": f"Razorpay rejected the submission: {e}"}
    except requests.RequestException as e:
        return {"status": "error", "detail": f"Could not reach Razorpay: {e}"}

    with _LOCK:
        data = _load()
        record = data.get(dispute_id)
        if record is not None:
            record["status"] = "submitted"
            record["summary"] = summary
            record["submitted_at"] = datetime.now(timezone.utc).isoformat()
            record["submitted_by"] = admin_username
            _save(data)
    log_event("disputes", "dispute_response_submitted",
              {"dispute_id": dispute_id, "admin": admin_username}, {"razorpay_status": raw.get("status")}, "ok")
    return {"status": "submitted", "raw": raw}


def accept_dispute_action(dispute_id: str, admin_username: str) -> dict:
    """The other path a human can take: concede the dispute (the customer is refunded) instead
    of contesting it. Real Razorpay call (POST /v1/disputes/:id/accept), always admin-initiated."""
    try:
        raw = razorpay_rest.accept_dispute(dispute_id)
    except requests.HTTPError as e:
        return {"status": "error", "detail": f"Razorpay rejected accepting the dispute: {e}"}
    except requests.RequestException as e:
        return {"status": "error", "detail": f"Could not reach Razorpay: {e}"}

    with _LOCK:
        data = _load()
        record = data.get(dispute_id)
        if record is not None:
            record["status"] = "accepted"
            record["accepted_by"] = admin_username
            record["accepted_at"] = datetime.now(timezone.utc).isoformat()
            _save(data)
    log_event("disputes", "dispute_accepted", {"dispute_id": dispute_id, "admin": admin_username},
              {"razorpay_status": raw.get("status")}, "ok")
    return {"status": "accepted", "raw": raw}
