"""Acceptance tests for reconciliation/live_verification.py. Every test monkeypatches
guardrail.load_ledger and razorpay_rest's real-API functions -- same convention already used by
reconciliation/test_reconciliation.py's live-ledger tests and api/test_webhooks.py's razorpay_rest
monkeypatching -- so nothing here ever touches the real Razorpay API or the real ledger file."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from reconciliation import live_verification
from reconciliation.live_verification import verify_order_against_razorpay, check_live_drift, remediate_overcharge


def _payment(payment_id, amount_inr, status="captured", amount_refunded_inr=0):
    return {"id": payment_id, "status": status, "amount": round(amount_inr * 100), "amount_refunded": round(amount_refunded_inr * 100)}


def _ledger_entry(order_id, expected_inr, status="success"):
    return {
        "mandate_id": "m1", "product_id": "p001", "razorpay_order_id": order_id,
        "expected_amount_inr": expected_inr, "status": status, "requesting_customer_id": "ai_buyer_001",
        "timestamp": "2026-08-28T05:37:19+00:00",
    }


def _make_available(monkeypatch):
    monkeypatch.setattr(live_verification.razorpay_rest, "REAL_CHECKOUT_AVAILABLE", True)


def test_unavailable_without_real_credentials(monkeypatch):
    monkeypatch.setattr(live_verification.razorpay_rest, "REAL_CHECKOUT_AVAILABLE", False)
    result = check_live_drift()
    assert result["checked"] == 0
    assert "unavailable_reason" in result


def test_verify_order_handles_a_clean_capture(monkeypatch):
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments", lambda oid: [_payment("pay_1", 500)])
    live = verify_order_against_razorpay("order_1")
    assert live == {"razorpay_captured_amount_inr": 500.0, "razorpay_payment_status": "captured", "captured_payment_id": "pay_1"}


def test_verify_order_handles_partial_refund(monkeypatch):
    # Razorpay leaves status "captured" for a partial refund -- only amount_refunded moves.
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments",
                         lambda oid: [_payment("pay_1", 500, status="captured", amount_refunded_inr=200)])
    live = verify_order_against_razorpay("order_1")
    assert live["razorpay_captured_amount_inr"] == 300.0
    assert live["razorpay_payment_status"] == "partially_refunded"


def test_verify_order_handles_full_refund_status(monkeypatch):
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments",
                         lambda oid: [_payment("pay_1", 500, status="refunded", amount_refunded_inr=500)])
    live = verify_order_against_razorpay("order_1")
    assert live["razorpay_captured_amount_inr"] == 0.0
    assert live["razorpay_payment_status"] == "refunded"


def test_verify_order_with_no_captured_or_refunded_payment(monkeypatch):
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments", lambda oid: [_payment("pay_1", 500, status="authorized")])
    live = verify_order_against_razorpay("order_1")
    assert live == {"razorpay_captured_amount_inr": 0.0, "razorpay_payment_status": "no_capture", "captured_payment_id": None}


def test_check_live_drift_clean_when_razorpay_agrees(monkeypatch):
    _make_available(monkeypatch)
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500)])
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments", lambda oid: [_payment("pay_1", 500)])
    result = check_live_drift()
    assert result["checked"] == 1
    assert result["clean"] == 1
    assert result["exceptions"] == []


def test_check_live_drift_detects_overcharge(monkeypatch):
    _make_available(monkeypatch)
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500)])
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments", lambda oid: [_payment("pay_1", 700)])
    result = check_live_drift()
    assert result["checked"] == 1
    assert result["clean"] == 0
    assert len(result["exceptions"]) == 1
    exc = result["exceptions"][0]
    assert exc["type"] == "overcharge_drift"
    assert exc["remediable"] is True
    assert exc["razorpay_captured_amount_inr"] == 700.0
    assert exc["expected_amount_inr"] == 500


def test_check_live_drift_detects_undercharge_but_flags_it_not_remediable(monkeypatch):
    _make_available(monkeypatch)
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500)])
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments", lambda oid: [_payment("pay_1", 300)])
    result = check_live_drift()
    exc = result["exceptions"][0]
    assert exc["type"] == "undercharge_drift"
    assert exc["remediable"] is False


def test_check_live_drift_flags_refund_not_reflected_at_razorpay(monkeypatch):
    # Ledger already believes this order was refunded, but Razorpay still shows it captured --
    # e.g. our own refund call failed silently, or someone edited the ledger by hand.
    _make_available(monkeypatch)
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, status="refunded")])
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments", lambda oid: [_payment("pay_1", 500)])
    result = check_live_drift()
    exc = result["exceptions"][0]
    assert exc["type"] == "refund_not_reflected"
    assert exc["remediable"] is False


def test_check_live_drift_refunded_ledger_entry_clean_when_razorpay_agrees(monkeypatch):
    _make_available(monkeypatch)
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500, status="refunded")])
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments",
                         lambda oid: [_payment("pay_1", 500, status="refunded", amount_refunded_inr=500)])
    result = check_live_drift()
    assert result["clean"] == 1
    assert result["exceptions"] == []


def test_check_live_drift_skips_entries_with_no_razorpay_order_id(monkeypatch):
    _make_available(monkeypatch)
    entry = _ledger_entry(None, 500)
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [entry])
    result = check_live_drift()
    assert result["checked"] == 0


def test_check_live_drift_skips_entries_not_success_or_refunded(monkeypatch):
    _make_available(monkeypatch)
    entry = _ledger_entry("order_1", 500, status="blocked")
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [entry])
    result = check_live_drift()
    assert result["checked"] == 0


def test_remediate_overcharge_issues_a_refund_for_exactly_the_difference(monkeypatch):
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500)])
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments", lambda oid: [_payment("pay_1", 700)])

    calls = []

    def fake_refund(payment_id, amount_inr):
        calls.append((payment_id, amount_inr))
        return {"id": "rfnd_1", "status": "processed"}

    monkeypatch.setattr(live_verification.razorpay_rest, "refund_payment", fake_refund)
    result = remediate_overcharge("order_1", admin_username="admin")
    assert result["status"] == "refunded"
    assert result["refund_amount_inr"] == 200
    assert result["razorpay_refund_id"] == "rfnd_1"
    assert calls == [("pay_1", 200)]


def test_remediate_overcharge_re_verifies_and_does_nothing_if_already_fixed(monkeypatch):
    # Simulates someone else having already refunded it between the dashboard's last check and
    # this admin clicking the button -- must re-check fresh, not trust a stale number, and must
    # never call refund_payment again.
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [_ledger_entry("order_1", 500)])
    monkeypatch.setattr(live_verification.razorpay_rest, "fetch_order_payments", lambda oid: [_payment("pay_1", 500)])

    def fail_if_called(payment_id, amount_inr):
        raise AssertionError("refund_payment should not be called when there's nothing to remediate")

    monkeypatch.setattr(live_verification.razorpay_rest, "refund_payment", fail_if_called)
    result = remediate_overcharge("order_1", admin_username="admin")
    assert result["status"] == "no_action_needed"


def test_remediate_overcharge_unknown_order_is_an_error(monkeypatch):
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [])
    result = remediate_overcharge("order_ghost", admin_username="admin")
    assert result["status"] == "error"


def test_remediate_overcharge_missing_expected_amount_is_an_error(monkeypatch):
    entry = _ledger_entry("order_1", None)
    monkeypatch.setattr(live_verification.guardrail, "load_ledger", lambda: [entry])
    result = remediate_overcharge("order_1", admin_username="admin")
    assert result["status"] == "error"
