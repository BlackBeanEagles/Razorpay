"""POST /api/settlement-qa -- natural-language Q&A over the real reconciliation batch
(reconciliation/settlement_qa.py). Admin-only: same reasoning as audit.py, this is internal
finance-ops data, not something a shopper should be able to query."""
import os
import sys

from fastapi import APIRouter, Depends
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from reconciliation.settlement_qa import ask
from api.auth import require_admin

router = APIRouter()


class SettlementQARequest(BaseModel):
    question: str


@router.post("/api/settlement-qa")
def settlement_qa(body: SettlementQARequest, _admin: str = Depends(require_admin)):
    return ask(body.question)
