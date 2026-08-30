"""Bank statement cross-check: a third reconciliation source, independent of both this app's own
ledger and Razorpay's own API -- the merchant's actual bank statement, the real ground truth a
founder cares about. Directly inspired by Razorpay's own Agentic Platform ("Intelligent
Reconciliation": paste or upload a bank statement, an agent extracts transaction lines and flags
discrepancies against Razorpay records) -- built here scoped to what's realistic without OCR:
pasted or uploaded text (CSV rows, or copy-pasted statement lines), not a screenshot image.

Test mode has no real bank settlement cycle (see guardrail/razorpay_mcp_client.py's docstring),
so there is no real UTR on our side to match against here -- matching is by amount (within
AMOUNT_TOLERANCE_INR) and date proximity (within a configurable window) instead, an honest,
real-world fallback finance teams already reach for when UTR data isn't reliably available on
both sides. A live-mode account restores exact UTR matching for free, since Razorpay's own real
settlement records do carry it.

Extraction uses a forced Groq tool call (record_transactions), not free-text JSON parsing --
same reliability reasoning as every other structured extraction in this codebase: a tool call's
arguments are already valid JSON by construction, so there's no brittle "hope the model wrapped
its JSON correctly" parsing step.
"""
import json
import os
import sys
from datetime import datetime

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from guardrail import guardrail
from agent.groq_client import chat_completion, LLM_AVAILABLE
from audit.audit_log import log_event

AMOUNT_TOLERANCE_INR = 1
DEFAULT_DATE_WINDOW_DAYS = 2

_EXTRACT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "record_transactions",
            "description": "Records every transaction line found in the bank statement text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "transactions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD if present in the statement, else null"},
                                "utr": {"type": ["string", "null"], "description": "UTR/reference number if present in the statement, else null"},
                                "amount_inr": {"type": "number", "description": "Transaction amount in INR, always positive"},
                                "narration": {"type": "string", "description": "The raw description/narration text for this line"},
                            },
                            "required": ["amount_inr", "narration"],
                        },
                    },
                },
                "required": ["transactions"],
            },
        },
    },
]


def parse_bank_statement(statement_text: str) -> dict:
    """Returns {"parsed": bool, "transactions": [...], "detail": str|None}. Never raises --
    every real failure mode (no API key, no tool call returned, malformed arguments, a
    request-level error reaching Groq) is reported honestly in "detail" instead."""
    if not LLM_AVAILABLE:
        return {"parsed": False, "transactions": [], "detail": "Bank statement parsing isn't configured (no GROQ_API_KEY)."}
    if not statement_text or not statement_text.strip():
        return {"parsed": False, "transactions": [], "detail": "No statement text provided."}

    messages = [
        {"role": "system", "content": (
            "You extract structured transaction data from bank statement text (pasted CSV rows "
            "or copy-pasted statement lines). Call record_transactions with every transaction "
            "line you can find. Never invent a transaction that isn't actually in the text."
        )},
        {"role": "user", "content": statement_text},
    ]
    try:
        response = chat_completion(
            messages, tools=_EXTRACT_TOOL,
            tool_choice={"type": "function", "function": {"name": "record_transactions"}},
            temperature=0,
        )
    except requests.RequestException as e:
        return {"parsed": False, "transactions": [], "detail": f"Couldn't reach the model: {e}"}

    tool_calls = response["choices"][0]["message"].get("tool_calls") or []
    if not tool_calls:
        return {"parsed": False, "transactions": [], "detail": "The model didn't return structured transactions."}
    try:
        args = json.loads(tool_calls[0]["function"]["arguments"])
    except (json.JSONDecodeError, KeyError):
        return {"parsed": False, "transactions": [], "detail": "Could not parse the model's structured response."}
    return {"parsed": True, "transactions": args.get("transactions") or [], "detail": None}


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def match_statement_to_ledger(transactions: list, amount_tolerance_inr: float = AMOUNT_TOLERANCE_INR,
                               date_window_days: int = DEFAULT_DATE_WINDOW_DAYS) -> dict:
    """Matches each parsed statement line against a real, captured ledger entry by amount and
    (when both sides have one) date proximity -- never by UTR, see module docstring for why.
    Greedy first-fit: each ledger entry can be consumed by at most one statement line, so a
    duplicate line in the statement correctly shows up as unmatched rather than double-counting
    the same real order."""
    ledger_entries = [
        {"order_id": e["razorpay_order_id"], "expected_amount_inr": e.get("expected_amount_inr"),
         "date": (e.get("timestamp") or "")[:10], "product_id": e.get("product_id"),
         "customer_id": e.get("requesting_customer_id")}
        for e in guardrail.load_ledger()
        if e.get("status") in ("success", "refunded") and e.get("razorpay_order_id")
    ]
    used_indexes = set()
    matched, unmatched_statement = [], []

    for line in transactions:
        amount = line.get("amount_inr")
        line_date = _parse_date(line.get("date"))
        found_index = None
        for idx, entry in enumerate(ledger_entries):
            if idx in used_indexes:
                continue
            if entry["expected_amount_inr"] is None or amount is None or abs(entry["expected_amount_inr"] - amount) > amount_tolerance_inr:
                continue
            entry_date = _parse_date(entry["date"])
            if line_date and entry_date and abs((line_date - entry_date).days) > date_window_days:
                continue
            found_index = idx
            break
        if found_index is not None:
            used_indexes.add(found_index)
            matched.append({"statement_line": line, "ledger_order_id": ledger_entries[found_index]["order_id"]})
        else:
            unmatched_statement.append(line)

    unmatched_ledger = [e for idx, e in enumerate(ledger_entries) if idx not in used_indexes]
    return {
        "matched": matched, "unmatched_statement_lines": unmatched_statement, "unmatched_ledger_entries": unmatched_ledger,
        "total_statement_lines": len(transactions), "total_ledger_entries": len(ledger_entries),
    }


def reconcile_bank_statement(statement_text: str) -> dict:
    """The full flow: parse the pasted statement, then match it against the real ledger.
    Returns {"parsed", "detail", "matched", "unmatched_statement_lines",
    "unmatched_ledger_entries", "total_statement_lines", "total_ledger_entries"}."""
    parse_result = parse_bank_statement(statement_text)
    if not parse_result["parsed"]:
        return {
            "parsed": False, "detail": parse_result["detail"], "matched": [],
            "unmatched_statement_lines": [], "unmatched_ledger_entries": [],
            "total_statement_lines": 0, "total_ledger_entries": 0,
        }

    match_result = match_statement_to_ledger(parse_result["transactions"])
    all_clean = not match_result["unmatched_statement_lines"] and not match_result["unmatched_ledger_entries"]
    log_event(
        "reconciliation", "bank_statement_reconciled",
        {"transaction_count": len(parse_result["transactions"])},
        {"matched": len(match_result["matched"]), "unmatched_statement": len(match_result["unmatched_statement_lines"]),
         "unmatched_ledger": len(match_result["unmatched_ledger_entries"])},
        "ok" if all_clean else "flagged",
    )
    return {"parsed": True, "detail": None, **match_result}
