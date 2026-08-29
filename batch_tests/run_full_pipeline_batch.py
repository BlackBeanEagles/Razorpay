"""Runs the full Shelf -> Parity -> Guardrail pipeline against all 25 test_intents.json entries."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shelf.shelf import search_catalog
from parity.parity import check_price_fairness
from guardrail import guardrail
from guardrail.razorpay_client import _MOCK_ORDERS
from audit.audit_log import set_source

set_source("batch_test")  # keeps this run out of the dashboard's live-activity stats

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
# Isolated from the real guardrail/mandates.json and guardrail/ledger.json -- see
# guardrail.use_isolated_store's docstring and run_guardrail_batch.py's identical isolation for why.
MANDATE_PATH = os.path.join(os.path.dirname(__file__), "results", "_isolated_full_pipeline_batch_mandates.json")
LEDGER_PATH = os.path.join(os.path.dirname(__file__), "results", "_isolated_full_pipeline_batch_ledger.json")
guardrail.use_isolated_store(MANDATE_PATH, LEDGER_PATH)


def _reset():
    guardrail.reset_mandate_store()
    if os.path.exists(LEDGER_PATH):
        os.remove(LEDGER_PATH)
    _MOCK_ORDERS.clear()


def _run_one_leg(query, budget, customer_id, mandate_id, simulate_mismatch=False):
    """Shelf -> Parity -> Guardrail for a single buying intent. Returns a stage-by-stage trace."""
    trace = {"shelf": None, "parity": None, "guardrail": None, "final_stage_reached": None}

    shelf_result = search_catalog(query, budget)
    trace["shelf"] = shelf_result
    if not shelf_result["matches"]:
        trace["final_stage_reached"] = "shelf_no_match"
        return trace

    product = shelf_result["matches"][0]
    fairness = check_price_fairness(product["product_id"], customer_id, product["price_inr"])
    trace["parity"] = fairness
    if fairness["verdict"] == "flagged":
        trace["final_stage_reached"] = "parity_flagged"
        return trace

    token = guardrail.get_mandate_token(mandate_id)
    purchase = guardrail.execute_purchase(token, product["product_id"], product["price_inr"],
                                           simulate_settlement_mismatch=simulate_mismatch)
    trace["guardrail"] = purchase
    trace["final_stage_reached"] = f"guardrail_{purchase['status']}"
    return trace


def run():
    with open(os.path.join(DATA_DIR, "test_intents.json"), encoding="utf-8") as f:
        intents = json.load(f)

    rows = []
    funnel = {"shelf_no_match": 0, "parity_flagged": 0, "guardrail_success": 0,
              "guardrail_blocked": 0, "guardrail_failed_verification": 0}

    for intent in intents:
        _reset()
        simulate_mismatch = intent["intent_id"] == "i19"  # deliberately seeded mismatch case

        if intent["intent_id"] == "i18":
            # Two sequential purchases against the same cumulative mandate.
            leg1 = _run_one_leg(intent["query_text"], intent["stated_budget_inr"], intent["customer_id"], intent["mandate_id"])
            leg2 = _run_one_leg("smartwatch, any kind, whatever's cheapest", 3000, intent["customer_id"], intent["mandate_id"])
            # Both legs' outcomes must count toward the funnel too -- the "else" branch below
            # does this for every other (single-leg) intent; skipping it here silently left the
            # funnel breakdown under its true total (23 instead of 25 stage-outcomes).
            funnel[leg1["final_stage_reached"]] = funnel.get(leg1["final_stage_reached"], 0) + 1
            funnel[leg2["final_stage_reached"]] = funnel.get(leg2["final_stage_reached"], 0) + 1
            outcome = f"{leg1['final_stage_reached']},{leg2['final_stage_reached']}"
            expected = intent["expected_guardrail_outcome"]
            correct = (leg1["final_stage_reached"] == "guardrail_success"
                       and leg2["final_stage_reached"] == "guardrail_blocked")
            trace = {"leg1": leg1, "leg2": leg2}
        else:
            trace = _run_one_leg(intent["query_text"], intent["stated_budget_inr"], intent["customer_id"],
                                  intent["mandate_id"], simulate_mismatch=simulate_mismatch)
            outcome = trace["final_stage_reached"]
            funnel[outcome] = funnel.get(outcome, 0) + 1
            expected = intent["expected_guardrail_outcome"]
            if expected == "not_attempted":
                correct = outcome == "shelf_no_match"
            elif expected == "success":
                correct = outcome == "guardrail_success"
            elif expected == "failed_verification":
                correct = outcome == "guardrail_failed_verification"
            elif expected.startswith("blocked"):
                correct = outcome == "guardrail_blocked"
            else:
                correct = False

        rows.append({
            "intent_id": intent["intent_id"], "query": intent["query_text"], "expected": expected,
            "actual_outcome": outcome, "correct": correct, "notes": intent["notes"],
        })

    total = len(rows)
    correct_count = sum(r["correct"] for r in rows)

    summary = {
        "total_intents": total,
        "correct": correct_count,
        "accuracy": correct_count / total,
        "funnel": funnel,
        "rows": rows,
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "full_pipeline_batch_results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Full pipeline batch: {correct_count}/{total} correct ({summary['accuracy']:.1%})")
    print(f"  funnel: {funnel}")
    for r in rows:
        if not r["correct"]:
            print(f"  MISMATCH {r['intent_id']}: expected={r['expected']} actual={r['actual_outcome']}")
    return summary


if __name__ == "__main__":
    run()
