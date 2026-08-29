"""Acceptance tests for disputes/dispute_response.py. Every test isolates DISPUTES_PATH to a
fresh temp file (pytest's tmp_path) and monkeypatches guardrail.load_ledger / audit_log.read_all
/ razorpay_rest -- same "never touch the real store or the real API" convention already used by
reconciliation/test_live_verification.py and api/test_webhooks.py."""
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from disputes import dispute_response
from disputes.dispute_response import (
    draft_evidence_from_audit_trail, record_dispute_created, update_dispute_status,
    submit_dispute_response, accept_dispute_action, get_dispute_draft, list_dispute_drafts,
)


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(dispute_response, "DISPUTES_PATH", str(tmp_path / "dispute_drafts.json"))


_LEDGER_ENTRY = {
    "mandate_id": "m_default", "product_id": "p001", "razorpay_order_id": "order_disputed_1",
    "expected_amount_inr": 500, "status": "success", "requesting_customer_id": "c888",
    "verification": {"amount_charged_inr": 500, "amount_expected_inr": 500, "amount_settled_inr": 500, "all_match": True},
    "timestamp": "2026-08-29T10:00:00+00:00",
}

_FAIRNESS_ENTRY = {
    "timestamp": "2026-08-29T09:59:00+00:00", "component": "parity", "event": "fairness_check",
    "input_summary": {"product_id": "p001", "customer_id": "c888", "offered_price_inr": 500},
    "result_summary": {"verdict": "fair", "reason": "Offered price 500 is within 9.5% of the baseline median (510).", "comparison_baseline": 510, "explained_by": None},
    "outcome": "ok",
}


def test_draft_evidence_when_no_ledger_entry_exists(monkeypatch):
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [])
    evidence = draft_evidence_from_audit_trail("order_ghost")
    assert evidence["found"] is False
    assert "order_ghost" in evidence["summary"]
    assert evidence["facts"] == []


def test_draft_evidence_builds_summary_from_ledger_and_fairness_check(monkeypatch):
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [_LEDGER_ENTRY])
    monkeypatch.setattr(dispute_response, "read_all", lambda: [_FAIRNESS_ENTRY])
    evidence = draft_evidence_from_audit_trail("order_disputed_1")
    assert evidence["found"] is True
    assert "m_default" in evidence["summary"]
    assert "fair" in evidence["summary"]
    assert "500" in evidence["summary"]
    assert len(evidence["facts"]) >= 3


def test_draft_evidence_without_a_fairness_check_still_produces_a_summary(monkeypatch):
    # No matching Parity entry (e.g. an old order, or logs rotated) -- still honest, just thinner.
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [_LEDGER_ENTRY])
    monkeypatch.setattr(dispute_response, "read_all", lambda: [])
    evidence = draft_evidence_from_audit_trail("order_disputed_1")
    assert evidence["found"] is True
    assert evidence["fairness_check"] is None


def test_record_dispute_created_saves_locally_and_drafts_on_razorpay(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [_LEDGER_ENTRY])
    monkeypatch.setattr(dispute_response, "read_all", lambda: [_FAIRNESS_ENTRY])
    monkeypatch.setattr(dispute_response.razorpay_rest, "REAL_CHECKOUT_AVAILABLE", True)
    monkeypatch.setattr(dispute_response.razorpay_rest, "contest_dispute",
                         lambda dispute_id, summary, action, amount_inr=None: {"id": dispute_id, "status": "open"})

    record = record_dispute_created("disp_1", "pay_1", "order_disputed_1", 500, "chargeback", 1234567890)
    assert record["status"] == "draft_pending"
    assert record["evidence_found"] is True
    assert record["razorpay_draft"]["attempted"] is True
    assert record["razorpay_draft"]["saved"] is True
    assert get_dispute_draft("disp_1")["dispute_id"] == "disp_1"
    assert list_dispute_drafts()[0]["dispute_id"] == "disp_1"


def test_record_dispute_created_handles_a_404_from_razorpay_honestly(monkeypatch, tmp_path):
    # No self-serve way to create a real test-mode dispute -- a synthetic demo dispute_id will
    # legitimately 404 against the real API. Must be reported honestly, not crash, not silently
    # claimed as saved.
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [_LEDGER_ENTRY])
    monkeypatch.setattr(dispute_response, "read_all", lambda: [_FAIRNESS_ENTRY])
    monkeypatch.setattr(dispute_response.razorpay_rest, "REAL_CHECKOUT_AVAILABLE", True)

    def raise_404(dispute_id, summary, action, amount_inr=None):
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError("404 Not Found", response=resp)

    monkeypatch.setattr(dispute_response.razorpay_rest, "contest_dispute", raise_404)
    record = record_dispute_created("disp_synthetic", "pay_1", "order_disputed_1", 500, "chargeback", 1234567890)
    assert record["status"] == "draft_pending"  # still saved locally
    assert record["razorpay_draft"]["attempted"] is True
    assert record["razorpay_draft"]["saved"] is False
    assert "404" in record["razorpay_draft"]["detail"]


