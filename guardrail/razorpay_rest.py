"""Thin, explicit REST wrapper for real Razorpay test-mode Orders/Payments -- used only by the
human-verified checkout flow (guardrail.initiate_purchase / confirm_purchase), never by the
fully-automated mock flow (razorpay_client.py, used by execute_purchase/batches/tests).

Deliberately separate module: nothing here auto-activates based on .env contents the way the
old razorpay_client.py did (see that file's docstring for why that was a footgun). Callers
decide explicitly when to use this -- REAL_CHECKOUT_AVAILABLE just tells them whether it's
possible to.
"""
import os

import requests
from dotenv import load_dotenv

# Loaded here explicitly rather than relying on some other module (mandate.py, the server
# entrypoint) to have already called load_dotenv() first -- REAL_CHECKOUT_AVAILABLE below is
# computed once at import time, so if this module were ever imported before .env had been
# loaded by anything else, it would silently and permanently see empty credentials.
load_dotenv()

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
_PLACEHOLDER_KEY = "rzp_test_xxxxxxxxxxxx"

REAL_CHECKOUT_AVAILABLE = bool(
    RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET
    and RAZORPAY_KEY_ID != _PLACEHOLDER_KEY
    and RAZORPAY_KEY_ID.startswith("rzp_test_")
)

_BASE_URL = "https://api.razorpay.com/v1"


def create_order(amount_inr: int, receipt: str) -> dict:
    resp = requests.post(
        f"{_BASE_URL}/orders",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        json={"amount": amount_inr * 100, "currency": "INR", "receipt": receipt},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_order_payments(razorpay_order_id: str) -> list:
    resp = requests.get(
        f"{_BASE_URL}/orders/{razorpay_order_id}/payments",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def captured_amount_inr(razorpay_order_id: str) -> float:
    """Sums up any captured payments against this order. 0 if none have been captured yet
    (e.g. the human hasn't completed Checkout, or payment is still authorized-not-captured)."""
    payments = fetch_order_payments(razorpay_order_id)
    return sum(p["amount"] for p in payments if p["status"] == "captured") / 100


def captured_payment_id(razorpay_order_id: str) -> str | None:
    """The id of this order's captured payment (refunds are issued against a payment, not an
    order). None if nothing is captured yet. Assumes at most one captured payment per order,
    which holds for this flow -- one real Checkout session per order."""
    payments = fetch_order_payments(razorpay_order_id)
    captured = [p for p in payments if p["status"] == "captured"]
    return captured[0]["id"] if captured else None


def refund_payment(payment_id: str, amount_inr: float) -> dict:
    """Issues a real (test-mode) refund for a captured payment. Used only when a payment was
    genuinely captured but Guardrail determined afterward it can't be counted against the
    mandate (e.g. a concurrent purchase against the same mandate landed first) -- the money
    must not simply stay taken."""
    resp = requests.post(
        f"{_BASE_URL}/payments/{payment_id}/refund",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        json={"amount": round(amount_inr * 100)},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
