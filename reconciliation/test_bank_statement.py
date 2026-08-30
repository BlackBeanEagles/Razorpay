"""Acceptance tests for reconciliation/bank_statement.py. Monkeypatches guardrail.load_ledger and
agent.groq_client.chat_completion -- never touches the real ledger or calls the real Groq API."""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from reconciliation import bank_statement
from reconciliation.bank_statement import parse_bank_statement, match_statement_to_ledger, reconcile_bank_statement


def _tool_call_response(transactions: list) -> dict:
    return {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "record_transactions", "arguments": json.dumps({"transactions": transactions})},
                }],
            },
        }],
    }


def _ledger_entry(order_id, amount, date, status="success", product_id="p001", customer_id="c777"):
    return {
        "razorpay_order_id": order_id, "expected_amount_inr": amount, "status": status,
        "product_id": product_id, "requesting_customer_id": customer_id, "timestamp": f"{date}T10:00:00+00:00",
    }


# ---------- parse_bank_statement ----------

def test_parse_bank_statement_without_llm_configured(monkeypatch):
    monkeypatch.setattr(bank_statement, "LLM_AVAILABLE", False)
    result = parse_bank_statement("some statement text")
    assert result["parsed"] is False
    assert "GROQ_API_KEY" in result["detail"]


def test_parse_bank_statement_with_empty_text(monkeypatch):
    monkeypatch.setattr(bank_statement, "LLM_AVAILABLE", True)
    result = parse_bank_statement("   ")
    assert result["parsed"] is False


def test_parse_bank_statement_returns_extracted_transactions(monkeypatch):
    monkeypatch.setattr(bank_statement, "LLM_AVAILABLE", True)
    fake_transactions = [{"date": "2026-08-01", "utr": "UTR123", "amount_inr": 500, "narration": "UPI/pay001"}]
    monkeypatch.setattr(bank_statement, "chat_completion", lambda messages, **kwargs: _tool_call_response(fake_transactions))
    result = parse_bank_statement("01-08-2026 UPI/pay001 500.00")
    assert result["parsed"] is True
    assert result["transactions"] == fake_transactions


def test_parse_bank_statement_forces_the_tool_call(monkeypatch):
    monkeypatch.setattr(bank_statement, "LLM_AVAILABLE", True)
    captured = {}

    def fake_chat_completion(messages, tools=None, tool_choice=None, temperature=None):
        captured["tool_choice"] = tool_choice
        return _tool_call_response([])

    monkeypatch.setattr(bank_statement, "chat_completion", fake_chat_completion)
    parse_bank_statement("some text")
    assert captured["tool_choice"] == {"type": "function", "function": {"name": "record_transactions"}}


def test_parse_bank_statement_handles_no_tool_call_returned(monkeypatch):
    monkeypatch.setattr(bank_statement, "LLM_AVAILABLE", True)
    monkeypatch.setattr(bank_statement, "chat_completion", lambda messages, **kwargs: {"choices": [{"message": {}}]})
    result = parse_bank_statement("some text")
    assert result["parsed"] is False
    assert "didn't return structured transactions" in result["detail"]


def test_parse_bank_statement_handles_malformed_arguments(monkeypatch):
    monkeypatch.setattr(bank_statement, "LLM_AVAILABLE", True)
    bad_response = {"choices": [{"message": {"tool_calls": [{"function": {"arguments": "not json"}}]}}]}
    monkeypatch.setattr(bank_statement, "chat_completion", lambda messages, **kwargs: bad_response)
    result = parse_bank_statement("some text")
    assert result["parsed"] is False
    assert "Could not parse" in result["detail"]


def test_parse_bank_statement_handles_a_request_exception(monkeypatch):
    monkeypatch.setattr(bank_statement, "LLM_AVAILABLE", True)

    def raise_error(messages, **kwargs):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(bank_statement, "chat_completion", raise_error)
    result = parse_bank_statement("some text")
    assert result["parsed"] is False
    assert "Couldn't reach the model" in result["detail"]


# ---------- match_statement_to_ledger ----------

def test_match_by_amount_and_close_date(monkeypatch):
    monkeypatch.setattr(bank_statement.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, "2026-08-01")])
    transactions = [{"date": "2026-08-01", "amount_inr": 500, "narration": "UPI/x"}]
    result = match_statement_to_ledger(transactions)
    assert len(result["matched"]) == 1
    assert result["matched"][0]["ledger_order_id"] == "order_1"
    assert result["unmatched_statement_lines"] == []
    assert result["unmatched_ledger_entries"] == []