def test_record_dispute_created_skips_razorpay_call_without_real_credentials(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [_LEDGER_ENTRY])
    monkeypatch.setattr(dispute_response, "read_all", lambda: [])
    monkeypatch.setattr(dispute_response.razorpay_rest, "REAL_CHECKOUT_AVAILABLE", False)

    def fail_if_called(*a, **k):
        raise AssertionError("should not call Razorpay without real credentials")

    monkeypatch.setattr(dispute_response.razorpay_rest, "contest_dispute", fail_if_called)
    record = record_dispute_created("disp_2", "pay_1", "order_disputed_1", 500, "chargeback", 1234567890)
    assert record["razorpay_draft"]["attempted"] is False


def test_update_dispute_status_updates_existing_record(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [_LEDGER_ENTRY])
    monkeypatch.setattr(dispute_response, "read_all", lambda: [])
    monkeypatch.setattr(dispute_response.razorpay_rest, "REAL_CHECKOUT_AVAILABLE", False)
    record_dispute_created("disp_3", "pay_1", "order_disputed_1", 500, "chargeback", 1234567890)

    updated = update_dispute_status("disp_3", "won")
    assert updated["status"] == "won"
    assert get_dispute_draft("disp_3")["status"] == "won"


def test_update_dispute_status_unknown_dispute_returns_none(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert update_dispute_status("disp_ghost", "won") is None


def test_submit_dispute_response_calls_contest_with_submit_action(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [_LEDGER_ENTRY])
    monkeypatch.setattr(dispute_response, "read_all", lambda: [_FAIRNESS_ENTRY])
    monkeypatch.setattr(dispute_response.razorpay_rest, "REAL_CHECKOUT_AVAILABLE", False)
    record_dispute_created("disp_4", "pay_1", "order_disputed_1", 500, "chargeback", 1234567890)

    calls = []

    def fake_contest(dispute_id, summary, action, amount_inr=None):
        calls.append((dispute_id, action))
        return {"id": dispute_id, "status": "under_review"}

    monkeypatch.setattr(dispute_response.razorpay_rest, "contest_dispute", fake_contest)
    result = submit_dispute_response("disp_4", admin_username="admin")
    assert result["status"] == "submitted"
    assert calls == [("disp_4", "submit")]
    assert get_dispute_draft("disp_4")["status"] == "submitted"
    assert get_dispute_draft("disp_4")["submitted_by"] == "admin"


def test_submit_dispute_response_uses_override_summary(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [_LEDGER_ENTRY])
    monkeypatch.setattr(dispute_response, "read_all", lambda: [])
    monkeypatch.setattr(dispute_response.razorpay_rest, "REAL_CHECKOUT_AVAILABLE", False)
    record_dispute_created("disp_5", "pay_1", "order_disputed_1", 500, "chargeback", 1234567890)

    captured = {}

    def fake_contest(dispute_id, summary, action, amount_inr=None):
        captured["summary"] = summary
        return {"id": dispute_id, "status": "under_review"}

    monkeypatch.setattr(dispute_response.razorpay_rest, "contest_dispute", fake_contest)
    submit_dispute_response("disp_5", admin_username="admin", summary_override="A human-edited explanation.")
    assert captured["summary"] == "A human-edited explanation."
    assert get_dispute_draft("disp_5")["summary"] == "A human-edited explanation."


def test_submit_dispute_response_unknown_dispute_is_an_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    result = submit_dispute_response("disp_ghost", admin_username="admin")
    assert result["status"] == "error"


def test_submit_dispute_response_already_submitted_is_a_noop(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [_LEDGER_ENTRY])
    monkeypatch.setattr(dispute_response, "read_all", lambda: [])
    monkeypatch.setattr(dispute_response.razorpay_rest, "REAL_CHECKOUT_AVAILABLE", False)
    record_dispute_created("disp_6", "pay_1", "order_disputed_1", 500, "chargeback", 1234567890)
    monkeypatch.setattr(dispute_response.razorpay_rest, "contest_dispute",
                         lambda dispute_id, summary, action, amount_inr=None: {"id": dispute_id, "status": "under_review"})
    submit_dispute_response("disp_6", admin_username="admin")

    def fail_if_called(*a, **k):
        raise AssertionError("should not call Razorpay again once already submitted")

    monkeypatch.setattr(dispute_response.razorpay_rest, "contest_dispute", fail_if_called)
    result = submit_dispute_response("disp_6", admin_username="admin2")
    assert result["status"] == "already_submitted"


def test_accept_dispute_action_calls_real_accept_and_updates_status(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(dispute_response.guardrail, "load_ledger", lambda: [_LEDGER_ENTRY])
    monkeypatch.setattr(dispute_response, "read_all", lambda: [])
    monkeypatch.setattr(dispute_response.razorpay_rest, "REAL_CHECKOUT_AVAILABLE", False)
    record_dispute_created("disp_7", "pay_1", "order_disputed_1", 500, "chargeback", 1234567890)

    monkeypatch.setattr(dispute_response.razorpay_rest, "accept_dispute", lambda dispute_id: {"id": dispute_id, "status": "closed"})
    result = accept_dispute_action("disp_7", admin_username="admin")
    assert result["status"] == "accepted"
    assert get_dispute_draft("disp_7")["status"] == "accepted"
    assert get_dispute_draft("disp_7")["accepted_by"] == "admin"


def test_accept_dispute_action_error_when_razorpay_rejects(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    def raise_error(dispute_id):
        raise requests.RequestException("network down")

    monkeypatch.setattr(dispute_response.razorpay_rest, "accept_dispute", raise_error)
    result = accept_dispute_action("disp_ghost", admin_username="admin")
    assert result["status"] == "error"
