"""Acceptance tests for subscriptions/allowance_subscription.py. Isolates SUBSCRIPTIONS_PATH to a
fresh temp file per test (pytest's tmp_path) and monkeypatches guardrail + razorpay_rest -- same
convention as disputes/test_dispute_response.py and reconciliation/test_live_verification.py --
so nothing here touches the real store, the real mandate store, or the real Razorpay API."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from subscriptions import allowance_subscription
from subscriptions.allowance_subscription import (
    create_allowance_subscription, list_allowance_subscriptions, get_allowance_subscription,
    get_allowance_subscription_for_mandate, update_subscription_status, handle_subscription_charged,
)


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(allowance_subscription, "SUBSCRIPTIONS_PATH", str(tmp_path / "allowance_subscriptions.json"))


_MANDATE_STATE = {
    "mandate_id": "m_customer_1", "merchant_id": "techbazaar", "max_amount_inr": 5000,
    "amount_spent_so_far_inr": 4200, "issued_at": "2026-08-01T00:00:00+00:00",
    "expires_at": "2026-08-31T00:00:00+00:00", "single_use": False, "is_expired": False,
    "owner_customer_id": "c777",
}


def _stub_mandate(monkeypatch, state=_MANDATE_STATE):
    monkeypatch.setattr(allowance_subscription.guardrail, "get_mandate_state", lambda mandate_id, requesting_customer_id=None: state)


def test_create_allowance_subscription_makes_real_plan_and_subscription_calls(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _stub_mandate(monkeypatch)
    plan_calls, sub_calls = [], []
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_plan",
                         lambda amount_inr, period, interval, name, description=None: plan_calls.append((amount_inr, period, interval)) or {"id": "plan_1"})
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_subscription",
                         lambda plan_id, total_count, notes=None, customer_notify=True: sub_calls.append((plan_id, total_count, notes))
                         or {"id": "sub_1", "status": "created", "short_url": "https://rzp.io/x/abc"})

    record = create_allowance_subscription("m_customer_1", "c777", 5000, total_count=12)
    assert record["subscription_id"] == "sub_1"
    assert record["short_url"] == "https://rzp.io/x/abc"
    assert record["status"] == "created"
    assert record["last_paid_count"] == 0
    assert plan_calls == [(5000, "monthly", 1)]
    assert sub_calls[0][0] == "plan_1"
    assert sub_calls[0][2] == {"mandate_id": "m_customer_1", "customer_id": "c777", "purpose": "guardrail_allowance_refresh"}
    assert get_allowance_subscription("sub_1")["subscription_id"] == "sub_1"
    assert list_allowance_subscriptions(customer_id="c777")[0]["subscription_id"] == "sub_1"


def test_create_allowance_subscription_rejects_unknown_or_unowned_mandate(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(allowance_subscription.guardrail, "get_mandate_state", lambda mandate_id, requesting_customer_id=None: None)
    result = create_allowance_subscription("m_ghost", "c777", 5000)
    assert result["status"] == "error"


def test_create_allowance_subscription_rejects_a_second_one_for_the_same_mandate(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _stub_mandate(monkeypatch)
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_plan", lambda *a, **k: {"id": "plan_1"})
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_subscription",
                         lambda *a, **k: {"id": "sub_1", "status": "created", "short_url": "https://rzp.io/x/abc"})
    create_allowance_subscription("m_customer_1", "c777", 5000)

    result = create_allowance_subscription("m_customer_1", "c777", 5000)
    assert result["status"] == "error"
    assert "already" in result["detail"].lower()


def test_get_allowance_subscription_for_mandate_ignores_cancelled_ones(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _stub_mandate(monkeypatch)
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_plan", lambda *a, **k: {"id": "plan_1"})
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_subscription",
                         lambda *a, **k: {"id": "sub_1", "status": "created", "short_url": "https://rzp.io/x/abc"})
    create_allowance_subscription("m_customer_1", "c777", 5000)
    update_subscription_status("sub_1", "cancelled")

    assert get_allowance_subscription_for_mandate("m_customer_1", "c777") is None
    # A new one can now be registered for the same mandate since the old one is cancelled.
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_subscription",
                         lambda *a, **k: {"id": "sub_2", "status": "created", "short_url": "https://rzp.io/x/def"})
    result = create_allowance_subscription("m_customer_1", "c777", 5000)
    assert result["subscription_id"] == "sub_2"


def test_update_subscription_status_updates_existing_record(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _stub_mandate(monkeypatch)
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_plan", lambda *a, **k: {"id": "plan_1"})
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_subscription",
                         lambda *a, **k: {"id": "sub_1", "status": "created", "short_url": "https://rzp.io/x/abc"})
    create_allowance_subscription("m_customer_1", "c777", 5000)

    updated = update_subscription_status("sub_1", "active")
    assert updated["status"] == "active"
    assert get_allowance_subscription("sub_1")["status"] == "active"


def test_update_subscription_status_unknown_subscription_returns_none(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert update_subscription_status("sub_ghost", "active") is None


def test_handle_subscription_charged_renews_the_mandate(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _stub_mandate(monkeypatch)
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_plan", lambda *a, **k: {"id": "plan_1"})
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_subscription",
                         lambda *a, **k: {"id": "sub_1", "status": "created", "short_url": "https://rzp.io/x/abc"})
    create_allowance_subscription("m_customer_1", "c777", 5000)

    renew_calls = []

    def fake_renew(mandate_id, new_expires_at):
        renew_calls.append((mandate_id, new_expires_at))
        return {**_MANDATE_STATE, "amount_spent_so_far_inr": 0, "expires_at": "2026-09-30T00:00:00+00:00"}

    monkeypatch.setattr(allowance_subscription.guardrail, "renew_mandate", fake_renew)
    result = handle_subscription_charged("sub_1", paid_count=1, current_end=1780000000)
    assert result["status"] == "renewed"
    assert renew_calls == [("m_customer_1", 1780000000.0)]
    assert get_allowance_subscription("sub_1")["last_paid_count"] == 1
    assert get_allowance_subscription("sub_1")["status"] == "active"
    assert get_allowance_subscription("sub_1")["last_renewed_at"] is not None


def test_handle_subscription_charged_is_idempotent_against_webhook_redelivery(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _stub_mandate(monkeypatch)
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_plan", lambda *a, **k: {"id": "plan_1"})
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_subscription",
                         lambda *a, **k: {"id": "sub_1", "status": "created", "short_url": "https://rzp.io/x/abc"})
    create_allowance_subscription("m_customer_1", "c777", 5000)

    calls = []
    monkeypatch.setattr(allowance_subscription.guardrail, "renew_mandate",
                         lambda mandate_id, new_expires_at: calls.append(1) or {**_MANDATE_STATE, "amount_spent_so_far_inr": 0})
    handle_subscription_charged("sub_1", paid_count=1, current_end=1780000000)
    result = handle_subscription_charged("sub_1", paid_count=1, current_end=1780000000)  # redelivery of the same cycle
    assert result["status"] == "already_processed"
    assert len(calls) == 1  # renew_mandate only called once, not twice


def test_handle_subscription_charged_with_no_local_record_is_ignored(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    result = handle_subscription_charged("sub_ghost", paid_count=1, current_end=1780000000)
    assert result["status"] == "ignored"


def test_handle_subscription_charged_when_mandate_no_longer_exists(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    _stub_mandate(monkeypatch)
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_plan", lambda *a, **k: {"id": "plan_1"})
    monkeypatch.setattr(allowance_subscription.razorpay_rest, "create_subscription",
                         lambda *a, **k: {"id": "sub_1", "status": "created", "short_url": "https://rzp.io/x/abc"})
    create_allowance_subscription("m_customer_1", "c777", 5000)

    monkeypatch.setattr(allowance_subscription.guardrail, "renew_mandate", lambda mandate_id, new_expires_at: None)
    result = handle_subscription_charged("sub_1", paid_count=1, current_end=1780000000)
    assert result["status"] == "mandate_missing"
    assert get_allowance_subscription("sub_1")["last_paid_count"] == 1  # still marked processed
    assert get_allowance_subscription("sub_1")["status"] == "mandate_missing"
