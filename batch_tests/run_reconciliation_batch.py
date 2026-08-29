"""Runs the reconciliation engine against the 53-order synthetic batch (data/internal_orders.json,
data/settlement_records.json) and reports throughput, measured accuracy against the seeded ground
truth, and the honest exception list -- Track 4's bar verbatim: "throughput plus measured accuracy
plus an honest exception list. One cherry-picked match proves nothing." The batch is deliberately
NOT all clean matches (see data/generate_reconciliation_seed.py for the seeded distribution)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from reconciliation.reconciliation import reconcile, load_orders, load_settlements, reconcile_from_disk, load_live_orders_and_settlements
from audit.audit_log import set_source

set_source("batch_test")  # keeps this run out of the dashboard's live-activity stats

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def run():
    orders = load_orders()
    settlements = load_settlements()

    # Classification accuracy is measured ONLY against the synthetic seed, which is the only
    # data with a known-in-advance ground truth (_expected_reconciliation_status) -- a live
    # ledger entry's "correct" classification was never scripted, so folding it into this
    # accuracy number would silently overstate what's actually been verified. See
    # reconciliation.reconcile_from_disk's docstring for the same reasoning.
    seed_only = reconcile(orders, settlements)
    exception_by_order = {e["order_id"]: e["type"] for e in seed_only["exceptions"] if e["type"] != "orphan_settlement"}
    correct = 0
    mismatches = []
    for order in orders:
        expected = order["_expected_reconciliation_status"]
        actual = exception_by_order.get(order["order_id"], "matched")
        if actual == expected:
            correct += 1
        else:
            mismatches.append({"order_id": order["order_id"], "expected": expected, "actual": actual})

    classification_accuracy = correct / len(orders) if orders else 0.0

    # The reported batch itself includes real transactions from guardrail/ledger.json (any
    # purchase that actually went through Guardrail, including AI-buyer purchases driven over
    # MCP) alongside the synthetic seed -- this is not two separate demos, it's one finance-ops
    # loop that closes over whatever the merchant actually did plus a fixed proof batch.
    result = reconcile_from_disk(include_live=True)
    live_orders, live_settlements = load_live_orders_and_settlements()

    summary = {
        "total_orders": result["total_orders"],
        "total_settlements": len(settlements) + len(live_settlements),
        "matched": result["matched"],
        "match_rate": result["match_rate"],
        "exception_counts": result["exception_counts"],
        "by_source": result["by_source"],
        "classification_accuracy_vs_ground_truth": classification_accuracy,
        "classification_accuracy_sample_size": len(orders),
        "classification_mismatches": mismatches,
        "exceptions": result["exceptions"],
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "reconciliation_batch_results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Reconciliation batch: {result['total_orders']} orders ({result['by_source']})")
    print(f"  matched: {result['matched']}/{result['total_orders']} ({result['match_rate']:.1%})")
    print(f"  exceptions by type: {result['exception_counts']}")
    print(f"  classification accuracy vs seeded ground truth (synthetic subset, n={len(orders)}): {classification_accuracy:.1%} ({correct}/{len(orders)})")
    if mismatches:
        for m in mismatches:
            print(f"  MISMATCH {m['order_id']}: expected={m['expected']} actual={m['actual']}")
    print("  exception list (every one the engine could not auto-resolve):")
    for e in result["exceptions"]:
        print(f"    [{e['type']}] ({e.get('source')}) {e['order_id']}: {e['reason']}")
    return summary


if __name__ == "__main__":
    run()
