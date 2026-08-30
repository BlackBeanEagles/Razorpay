"""POST/GET /api/allowance-subscriptions -- customer-facing: register a real, bank-authorized
monthly top-up for one of the customer's own mandates (subscriptions/allowance_subscription.py).
GET /api/admin/allowance-subscriptions -- admin visibility into every customer's real
subscriptions, same reasoning as audit.py."""
import os
import sys

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from subscriptions.allowance_subscription import (
    create_allowance_subscription, list_allowance_subscriptions, get_allowance_subscription_for_mandate,
    get_allowance_subscription, send_recovery_link,
)
from api.customer_auth import require_customer
from api.auth import require_admin

router = APIRouter()


class CreateAllowanceSubscriptionRequest(BaseModel):
    mandate_id: str
    amount_inr: float
    total_count: int = 12


@router.post("/api/allowance-subscriptions")
def create(body: CreateAllowanceSubscriptionRequest, customer_id: str = Depends(require_customer)):
    result = create_allowance_subscription(body.mandate_id, customer_id, body.amount_inr, body.total_count)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


@router.get("/api/allowance-subscriptions")
def list_own(customer_id: str = Depends(require_customer)):
    return {"subscriptions": list_allowance_subscriptions(customer_id=customer_id)}


@router.get("/api/allowance-subscriptions/for-mandate/{mandate_id}")
def for_mandate(mandate_id: str, customer_id: str = Depends(require_customer)):
    return {"subscription": get_allowance_subscription_for_mandate(mandate_id, customer_id)}


@router.get("/api/admin/allowance-subscriptions")
def list_all(_admin: str = Depends(require_admin)):
    return {"subscriptions": list_allowance_subscriptions()}


@router.post("/api/admin/allowance-subscriptions/{subscription_id}/send-recovery-link")
def resend_recovery_link(subscription_id: str, _admin: str = Depends(require_admin)):
    """Manual trigger for the same real recovery-link flow that fires automatically when
    Razorpay halts a subscription -- lets an admin resend it (e.g. the customer says they never
    got the email) without waiting for another real halt."""
    if get_allowance_subscription(subscription_id) is None:
        raise HTTPException(status_code=404, detail=f"No allowance subscription {subscription_id!r}.")
    result = send_recovery_link(subscription_id)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["detail"])
    return result
