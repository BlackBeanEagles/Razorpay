"""Runs Guardrail's 6 acceptance cases plus ~16 additional varied purchase attempts."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from guardrail import guardrail
from guardrail.razorpay_client import _MOCK_ORDERS
from audit.audit_log import set_source

set_source("batch_test")  # keeps this run out of the dashboard's live-activity stats

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
# Isolated from the real guardrail/mandates.json and guardrail/ledger.json -- reset_mandate_store()
# below rebuilds the mandate store from seed fixtures, discarding anything not in that seed. Run
# against the real store, that would wipe any actual in-progress mandate (a real customer's, or an
# external AI buyer's mid-checkout purchase over MCP) every time this batch runs. See
# guardrail.use_isolated_store's docstring -- this happened for real during development.
MANDATE_PATH = os.path.join(os.path.dirname(__file__), "results", "_isolated_guardrail_batch_mandates.json")
LEDGER_PATH = os.path.join(os.path.dirname(__file__), "results", "_isolated_guardrail_batch_ledger.json")
guardrail.use_isolated_store(MANDATE_PATH, LEDGER_PATH)


def _reset():
    guardrail.reset_mandate_store()
    if os.path.exists(LEDGER_PATH):
        os.remove(LEDGER_PATH)
    _MOCK_ORDERS.clear()


# Each case: (case_id, mandate_id, product_id, amount_inr, simulate_mismatch, expected_status)
CASES = [
    # --- 6 acceptance criteria (spec section 6) ---
    ("acc1_clean_success", "m_default", "p001", 1799, False, "success"),
    ("acc2_exceeds_mandate", "m_default", "p007", 4499, False, "blocked"),
    ("acc3_expired_mandate", "m_expired", "p001", 1799, False, "blocked"),
    ("acc4_single_use_reused", "m_single_use_spent", "p001", 1799, False, "blocked"),
    ("acc5_settlement_mismatch", "m_default", "p010", 1199, True, "failed_verification"),
    # acc6 (cumulative) handled separately below, it's a 2-step case.

    # --- additional varied cases ---
    ("v01_clean_small", "m_default", "p009", 399, False, "success"),
    ("v02_clean_edge_at_limit", "m_default", "p006", 2000, False, "success"),
    ("v03_over_by_one_rupee", "m_default", "p015", 2001, False, "blocked"),
    ("v04_clean_mid", "m_default", "p005", 1299, False, "success"),
    ("v05_clean_mid2", "m_default", "p013", 799, False, "success"),
    ("v06_over_mandate_big", "m_default", "p003", 3499, False, "blocked"),
    ("v07_expired_small_amount", "m_expired", "p012", 299, False, "blocked"),
    ("v08_single_use_zero_amount_still_blocked", "m_single_use_spent", "p012", 299, False, "blocked"),
    ("v09_mismatch_small", "m_default", "p008", 899, True, "failed_verification"),
    ("v10_mismatch_mid", "m_default", "p004", 1999, True, "failed_verification"),
    ("v11_clean_charger", "m_default", "p010", 1199, False, "success"),
    ("v12_clean_stand", "m_default", "p015", 1099, False, "success"),
    ("v13_over_mandate_smartwatch_pro", "m_default", "p007", 4499, False, "blocked"),
    ("v14_clean_cable", "m_default", "p012", 299, False, "success"),
    ("v15_over_by_headphones", "m_default", "p004", 2999, False, "blocked"),
    ("v16_mismatch_earbuds", "m_default", "p002", 999, True, "failed_verification"),
]


def run():
    rows = []
    for case_id, mandate_id, product_id, amount, mismatch, expected in CASES:
        _reset()
        token = guardrail.get_mandate_token(mandate_id)
        r = guardrail.execute_purchase(token, product_id, amount, simulate_settlement_mismatch=mismatch)
        rows.append({
            "case_id": case_id, "mandate_id": mandate_id, "product_id": product_id, "amount_inr": amount,
            "simulate_mismatch": mismatch, "expected_status": expected, "actual_status": r["status"],
            "correct": r["status"] == expected, "reason": r["reason"],
        })

    # Cumulative spend case (acc6): two sequential purchases on one mandate.
    _reset()
    token1 = guardrail.get_mandate_token("m_cumulative_test")
    r1 = guardrail.execute_purchase(token1, "p001", 1799)
    token2 = guardrail.get_mandate_token("m_cumulative_test")
    r2 = guardrail.execute_purchase(token2, "p006", 2499)
    rows.append({"case_id": "acc6_cumulative_first", "mandate_id": "m_cumulative_test", "product_id": "p001",
                 "amount_inr": 1799, "simulate_mismatch": False, "expected_status": "success",
                 "actual_status": r1["status"], "correct": r1["status"] == "success", "reason": r1["reason"]})
    rows.append({"case_id": "acc6_cumulative_second", "mandate_id": "m_cumulative_test", "product_id": "p006",
                 "amount_inr": 2499, "simulate_mismatch": False, "expected_status": "blocked",
                 "actual_status": r2["status"], "correct": r2["status"] == "blocked", "reason": r2["reason"]})

    total = len(rows)
    correct = sum(r["correct"] for r in rows)
    status_counts = {}
    for r in rows:
        status_counts[r["actual_status"]] = status_counts.get(r["actual_status"], 0) + 1

    block_reasons = {}
    for r in rows:
        if r["actual_status"] == "blocked":
            key = "expired" if "expired" in r["reason"].lower() else \
                  "single_use" if "single-use" in r["reason"].lower() else \
                  "mandate_exceeded" if "exceed" in r["reason"].lower() else "other"
            block_reasons[key] = block_reasons.get(key, 0) + 1

    reconciled = [r for r in rows if r["actual_status"] in ("success", "failed_verification")]
    reconciliation_match_rate = (
        sum(1 for r in reconciled if r["actual_status"] == "success") / len(reconciled) if reconciled else None
    )

    summary = {
        "total_cases": total,
        "correct": correct,
        "accuracy": correct / total,
        "status_breakdown": status_counts,
        "block_reason_breakdown": block_reasons,
        "reconciliation_match_rate": reconciliation_match_rate,
        "rows": rows,
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "guardrail_batch_results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Guardrail batch: {correct}/{total} correct ({summary['accuracy']:.1%})")
    print(f"  status breakdown: {status_counts}")
    print(f"  block reasons: {block_reasons}")
    for r in rows:
        if not r["correct"]:
            print(f"  MISMATCH {r['case_id']}: expected={r['expected_status']} actual={r['actual_status']}")
    return summary


if __name__ == "__main__":
    run()
