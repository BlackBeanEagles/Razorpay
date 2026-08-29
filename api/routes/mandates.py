"""Mandate CRUD -- BUILD_SPEC.md section 8."""
import os
import sys

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from guardrail import guardrail
from api.customer_auth import require_customer

router = APIRouter()


class IssueMandateRequest(BaseModel):
    merchant_id: str
    max_amount_inr: int
    expires_in_seconds: int
    single_use: bool = False


@router.get("/api/mandates/{mandate_id}")
def get_mandate(mandate_id: str, customer_id: str = Depends(require_customer)):
    # requesting_customer_id enforces ownership -- a customer's own mandate (from "set
    # spending limit") is only readable by that same customer; shared/demo mandates (owner
    # None, e.g. m_default) remain readable by anyone logged in, as before.
    state = guardrail.get_mandate_state(mandate_id, requesting_customer_id=customer_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Unknown mandate_id: {mandate_id}")
    return state


@router.post("/api/mandates")
def create_mandate(body: IssueMandateRequest, customer_id: str = Depends(require_customer)):
    return guardrail.issue_and_store_mandate(
        body.merchant_id, body.max_amount_inr, body.expires_in_seconds, body.single_use,
        owner_customer_id=customer_id,
    )