def test_match_within_amount_tolerance(monkeypatch):
    monkeypatch.setattr(bank_statement.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, "2026-08-01")])
    transactions = [{"date": "2026-08-01", "amount_inr": 500.5, "narration": "UPI/x"}]
    result = match_statement_to_ledger(transactions, amount_tolerance_inr=1)
    assert len(result["matched"]) == 1


def test_amount_outside_tolerance_is_unmatched(monkeypatch):
    monkeypatch.setattr(bank_statement.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, "2026-08-01")])
    transactions = [{"date": "2026-08-01", "amount_inr": 550, "narration": "UPI/x"}]
    result = match_statement_to_ledger(transactions, amount_tolerance_inr=1)
    assert result["matched"] == []
    assert len(result["unmatched_statement_lines"]) == 1
    assert len(result["unmatched_ledger_entries"]) == 1


def test_date_within_window_matches(monkeypatch):
    monkeypatch.setattr(bank_statement.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, "2026-08-01")])
    transactions = [{"date": "2026-08-03", "amount_inr": 500, "narration": "UPI/x"}]  # 2 days later
    result = match_statement_to_ledger(transactions, date_window_days=2)
    assert len(result["matched"]) == 1


def test_date_outside_window_does_not_match(monkeypatch):
    monkeypatch.setattr(bank_statement.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, "2026-08-01")])
    transactions = [{"date": "2026-08-10", "amount_inr": 500, "narration": "UPI/x"}]  # 9 days later
    result = match_statement_to_ledger(transactions, date_window_days=2)
    assert result["matched"] == []
    assert len(result["unmatched_statement_lines"]) == 1


def test_missing_dates_on_either_side_still_match_by_amount_alone(monkeypatch):
    monkeypatch.setattr(bank_statement.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, "2026-08-01")])
    transactions = [{"date": None, "amount_inr": 500, "narration": "UPI/x"}]
    result = match_statement_to_ledger(transactions)
    assert len(result["matched"]) == 1


def test_a_ledger_entry_is_never_matched_twice(monkeypatch):
    # Two statement lines with the same amount, one real ledger entry -- only one can match.
    monkeypatch.setattr(bank_statement.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, "2026-08-01")])
    transactions = [
        {"date": "2026-08-01", "amount_inr": 500, "narration": "UPI/x"},
        {"date": "2026-08-01", "amount_inr": 500, "narration": "UPI/y (duplicate)"},
    ]
    result = match_statement_to_ledger(transactions)
    assert len(result["matched"]) == 1
    assert len(result["unmatched_statement_lines"]) == 1


def test_ignores_blocked_and_failed_ledger_entries(monkeypatch):
    monkeypatch.setattr(bank_statement.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, "2026-08-01", status="blocked")])
    transactions = [{"date": "2026-08-01", "amount_inr": 500, "narration": "UPI/x"}]
    result = match_statement_to_ledger(transactions)
    assert result["matched"] == []
    assert result["unmatched_ledger_entries"] == []  # never even considered -- not a real captured payment


# ---------- reconcile_bank_statement (end to end) ----------

def test_reconcile_bank_statement_end_to_end(monkeypatch):
    monkeypatch.setattr(bank_statement, "LLM_AVAILABLE", True)
    monkeypatch.setattr(bank_statement, "chat_completion",
                         lambda messages, **kwargs: _tool_call_response([{"date": "2026-08-01", "amount_inr": 500, "narration": "UPI/x"}]))
    monkeypatch.setattr(bank_statement.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, "2026-08-01")])

    result = reconcile_bank_statement("01-08-2026 UPI/x 500.00")
    assert result["parsed"] is True
    assert len(result["matched"]) == 1
    assert result["total_statement_lines"] == 1
    assert result["total_ledger_entries"] == 1


def test_reconcile_bank_statement_reports_parse_failure_honestly(monkeypatch):
    monkeypatch.setattr(bank_statement, "LLM_AVAILABLE", False)
    result = reconcile_bank_statement("some text")
    assert result["parsed"] is False
    assert result["matched"] == []
