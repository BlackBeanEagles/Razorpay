"""Bank-registered mandate top-ups: a customer can optionally register a real, recurring
Razorpay Subscription (billed monthly, authorized via UPI AutoPay/eMandate/card at Razorpay's own
real Checkout) that refreshes one of their Guardrail mandates every period. This is deliberately
scoped to what a Subscription actually is -- a fixed-amount, fixed-schedule recurring charge, real
and bank-authorized -- not a claim that Guardrail's ad-hoc, merchant-triggered spend drawdown
itself is bank-enforced (that would need Razorpay's Recurring Payments API, which is gated behind
a support-activated on-demand request, not self-serve). What IS real here: every period, the
customer's own bank/UPI app is asked to independently confirm the charge before the allowance
refreshes -- if this app's server had a bug, the bank still won't authorize more than what the
customer registered.

Flow: create_allowance_subscription() makes a real Plan + Subscription (guardrail/razorpay_rest.py)
and returns Razorpay's own hosted checkout short_url for the customer to complete authorization.
Once authorized, Razorpay bills that plan every period on its own; a real, signature-verified
subscription.charged webhook (api/routes/webhooks.py) is what actually triggers the refresh here
-- handle_subscription_charged() calls guardrail.renew_mandate() with the real current_end from
the webhook payload, so the mandate's new window matches the actual, bank-confirmed billing cycle
exactly, not a guessed offset.
"""
import json
import os
import sys
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from guardrail import guardrail, razorpay_rest
from audit.audit_log import log_event

_LOCK = threading.Lock()
SUBSCRIPTIONS_PATH = os.path.join(os.path.dirname(__file__), "allowance_subscriptions.json")

PERIOD = "monthly"
INTERVAL = 1
DEFAULT_TOTAL_COUNT = 12  # a year's worth of cycles -- Razorpay requires a finite total_count


