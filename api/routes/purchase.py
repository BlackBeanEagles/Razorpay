"""POST /api/purchase/initiate and POST /api/purchase/confirm -- the human-verified real
Razorpay checkout flow (see guardrail.initiate_purchase / confirm_purchase for the mechanics).
Separate from POST /api/chat's fully-automated mock purchase path used by the LLM/deterministic
agent when real Checkout isn't in the loop."""
import os
import sys

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from guardrail import guardrail
from growth.upsell import suggest_complementary
from api.customer_auth import require_customer

router = APIRouter()

# Only genuinely expected failure modes get turned into a clean 502 -- a real Razorpay API
# error/timeout, or the mandate store lock timing out under contention. Anything else (a
# KeyError/TypeError from an actual bug) is left to propagate as an unhandled 500 rather than
# being mislabeled "Razorpay is down", which would hide a real defect from logs/debugging.
_EXPECTED_UPSTREAM_ERRORS = (requests.RequestException, TimeoutError)


class InitiateRequest(BaseModel):
    product_id: str
    amount_inr: int
    mandate_id: str


class ConfirmRequest(BaseModel):
    product_id: str
    amount_inr: int
    mandate_id: str
    razorpay_order_id: str


@router.post("/api/purchase/initiate")
def initiate(body: InitiateRequest, customer_id: str = Depends(require_customer)):
    try:
        # requesting_customer_id enforces ownership -- a customer can only initiate a real
        # purchase against a mandate they own, or a shared/demo mandate (owner None).
        token = guardrail.get_mandate_token(body.mandate_id, requesting_customer_id=customer_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown mandate_id: {body.mandate_id}")
    try:
        return guardrail.initiate_purchase(token, body.product_id, body.amount_inr, requesting_customer_id=customer_id)
    except _EXPECTED_UPSTREAM_ERRORS as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Razorpay to create the order: {e}")


@router.post("/api/purchase/confirm")
def confirm(body: ConfirmRequest, customer_id: str = Depends(require_customer)):
    try:
        return guardrail.confirm_purchase(
            body.mandate_id, body.product_id, body.amount_inr, body.razorpay_order_id,
            requesting_customer_id=customer_id,
        )
    except _EXPECTED_UPSTREAM_ERRORS as e:
        raise HTTPException(status_code=502, detail=f"Could not complete verification right now: {e}")


@router.get("/api/upsell/{product_id}")
def upsell(product_id: str, customer_id: str = Depends(require_customer)):
    # The real, human-verified Checkout flow (initiate_purchase/confirm_purchase) pauses the
    # chat agent's own tool-calling loop at "checkout_required" -- payment isn't done yet, so
    # the LLM never gets a turn to call get_upsell_suggestions itself once it actually completes.
    # This is the frontend's own hook for that exact moment (see app.js's addCheckoutCard),
    # called only after confirm_purchase independently verifies a real "success".
    return suggest_complementary(product_id, requesting_customer_id=customer_id)
