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


def create_or_get_customer(name: str, email: str) -> dict:
    """Real Razorpay Customer record for a TechBazaar account -- fail_existing="0" makes this
    idempotent (verified against the real API: calling it twice with the same email returns the
    exact same customer id, not an error), so callers can call this on every checkout without
    tracking "have we already created this" themselves. Purpose: pass this id into Checkout so
    Razorpay's own widget can offer to save a card/UPI method and recognize the same person on a
    later purchase -- the actual card data is tokenized and held by Razorpay, never by us."""
    resp = requests.post(
        f"{_BASE_URL}/customers",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        json={"name": name, "email": email, "fail_existing": "0"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


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


def fetch_dispute(dispute_id: str) -> dict:
    resp = requests.get(
        f"{_BASE_URL}/disputes/{dispute_id}",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def contest_dispute(dispute_id: str, summary: str, action: str, amount_inr: float = None) -> dict:
    """Drafts or submits evidence against a real dispute via Razorpay's Disputes API
    (PATCH /v1/disputes/:id/contest). action="draft" saves the evidence on Razorpay's own system
    WITHOUT submitting it -- Razorpay's own documented behavior, not something this module
    enforces -- so a draft can be reviewed and edited before anyone commits to it. Only
    action="submit" actually sends it to the customer's bank for review."""
    body = {"summary": summary, "action": action}
    if amount_inr is not None:
        body["amount"] = round(amount_inr * 100)
    resp = requests.patch(
        f"{_BASE_URL}/disputes/{dispute_id}/contest",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def accept_dispute(dispute_id: str) -> dict:
    """Accepts a dispute (POST /v1/disputes/:id/accept) -- the customer is refunded. Used only
    when a human admin has decided the dispute is legitimate, never automatically."""
    resp = requests.post(
        f"{_BASE_URL}/disputes/{dispute_id}/accept",
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