def _load() -> dict:
    if not os.path.exists(SUBSCRIPTIONS_PATH):
        return {}
    with open(SUBSCRIPTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    tmp_path = f"{SUBSCRIPTIONS_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, SUBSCRIPTIONS_PATH)


def list_allowance_subscriptions(customer_id: str = None) -> list:
    records = list(_load().values())
    if customer_id is not None:
        records = [r for r in records if r["customer_id"] == customer_id]
    return sorted(records, key=lambda r: r["created_at"], reverse=True)


def get_allowance_subscription(subscription_id: str) -> dict | None:
    return _load().get(subscription_id)


def get_allowance_subscription_for_mandate(mandate_id: str, customer_id: str) -> dict | None:
    for record in _load().values():
        if record["mandate_id"] == mandate_id and record["customer_id"] == customer_id and record["status"] != "cancelled":
            return record
    return None


def create_allowance_subscription(mandate_id: str, customer_id: str, amount_inr: float,
                                   total_count: int = DEFAULT_TOTAL_COUNT) -> dict:
    """Real REST calls -- creates a genuine test-mode Plan + Subscription. Returns the local
    record, including Razorpay's own real short_url the customer must open to complete
    authorization; the subscription does nothing (no charge, no bank registration) until they do."""
    mandate_state = guardrail.get_mandate_state(mandate_id, requesting_customer_id=customer_id)
    if mandate_state is None:
        return {"status": "error", "detail": f"Unknown mandate_id {mandate_id!r}, or it does not belong to this customer."}
    if get_allowance_subscription_for_mandate(mandate_id, customer_id) is not None:
        return {"status": "error", "detail": "This mandate already has an active or pending auto-refresh subscription."}

    plan = razorpay_rest.create_plan(
        amount_inr, PERIOD, INTERVAL,
        name=f"TechBazaar AI-agent allowance refresh -- {mandate_id}",
        description="Monthly top-up of this mandate's AI-agent spend allowance, authorized via UPI AutoPay/eMandate.",
    )
    subscription = razorpay_rest.create_subscription(
        plan["id"], total_count,
        notes={"mandate_id": mandate_id, "customer_id": customer_id, "purpose": "guardrail_allowance_refresh"},
    )

    record = {
        "subscription_id": subscription["id"], "plan_id": plan["id"], "mandate_id": mandate_id,
        "customer_id": customer_id, "amount_inr": amount_inr, "total_count": total_count,
        "status": subscription.get("status", "created"), "short_url": subscription.get("short_url"),
        "last_paid_count": 0, "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(), "last_renewed_at": None,
    }
    with _LOCK:
        data = _load()
        data[record["subscription_id"]] = record
        _save(data)

    log_event("subscriptions", "allowance_subscription_created",
              {"mandate_id": mandate_id, "customer_id": customer_id, "amount_inr": amount_inr},
              {"subscription_id": record["subscription_id"], "status": record["status"]}, "ok")
    return record


def _update(subscription_id: str, **fields) -> dict | None:
    with _LOCK:
        data = _load()
        record = data.get(subscription_id)
        if record is None:
            return None
        record.update(fields)
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save(data)
        return record


def update_subscription_status(subscription_id: str, new_status: str) -> dict | None:
    """Keeps the local record in sync with Razorpay's real subscription.{authenticated,activated,
    pending,halted,cancelled,paused,resumed,completed} webhooks -- Razorpay is the source of
    truth for the subscription's real state; this just mirrors it onto the dashboard/storefront."""
    updated = _update(subscription_id, status=new_status)
    if updated is not None:
        log_event("subscriptions", "allowance_subscription_status_changed", {"subscription_id": subscription_id}, {"status": new_status}, "ok")
    return updated


def handle_subscription_charged(subscription_id: str, paid_count: int, current_end) -> dict:
    """The actual refresh trigger: a real subscription.charged webhook means Razorpay's own
    banking rail just confirmed this period's charge succeeded. Idempotent against webhook
    redelivery via paid_count -- Razorpay increments this by exactly 1 per real cycle, so a
    redelivered event for a cycle already processed is a safe no-op, not a double top-up."""
    record = _load().get(subscription_id)
    if record is None:
        return {"status": "ignored", "detail": f"No local allowance-subscription record for {subscription_id!r}."}
    if paid_count <= record["last_paid_count"]:
        return {"status": "already_processed", "detail": f"paid_count {paid_count} already applied (last processed: {record['last_paid_count']})."}

    new_expires_at = float(current_end) if current_end is not None else None
    if new_expires_at is None:
        return {"status": "error", "detail": "Webhook payload had no current_end to renew the mandate against."}

    mandate_state = guardrail.renew_mandate(record["mandate_id"], new_expires_at)
    if mandate_state is None:
        # The mandate was deleted/rotated out from under an active subscription -- still mark
        # this cycle processed so a webhook redelivery doesn't keep retrying a lost cause, but
        # flag it loudly; the subscription is still real money moving on a mandate that no
        # longer exists to receive the top-up, and needs a human to look at it.
        _update(subscription_id, last_paid_count=paid_count, status="mandate_missing")
        log_event("subscriptions", "allowance_refresh_failed",
                  {"subscription_id": subscription_id, "mandate_id": record["mandate_id"], "paid_count": paid_count},
                  {"detail": "mandate no longer exists"}, "flagged")
        return {"status": "mandate_missing", "detail": f"Mandate {record['mandate_id']!r} no longer exists -- charge succeeded but could not be applied."}

    _update(subscription_id, last_paid_count=paid_count, status="active",
            last_renewed_at=datetime.now(timezone.utc).isoformat())
    log_event("subscriptions", "allowance_refreshed",
              {"subscription_id": subscription_id, "mandate_id": record["mandate_id"], "paid_count": paid_count},
              {"new_max_amount_inr": mandate_state["max_amount_inr"], "new_expires_at": mandate_state["expires_at"]}, "ok")
    return {"status": "renewed", "mandate_state": mandate_state}
