"""POST /api/webhooks/razorpay -- closes a real gap in this project's own design: Guardrail
previously only learned a real Checkout payment succeeded because the browser itself called
POST /api/purchase/confirm after Checkout closed. If that tab closed, crashed, or lost
connectivity right after paying, the payment was genuinely captured by Razorpay but Guardrail's
ledger would never know -- the money moved and nothing in this system recorded it.

Razorpay's webhooks (https://razorpay.com/docs/webhooks/) exist specifically for this: a
server-to-server push the moment payment.captured actually happens, independent of whether any
browser is still open. This handler is not a shortcut around confirm_purchase's own independent
verification -- it calls the exact same function the frontend path calls, which re-asks Razorpay
what was actually captured before ever touching mandate spend. The webhook is only what
*triggers* that call in a case the frontend path can't cover; it is never trusted on its own
say-so.

Also handles the payment.dispute.* events (see disputes/dispute_response.py): a real chargeback
notification triggers an AI-drafted evidence response built from this system's own audit trail,
saved as a real draft on Razorpay's side -- never auto-submitted, that stays a deliberate admin
click on the dashboard.

Not accessible from the public internet without a real HTTPS tunnel to this local server (ngrok,
Razorpay's own CLI forwarding, or a real deployment) -- see README.md for how to actually wire
this up to a live Razorpay Dashboard webhook subscription and test it end to end.
"""
import hashlib
import hmac
import json
import os
import sys

from fastapi import APIRouter, Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from guardrail import guardrail
from audit.audit_log import log_event
from disputes import dispute_response

router = APIRouter()

RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
WEBHOOK_VERIFICATION_AVAILABLE = bool(RAZORPAY_WEBHOOK_SECRET)

# Event types this handler acts on -- payment.captured is the direct, unambiguous "money moved"
# signal. order.paid fires for the same underlying event and would just be redundant work.
_PAYMENT_EVENTS = {"payment.captured"}

_DISPUTE_CREATED_EVENT = "payment.dispute.created"
# The rest of the dispute lifecycle -- just mirrored onto the local draft record so the dashboard
# reflects Razorpay's own real outcome; nothing here acts on these, they're status-sync only.
_DISPUTE_STATUS_EVENTS = {
    "payment.dispute.won": "won", "payment.dispute.lost": "lost", "payment.dispute.closed": "closed",
    "payment.dispute.under_review": "under_review", "payment.dispute.action_required": "action_required",
}

_HANDLED_EVENTS = _PAYMENT_EVENTS | {_DISPUTE_CREATED_EVENT} | set(_DISPUTE_STATUS_EVENTS)


def _verify_signature(raw_body: bytes, signature: str) -> bool:
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    # constant-time compare -- same reasoning as every password/token comparison elsewhere in
    # this codebase (api/customer_auth.py, api/auth.py): a naive == leaks how many leading bytes
    # matched via response-time differences, letting a signature be forged one byte at a time.
    return hmac.compare_digest(expected, signature)


