"""GET /api/disputes, POST /api/disputes/{id}/submit, POST /api/disputes/{id}/accept --
the admin-facing side of disputes/dispute_response.py's AI-drafted chargeback evidence.

Admin-only, same reasoning as audit.py and live_verification.py: this reads real dispute data
and, for submit/accept, sends a real response to a customer's bank or issues a real refund --
both are always a deliberate, confirmed admin action, never triggered by an AI agent on its own."""
import os
import sys

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from disputes.dispute_response import list_dispute_drafts, get_dispute_draft, submit_dispute_response, accept_dispute_action
from api.auth import require_admin

router = APIRouter()


@router.get("/api/disputes")
def disputes(_admin: str = Depends(require_admin)):
    return {"disputes": list_dispute_drafts()}


class SubmitRequest(BaseModel):
    summary: str | None = None


@router.post("/api/disputes/{dispute_id}/submit")
def submit(dispute_id: str, body: SubmitRequest, admin: str = Depends(require_admin)):
    if get_dispute_draft(dispute_id) is None:
        raise HTTPException(status_code=404, detail=f"No draft on file for dispute {dispute_id!r}.")
    result = submit_dispute_response(dispute_id, admin_username=admin, summary_override=body.summary)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["detail"])
    return result


@router.post("/api/disputes/{dispute_id}/accept")
def accept(dispute_id: str, admin: str = Depends(require_admin)):
    result = accept_dispute_action(dispute_id, admin_username=admin)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["detail"])
    return result
