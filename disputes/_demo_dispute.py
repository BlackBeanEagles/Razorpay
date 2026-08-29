"""Live demo driver: sends a REAL, HMAC-signed payment.dispute.created webhook over an actual
HTTP request to a running TechBazaar server -- exactly the way Razorpay itself would deliver it,
not a direct function call and not a mock. This is the part of the disputes feature that's
actually demoable in a recording.

What this script cannot do: Razorpay has no self-serve "create a test dispute" endpoint (disputes
are bank/issuer-initiated), so there is no way to trigger a genuinely real dispute on demand.
What IS real here: the HTTP round trip, the signature verification, the real payment_id looked
up from Razorpay for the order being disputed, and -- if a genuinely captured order is given --
the evidence drafted from this system's own real audit trail for that exact order.

Usage:
    python -m uvicorn api.server:app --port 8000          # in one terminal
    python disputes/_demo_dispute.py [razorpay_order_id]  # in another

razorpay_order_id should be a real order from guardrail/ledger.json -- i.e. a purchase actually
completed through the storefront's real Checkout flow (pay with Razorpay's published test-mode
card, then let confirm_purchase clear it -- see README.md's "Run the app" section). Without an
argument, this uses the most recent real "success" entry already in the ledger.
"""
import hashlib
import hmac
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from guardrail import guardrail, razorpay_rest

SERVER_URL = os.environ.get("DEMO_SERVER_URL", "http://localhost:8000")
WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")


def _sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _find_order_id(explicit: str = None) -> str:
    if explicit:
        return explicit
    successes = [e for e in guardrail.load_ledger() if e.get("status") == "success" and e.get("razorpay_order_id")]
    if not successes:
        raise SystemExit(
            "No real 'success' purchase found in guardrail/ledger.json to dispute against.\n"
            "Complete one real Checkout through the storefront first -- open the store, buy "
            "something, and pay with Razorpay's published test-mode card -- then re-run this "
            "script. See README.md's 'Run the app' section."
        )
    latest = max(successes, key=lambda e: e.get("timestamp", ""))
    return latest["razorpay_order_id"]


def main():
    if not WEBHOOK_SECRET:
        raise SystemExit("RAZORPAY_WEBHOOK_SECRET is not set in .env -- see .env.example.")

    order_id = _find_order_id(sys.argv[1] if len(sys.argv) > 1 else None)
    entry = next((e for e in guardrail.load_ledger() if e.get("razorpay_order_id") == order_id), None)
    if entry is None:
        raise SystemExit(f"No ledger entry found for order {order_id!r}.")

    amount_paise = round(entry["expected_amount_inr"] * 100)
    try:
        real_payment_id = razorpay_rest.captured_payment_id(order_id)
    except requests.RequestException:
        real_payment_id = None
    payment_id = real_payment_id or f"pay_demo_{order_id}"

    dispute_id = f"disp_demo_{int(time.time())}"
    body = json.dumps({
        "entity": "event", "event": "payment.dispute.created", "contains": ["payment", "dispute"],
        "payload": {
            "payment": {"entity": {"id": payment_id, "order_id": order_id, "amount": amount_paise, "status": "captured"}},
            "dispute": {"entity": {
                "id": dispute_id, "payment_id": payment_id, "amount": amount_paise,
                "reason_code": "processed_invalid_expired_card", "respond_by": int(time.time()) + 7 * 86400, "status": "open",
            }},
        },
        "created_at": int(time.time()),
    }).encode()

    print(f"POSTing a real, signed payment.dispute.created webhook for real order {order_id!r} to {SERVER_URL}...")
    resp = requests.post(
        f"{SERVER_URL}/api/webhooks/razorpay", data=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": _sign(body)},
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"Server response: ok={result.get('ok')}")

    draft = result.get("dispute_draft")
    if draft:
        print(f"\nDispute {draft['dispute_id']} drafted (evidence_found={draft['evidence_found']}):\n")
        print(draft["summary"])
        print(f"\nReal Razorpay draft call result: {draft['razorpay_draft']}")
        print("\nOpen the dashboard's 'Disputes & chargebacks' panel to review and submit or accept it.")
    else:
        print(result)


if __name__ == "__main__":
    main()