@router.post("/api/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    raw_body = await request.body()

    if not WEBHOOK_VERIFICATION_AVAILABLE:
        # Never process an unverified webhook -- without a configured secret there is no way to
        # tell a real Razorpay event from anyone on the internet POSTing a fake "payment
        # succeeded" straight to this endpoint. Reported honestly rather than silently trusting
        # the payload, which would be a genuine, exploitable hole.
        log_event("guardrail", "webhook_received", {}, {"detail": "RAZORPAY_WEBHOOK_SECRET not configured"}, "blocked")
        return {"ok": False, "detail": "Webhook signature verification not configured -- event ignored."}

    signature = request.headers.get("x-razorpay-signature", "")
    if not signature or not _verify_signature(raw_body, signature):
        log_event("guardrail", "webhook_received", {}, {"detail": "signature verification failed"}, "blocked")
        # 200, not 401/403 -- returning an error status makes Razorpay retry the same (still
        # invalid) request repeatedly; a forged or misconfigured request isn't going to become
        # valid on retry, so there's nothing productive a retry accomplishes here.
        return {"ok": False, "detail": "Signature verification failed."}

    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        return {"ok": False, "detail": "Invalid JSON payload."}

    event_type = event.get("event")
    if event_type not in _HANDLED_EVENTS:
        return {"ok": True, "detail": f"Event {event_type!r} not handled, ignored."}

    if event_type in _DISPUTE_STATUS_EVENTS:
        return _handle_dispute_status_event(event_type, event)
    if event_type == _DISPUTE_CREATED_EVENT:
        return _handle_dispute_created(event)
    return _handle_payment_captured(event_type, event)


def _handle_payment_captured(event_type: str, event: dict) -> dict:
    payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
    razorpay_order_id = payment_entity.get("order_id")
    if not razorpay_order_id:
        log_event("guardrail", "webhook_received", {"event": event_type}, {"detail": "no order_id in payload"}, "blocked")
        return {"ok": False, "detail": "No order_id in payment payload."}

    pending = guardrail.get_pending_purchase(razorpay_order_id)
    if pending is None:
        # Nothing Guardrail is tracking for this order -- either it was never initiated through
        # initiate_purchase (e.g. a mock-mode/automated purchase, which never creates a real
        # order in the first place), or it was already confirmed and cleared earlier (a webhook
        # redelivery, which Razorpay does on its own retry schedule). Either way, honest no-op,
        # not an error -- Razorpay expects a 2xx or it keeps retrying.
        log_event("guardrail", "webhook_received",
                  {"event": event_type, "razorpay_order_id": razorpay_order_id},
                  {"detail": "no pending purchase on file -- already resolved or not ours"}, "ok")
        return {"ok": True, "detail": "No pending purchase on file for this order."}

    log_event("guardrail", "webhook_received",
              {"event": event_type, "razorpay_order_id": razorpay_order_id, "requesting_customer_id": pending["requesting_customer_id"]},
              {"detail": "confirming via webhook -- independent of whether the browser ever calls /api/purchase/confirm"}, "ok")

    result = guardrail.confirm_purchase(
        pending["mandate_id"], pending["product_id"], pending["amount_inr"], razorpay_order_id,
        requesting_customer_id=pending["requesting_customer_id"],
    )
    return {"ok": True, "confirm_result": result}


def _handle_dispute_created(event: dict) -> dict:
    payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
    dispute_entity = event.get("payload", {}).get("dispute", {}).get("entity", {})
    dispute_id = dispute_entity.get("id")
    if not dispute_id:
        log_event("disputes", "webhook_received", {"event": _DISPUTE_CREATED_EVENT}, {"detail": "no dispute id in payload"}, "blocked")
        return {"ok": False, "detail": "No dispute id in payload."}

    record = dispute_response.record_dispute_created(
        dispute_id=dispute_id,
        payment_id=dispute_entity.get("payment_id") or payment_entity.get("id"),
        razorpay_order_id=payment_entity.get("order_id"),
        amount_inr=(dispute_entity.get("amount") or 0) / 100,
        reason_code=dispute_entity.get("reason_code"),
        respond_by=dispute_entity.get("respond_by"),
    )
    return {"ok": True, "dispute_draft": record}


def _handle_dispute_status_event(event_type: str, event: dict) -> dict:
    dispute_entity = event.get("payload", {}).get("dispute", {}).get("entity", {})
    dispute_id = dispute_entity.get("id")
    if not dispute_id:
        return {"ok": False, "detail": "No dispute id in payload."}

    new_status = _DISPUTE_STATUS_EVENTS[event_type]
    updated = dispute_response.update_dispute_status(dispute_id, new_status)
    if updated is None:
        return {"ok": True, "detail": f"No local draft on file for dispute {dispute_id!r} -- status change ignored."}
    return {"ok": True, "detail": f"Dispute {dispute_id} status updated to {new_status!r}."}
