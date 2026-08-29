"""Acceptance tests for the reconciliation engine."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from reconciliation import reconciliation
from reconciliation.reconciliation import reconcile, load_orders, load_settlements, load_live_orders_and_settlements


def _order(order_id, amount):
    return {"order_id": order_id, "product_id": "p001", "customer_id": "c001",
            "expected_amount_inr": amount, "order_date": "2026-08-01"}


def _settlement(sid, order_id, amount, status="captured"):
    return {"settlement_id": sid, "order_id": order_id, "settled_amount_inr": amount,
            "status": status, "settlement_date": "2026-08-01"}


def test_clean_match():
    r = reconcile([_order("o1", 1000)], [_settlement("s1", "o1", 1000)])
    assert r["matched"] == 1
    assert r["exceptions"] == []
    assert r["match_rate"] == 1.0


def test_small_rounding_difference_is_not_a_mismatch():
    # Within AMOUNT_TOLERANCE_INR -- a genuine settlement shouldn't be flagged over a paisa.
    r = reconcile([_order("o1", 1000)], [_settlement("s1", "o1", 1000)])
    assert r["matched"] == 1


def test_amount_mismatch_detected():
    r = reconcile([_order("o1", 1000)], [_settlement("s1", "o1", 950)])
    assert r["matched"] == 0
    assert len(r["exceptions"]) == 1
    assert r["exceptions"][0]["type"] == "amount_mismatch"
    assert r["exceptions"][0]["expected_amount_inr"] == 1000
    assert r["exceptions"][0]["settled_amount_inr"] == 950


def test_missing_settlement_detected():
    r = reconcile([_order("o1", 1000)], [])
    assert r["matched"] == 0
    assert r["exceptions"][0]["type"] == "missing_settlement"


def test_duplicate_settlement_detected():
    r = reconcile([_order("o1", 1000)], [_settlement("s1", "o1", 1000), _settlement("s2", "o1", 1000)])
    assert r["matched"] == 0
    assert r["exceptions"][0]["type"] == "duplicate_settlement"
    assert set(r["exceptions"][0]["settlement_ids"]) == {"s1", "s2"}


def test_status_exception_detected():
    r = reconcile([_order("o1", 1000)], [_settlement("s1", "o1", 1000, status="failed")])
    assert r["matched"] == 0
    assert r["exceptions"][0]["type"] == "status_exception"
    assert r["exceptions"][0]["settlement_status"] == "failed"


def test_orphan_settlement_detected():
    r = reconcile([_order("o1", 1000)], [_settlement("s1", "o1", 1000), _settlement("s2", "ghost", 500)])
    assert r["matched"] == 1
    orphan = [e for e in r["exceptions"] if e["type"] == "orphan_settlement"]
    assert len(orphan) == 1
    assert orphan[0]["order_id"] == "ghost"


def test_every_order_accounted_for_matched_or_exception():
    # The core honesty guarantee: nothing silently disappears from the report.
    orders = [_order("o1", 1000), _order("o2", 500), _order("o3", 200)]
    settlements = [_settlement("s1", "o1", 1000)]  # o2, o3 unresolved
    r = reconcile(orders, settlements)
    exception_order_ids = {e["order_id"] for e in r["exceptions"] if e["type"] != "orphan_settlement"}
    assert r["matched"] + len(exception_order_ids) == len(orders)
    assert exception_order_ids == {"o2", "o3"}


def test_seed_data_matches_its_own_ground_truth():
    # The seed data's _expected_reconciliation_status field IS the ground truth -- the engine
    # must classify every record exactly the way the generator intended, or the ground truth
    # and the engine have silently drifted apart.
    orders = load_orders()
    settlements = load_settlements()
    r = reconcile(orders, settlements)

    exception_by_order = {e["order_id"]: e["type"] for e in r["exceptions"] if e["type"] != "orphan_settlement"}
    for order in orders:
        expected = order["_expected_reconciliation_status"]
        actual = exception_by_order.get(order["order_id"], "matched")
        assert actual == expected, f"{order['order_id']}: expected {expected}, engine said {actual}"


def test_live_ledger_success_entry_becomes_a_matched_order(monkeypatch):
    fake_ledger = [{
        "mandate_id": "m1", "product_id": "p001", "razorpay_order_id": "order_live_1",
        "expected_amount_inr": 1799, "status": "success", "requesting_customer_id": "ai_buyer_001",
        "verification": {"amount_charged_inr": 1799, "amount_expected_inr": 1799, "amount_settled_inr": 1799, "all_match": True},
        "timestamp": "2026-08-28T05:37:19.795094+00:00",
    }]
    monkeypatch.setattr(reconciliation.guardrail, "load_ledger", lambda: fake_ledger)
    orders, settlements = load_live_orders_and_settlements()
    assert orders == [{
        "order_id": "order_live_1", "product_id": "p001", "customer_id": "ai_buyer_001",
        "expected_amount_inr": 1799, "order_date": "2026-08-28", "source": "live_ledger",
    }]
    r = reconcile(orders, settlements)
    assert r["matched"] == 1
    assert r["by_source"]["live_ledger"] == {"total": 1, "matched": 1}


def test_live_ledger_entry_never_reaching_razorpay_is_skipped(monkeypatch):
    # A purchase blocked before an order was ever created (razorpay_order_id is None) moved no
    # money -- reconciliation has nothing to say about it, so it must not appear as a phantom order.
    fake_ledger = [{
        "mandate_id": "m1", "product_id": "p001", "razorpay_order_id": None,
        "expected_amount_inr": 1799, "status": "blocked", "requesting_customer_id": "ai_buyer_001",
        "verification": None, "timestamp": "2026-08-28T05:37:19.795094+00:00",
    }]
    monkeypatch.setattr(reconciliation.guardrail, "load_ledger", lambda: fake_ledger)
    orders, settlements = load_live_orders_and_settlements()
    assert orders == []
    assert settlements == []


def test_live_ledger_refund_surfaces_as_status_exception_not_a_clean_match(monkeypatch):
    fake_ledger = [{
        "mandate_id": "m1", "product_id": "p001", "razorpay_order_id": "order_live_2",
        "expected_amount_inr": 500, "status": "refunded", "requesting_customer_id": "ai_buyer_002",
        "verification": {"amount_charged_inr": 500, "amount_expected_inr": 500, "amount_settled_inr": 500, "all_match": True},
        "timestamp": "2026-08-28T06:00:00+00:00",
    }]
    monkeypatch.setattr(reconciliation.guardrail, "load_ledger", lambda: fake_ledger)
    orders, settlements = load_live_orders_and_settlements()
    r = reconcile(orders, settlements)
    assert r["matched"] == 0
    assert r["exceptions"][0]["type"] == "status_exception"
    assert r["exceptions"][0]["source"] == "live_ledger"


def test_reconcile_from_disk_merges_live_ledger_by_default(monkeypatch):
    fake_ledger = [{
        "mandate_id": "m1", "product_id": "p001", "razorpay_order_id": "order_live_3",
        "expected_amount_inr": 1799, "status": "success", "requesting_customer_id": "ai_buyer_003",
        "verification": {"amount_charged_inr": 1799, "amount_expected_inr": 1799, "amount_settled_inr": 1799, "all_match": True},
        "timestamp": "2026-08-28T07:00:00+00:00",
    }]
    monkeypatch.setattr(reconciliation.guardrail, "load_ledger", lambda: fake_ledger)
    seed_only = reconciliation.reconcile_from_disk(include_live=False)
    merged = reconciliation.reconcile_from_disk(include_live=True)
    assert merged["total_orders"] == seed_only["total_orders"] + 1
    assert merged["by_source"]["live_ledger"] == {"total": 1, "matched": 1}


if __name__ == "__main__":
    test_clean_match()
    test_small_rounding_difference_is_not_a_mismatch()
    test_amount_mismatch_detected()
    test_missing_settlement_detected()
    test_duplicate_settlement_detected()
    test_status_exception_detected()
    test_orphan_settlement_detected()
    test_every_order_accounted_for_matched_or_exception()
    test_seed_data_matches_its_own_ground_truth()
    print("All Reconciliation tests passed (run via pytest for the live-ledger monkeypatch cases).")
