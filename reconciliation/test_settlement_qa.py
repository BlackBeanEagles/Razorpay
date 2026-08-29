"""Acceptance tests for the settlement Q&A tools -- the data-fetching functions the LLM calls,
not the LLM call itself (that's exercised manually/live, same as agent/llm_agent.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from guardrail import guardrail
from reconciliation.settlement_qa import get_batch_summary, get_order_detail, list_exceptions


def setup_module():
    # These tests read the REAL guardrail ledger (via reconcile_from_disk -> load_ledger()) --
    # if guardrail/test_guardrail.py or mcp_server/test_mcp_server.py ran earlier in the same
    # pytest process, their setup_module() pointed guardrail's global LEDGER_PATH at an isolated
    # test fixture file and nothing pointed it back. Without this, these tests would silently
    # read that leftover fixture data instead of the real, empty-or-genuine ledger.
    guardrail.use_real_store()


def test_batch_summary_matches_the_real_reconciliation_totals():
    summary = get_batch_summary()
    assert summary["total_orders"] == 53
    assert summary["matched"] == 42
    assert summary["by_source"]["synthetic_seed"]["total"] == 53


def test_order_detail_for_a_known_exception():
    detail = get_order_detail("ord_0052")  # seeded status_exception
    assert detail["status"] == "status_exception"
    assert "failed" in detail["reason"]


def test_order_detail_for_an_unknown_order_is_honest_not_a_guess():
    detail = get_order_detail("ord_does_not_exist")
    assert detail["status"] == "matched_or_unknown"


def test_list_exceptions_filters_by_type():
    result = list_exceptions(exception_type="amount_mismatch")
    assert result["count"] == 4
    assert all(e["type"] == "amount_mismatch" for e in result["exceptions"])


def test_list_exceptions_filters_by_source():
    result = list_exceptions(source="synthetic_seed")
    assert result["count"] == 14  # the full seeded exception count, no live purchases yet
    assert all(e.get("source") == "synthetic_seed" for e in result["exceptions"])


def test_list_exceptions_with_no_filter_returns_everything():
    result = list_exceptions()
    assert result["count"] == 14
