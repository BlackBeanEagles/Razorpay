"""GET /api/reconciliation/live-check and POST /api/reconciliation/remediate-overcharge --
independent verification of live purchases against Razorpay's own current records (not just this
app's own ledger), plus a tightly-scoped, admin-triggered refund for the one exception type
that's unambiguous enough to fix automatically. See reconciliation/live_verification.py's
docstring for why only overcharge_drift is ever remediable here, never undercharge_drift.

Admin-only, same reasoning as audit.py and settlement_qa.py -- this reads real Razorpay payment
data and, for the remediate endpoint, moves real (test-mode) money."""
import os
import sys

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from reconciliation.live_verification import check_live_drift, remediate_overcharge
from api.auth import require_admin

router = APIRouter()


@router.get("/api/reconciliation/live-check")
def live_check(_admin: str = Depends(require_admin)):
    return check_live_drift()


class RemediateRequest(BaseModel):
    order_id: str


@router.post("/api/reconciliation/remediate-overcharge")
def remediate(body: RemediateRequest, admin: str = Depends(require_admin)):
    result = remediate_overcharge(body.order_id, admin_username=admin)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["detail"])
    return result
