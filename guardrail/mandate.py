"""Sign/verify HMAC-signed spending mandate tokens."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from dotenv import load_dotenv

# Ensures a consistent MANDATE_SIGNING_SECRET whether this is imported via the server (which
# also loads .env) or run standalone (batch scripts, tests) -- without this, tokens signed by
# one entry point could fail to verify in another if they picked up different secrets.
load_dotenv()

SIGNING_SECRET = os.environ.get("MANDATE_SIGNING_SECRET", "dev-only-insecure-default-secret")


def _sign(payload_b64: str) -> str:
    sig = hmac.new(SIGNING_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode()


def issue_mandate(merchant_id: str, max_amount_inr: int, expires_in_seconds: int, single_use: bool,
                   owner_customer_id: str = None) -> str:
    now = time.time()
    mandate = {
        # A millisecond timestamp alone is guessable/enumerable -- the random suffix means
        # knowing roughly when a mandate was created doesn't get you its id. This is on top
        # of, not instead of, ownership enforcement (see owner_customer_id) -- guessing the id
        # no longer matters either way, but there's no reason to leave it easy to guess too.
        "mandate_id": f"m_{int(now * 1000)}_{secrets.token_hex(4)}",
        "merchant_id": merchant_id,
        "max_amount_inr": max_amount_inr,
        "issued_at": now,
        "expires_at": now + expires_in_seconds,
        "single_use": single_use,
        "amount_spent_so_far_inr": 0,
        # None = a shared/demo mandate usable by any authenticated customer (e.g. the seed
        # mandates in data/mandates.json, used deliberately across many different customers in
        # the batch test dataset). Set to a real customer_id when a customer creates their own
        # mandate (e.g. via "set spending limit") -- only that customer can then read or spend
        # against it. See guardrail.py's ownership check in get_mandate_token/get_mandate_state/
        # initiate_purchase/execute_purchase.
        "owner_customer_id": owner_customer_id,
    }
    return encode_mandate(mandate)


def encode_mandate(mandate: dict) -> str:
    payload_b64 = base64.urlsafe_b64encode(json.dumps(mandate, sort_keys=True).encode()).decode()
    signature = _sign(payload_b64)
    return f"{payload_b64}.{signature}"


def decode_and_verify(signed_mandate_token: str) -> dict | None:
    """Returns the mandate dict if the signature is valid, else None."""
    try:
        payload_b64, signature = signed_mandate_token.rsplit(".", 1)
    except ValueError:
        return None
    expected_signature = _sign(payload_b64)
    if not hmac.compare_digest(signature, expected_signature):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
    except (ValueError, json.JSONDecodeError):
        return None
