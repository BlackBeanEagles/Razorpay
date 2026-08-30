"""POST /api/reconciliation/bank-statement -- admin-only, cross-checks a pasted bank statement
against the real ledger (reconciliation/bank_statement.py). Same reasoning as the other
reconciliation/audit routes: this is internal finance-ops data, not something a shopper should
be able to query."""
import os
import sys

from fastapi import APIRouter, Depends
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from reconciliation.bank_statement import reconcile_bank_statement
from api.auth import require_admin

router = APIRouter()


class BankStatementRequest(BaseModel):
    statement_text: str


@router.post("/api/reconciliation/bank-statement")
def bank_statement(body: BankStatementRequest, _admin: str = Depends(require_admin)):
    return reconcile_bank_statement(body.statement_text)
