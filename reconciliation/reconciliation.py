"""Reconciliation: closes the internal-orders-vs-settlements finance-ops loop.

reconcile(orders, settlements) -> a per-order match/exception classification plus a summary
(match rate, exception counts by type). This is the batch generalization of the same
charged/expected/settled comparison Guardrail already does per-transaction (see
guardrail.execute_purchase/confirm_purchase's "verification" dict) -- here it runs across an
entire batch of records at once and, critically, never silently drops what it can't resolve:
every record ends up either "matched" or in the exception list with a specific reason, never
just absent from the report.

Amount comparisons use a small tolerance (AMOUNT_TOLERANCE_INR) rather than exact equality --
real settlement amounts can differ from the expected amount by a paisa or two due to rounding,
and treating that as a genuine mismatch would manufacture false exceptions.
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from guardrail import guardrail

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
ORDERS_PATH = os.path.join(DATA_DIR, "internal_orders.json")
SETTLEMENTS_PATH = os.path.join(DATA_DIR, "settlement_records.json")

AMOUNT_TOLERANCE_INR = 1  # settlement vs expected amounts within this are NOT a mismatch


def load_orders() -> list:
    with open(ORDERS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_settlements() -> list:
    with open(SETTLEMENTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_live_orders_and_settlements() -> tuple:
    """Turns real guardrail/ledger.json entries -- genuine purchases that went through
    Guardrail's mandate-enforced flow, including ones driven by external AI buyers over MCP --
    into the same order/settlement shape the synthetic seed uses, tagged source="live_ledger"
    so the report never conflates a real transaction with a synthetic one.

    A ledger entry that never reached Razorpay at all (blocked before an order existed --
    razorpay_order_id is None) moved no money and has nothing to reconcile, so it's skipped
    entirely rather than fabricating a placeholder order for it. Every entry that DID reach
    Razorpay is kept: a "refunded" outcome becomes a settlement with status "refunded" (correctly
    surfaces as a status_exception -- money moved and came back, not a clean match), and a
    captured-but-mismatched entry keeps its real settled amount (correctly surfaces as
    amount_mismatch) rather than being silently coerced into looking clean.
    """
    orders, settlements = [], []
    for entry in guardrail.load_ledger():
        order_id = entry.get("razorpay_order_id")
        if not order_id:
            continue
        verification = entry.get("verification") or {}
        order_date = entry.get("timestamp", "")[:10]
        orders.append({
            "order_id": order_id,
            "product_id": entry.get("product_id"),
            "customer_id": entry.get("requesting_customer_id"),
            "expected_amount_inr": entry.get("expected_amount_inr"),
            "order_date": order_date,
            "source": "live_ledger",
        })
        settled_amount = verification.get("amount_settled_inr")
        if settled_amount is None:
            continue  # captured_required/checkout_required never independently settled -- counts as missing_settlement, correctly
        settlements.append({
            "settlement_id": f"live_{order_id}",
            "order_id": order_id,
            "settled_amount_inr": settled_amount,
            "status": "refunded" if entry.get("status") == "refunded" else "captured",
            "settlement_date": order_date,
            "source": "live_ledger",
        })
    return orders, settlements


def reconcile(orders: list, settlements: list) -> dict:
    """Matches every order against its settlement(s) and classifies exceptions. Returns:
    {
      "total_orders": int, "matched": int, "match_rate": float,
      "exceptions": [ {order_id, type, reason, ...}, ... ],   # includes orphan settlements
      "exception_counts": {type: count},
    }
    Exception types: amount_mismatch, missing_settlement, duplicate_settlement,
    status_exception, orphan_settlement (a settlement with no corresponding internal order --
    the one exception type not keyed to an order_id that exists in `orders`)."""
    settlements_by_order = defaultdict(list)
    for s in settlements:
        settlements_by_order[s["order_id"]].append(s)

    order_ids = {o["order_id"] for o in orders}
    exceptions = []
    matched = 0
    matched_by_source = defaultdict(int)
    total_by_source = defaultdict(int)

    for order in orders:
        order_id = order["order_id"]
        source = order.get("source")
        total_by_source[source] += 1
        candidates = settlements_by_order.get(order_id, [])
        captured = [s for s in candidates if s["status"] == "captured"]

        if not candidates:
            exceptions.append({
                "order_id": order_id, "type": "missing_settlement", "source": source,
                "reason": f"No settlement record found for this order (expected {order['expected_amount_inr']} INR).",
                "expected_amount_inr": order["expected_amount_inr"],
            })
        elif len(captured) > 1:
            exceptions.append({
                "order_id": order_id, "type": "duplicate_settlement", "source": source,
                "reason": f"{len(captured)} captured settlements found for one order -- needs manual review, not auto-resolvable.",
                "settlement_ids": [s["settlement_id"] for s in captured],
            })
        elif not captured:
            exceptions.append({
                "order_id": order_id, "type": "status_exception", "source": source,
                "reason": f"Settlement exists but status is {candidates[0]['status']!r}, not captured -- order was never actually paid for.",
                "settlement_id": candidates[0]["settlement_id"], "settlement_status": candidates[0]["status"],
            })
        else:
            settlement = captured[0]
            diff = settlement["settled_amount_inr"] - order["expected_amount_inr"]
            if abs(diff) > AMOUNT_TOLERANCE_INR:
                exceptions.append({
                    "order_id": order_id, "type": "amount_mismatch", "source": source,
                    "reason": f"Expected {order['expected_amount_inr']} INR, settlement shows {settlement['settled_amount_inr']} INR (diff {diff:+} INR).",
                    "settlement_id": settlement["settlement_id"],
                    "expected_amount_inr": order["expected_amount_inr"],
                    "settled_amount_inr": settlement["settled_amount_inr"],
                })
            else:
                matched += 1
                matched_by_source[source] += 1

    for s in settlements:
        if s["order_id"] not in order_ids:
            exceptions.append({
                "order_id": s["order_id"], "type": "orphan_settlement", "source": s.get("source"),
                "reason": f"Settlement {s['settlement_id']} references order {s['order_id']!r}, which has no internal order record at all.",
                "settlement_id": s["settlement_id"], "settled_amount_inr": s["settled_amount_inr"],
            })

    exception_counts = defaultdict(int)
    for e in exceptions:
        exception_counts[e["type"]] += 1

    total = len(orders)
    return {
        "total_orders": total,
        "matched": matched,
        "match_rate": matched / total if total else 0.0,
        "exceptions": exceptions,
        "exception_counts": dict(exception_counts),
        "by_source": {
            source: {"total": total_by_source[source], "matched": matched_by_source[source]}
            for source in total_by_source
        },
    }


def reconcile_from_disk(include_live: bool = True) -> dict:
    """The full batch: the fixed synthetic seed (data/internal_orders.json,
    data/settlement_records.json -- generated with a known-in-advance ground truth, see
    data/generate_reconciliation_seed.py) plus, when include_live is True, every real purchase
    currently in guardrail/ledger.json. Real records have no _expected_reconciliation_status
    (nobody scripted their outcome in advance -- that's the point), so
    classification_accuracy_vs_ground_truth in the batch runner is deliberately measured against
    the synthetic subset only; the live subset is reported by source instead, honestly, not
    blended into an accuracy number that would silently overstate what's actually been verified."""
    orders, settlements = load_orders(), load_settlements()
    for o in orders:
        o.setdefault("source", "synthetic_seed")
    for s in settlements:
        s.setdefault("source", "synthetic_seed")
    if include_live:
        live_orders, live_settlements = load_live_orders_and_settlements()
        orders = orders + live_orders
        settlements = settlements + live_settlements
    return reconcile(orders, settlements)
