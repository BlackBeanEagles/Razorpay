"""Live demo driver: creates a REAL Razorpay Plan + Subscription for a mandate's monthly
allowance top-up (genuine test-mode REST calls -- visible in the Razorpay Dashboard under
Payment Products > Subscriptions), then sends a REAL, HMAC-signed subscription.charged webhook
over an actual HTTP request to a running TechBazaar server -- proving the mandate genuinely
renews from a real, signature-verified webhook, the same way it would after a customer's real
UPI AutoPay/eMandate authorization actually gets billed.

What this script cannot do: complete the real bank/UPI authorization itself -- that's Razorpay's
own hosted checkout (the short_url this script prints), which needs a human with a UPI app or
test card. What IS real here: the Plan/Subscription creation against Razorpay's live test-mode
API, the signed webhook HTTP round trip, and the mandate's spend counter genuinely resetting to 0
as a direct, observable result -- not simulated.

Usage:
    python -m uvicorn api.server:app --port 8000                       # in one terminal
    python subscriptions/_demo_allowance_subscription.py               # in another
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
from subscriptions.allowance_subscription import create_allowance_subscription

SERVER_URL = os.environ.get("DEMO_SERVER_URL", "http://localhost:8000")
WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
DEMO_CUSTOMER_ID = "demo_allowance_customer"
REFRESH_AMOUNT_INR = 5000
INITIAL_SPEND_INR = 4200  # simulated prior spend, so the reset-to-0 after renewal is visible


def _sign(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def main():
    if not WEBHOOK_SECRET:
        raise SystemExit("RAZORPAY_WEBHOOK_SECRET is not set in .env -- see .env.example.")
    if not razorpay_rest.REAL_CHECKOUT_AVAILABLE:
        raise SystemExit("Real Razorpay test-mode credentials (RAZORPAY_KEY_ID/SECRET) are not configured in .env.")

    print(f"Creating a real mandate owned by {DEMO_CUSTOMER_ID!r} (Rs.{REFRESH_AMOUNT_INR} ceiling)...")
    created = guardrail.issue_and_store_mandate(
        "techbazaar", REFRESH_AMOUNT_INR, expires_in_seconds=86400, single_use=False, owner_customer_id=DEMO_CUSTOMER_ID,
    )
    mandate_id = created["mandate_id"]
    token = guardrail.get_mandate_token(mandate_id)
    guardrail.execute_purchase(token, "p001", INITIAL_SPEND_INR, requesting_customer_id=DEMO_CUSTOMER_ID)
    before = guardrail.get_mandate_state(mandate_id)
    print(f"Mandate {mandate_id}: Rs.{before['amount_spent_so_far_inr']} spent of Rs.{before['max_amount_inr']} before renewal.")

    print("\nCreating a REAL Razorpay Plan + Subscription (test-mode REST, visible in your Dashboard)...")
    record = create_allowance_subscription(mandate_id, DEMO_CUSTOMER_ID, REFRESH_AMOUNT_INR)
    if record.get("status") == "error":
        raise SystemExit(f"Could not create the subscription: {record['detail']}")
    print(f"Subscription {record['subscription_id']} created (plan {record['plan_id']}).")
    print(f"Real Razorpay checkout to authorize it with a bank/UPI app or test card: {record['short_url']}")
    print("(Not opened automatically -- that step needs a human. This demo continues without it,")
    print(" simulating the subscription.charged webhook Razorpay would send after authorization.)")

    new_current_end = int(time.time()) + 30 * 86400
    body = json.dumps({
        "entity": "event", "event": "subscription.charged", "contains": ["subscription", "payment"],
        "payload": {"subscription": {"entity": {
            "id": record["subscription_id"], "plan_id": record["plan_id"], "customer_id": None,
            "status": "active", "paid_count": 1, "current_end": new_current_end,
        }}},
        "created_at": int(time.time()),
    }).encode()

    print(f"\nPOSTing a real, signed subscription.charged webhook to {SERVER_URL}...")
    resp = requests.post(
        f"{SERVER_URL}/api/webhooks/razorpay", data=body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": _sign(body)},
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"Server response: ok={result.get('ok')}, refresh_result={result.get('refresh_result')}")

    after = guardrail.get_mandate_state(mandate_id)
    print(f"\nMandate {mandate_id} after renewal: Rs.{after['amount_spent_so_far_inr']} spent of "
          f"Rs.{after['max_amount_inr']} (was Rs.{before['amount_spent_so_far_inr']}), now expires {after['expires_at']}.")
    print("\nOpen the storefront as this customer, or the dashboard's audit log, to see it reflected live.")


if __name__ == "__main__":
    main()
