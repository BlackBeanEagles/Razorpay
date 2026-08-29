"""Runs Shelf's 5 acceptance cases plus the 25 test_intents.json queries; reports match rate."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shelf.shelf import search_catalog
from audit.audit_log import set_source

set_source("batch_test")  # keeps this run out of the dashboard's live-activity stats

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")

ACCEPTANCE_CASES = [
    {"case_id": "acc1_clear_match", "query": "wireless earbuds under 2000 rupees, prefer good ratings", "budget": 2000, "expected": "match:p001"},
    {"case_id": "acc2_multi_candidate", "query": "Get me the fitness smartwatch with GPS", "budget": 5000, "expected": "match:p007"},
    {"case_id": "acc3_no_match_below_cheapest", "query": "Get me a laptop stand under 1000 rupees", "budget": 1000, "expected": "no_match"},
    {"case_id": "acc4_out_of_stock", "query": "Get me a power bank, at least 20000mAh", "budget": 2000, "expected": "no_match"},
    {"case_id": "acc5_category_mismatch", "query": "Buy me a car", "budget": 2000, "expected": "no_match"},
]


def _actual_outcome(result: dict) -> str:
    if result["matches"]:
        return f"match:{result['matches'][0]['product_id']}"
    return "no_match"


def run():
    with open(os.path.join(DATA_DIR, "test_intents.json"), encoding="utf-8") as f:
        intents = json.load(f)

    rows = []
    for c in ACCEPTANCE_CASES:
        result = search_catalog(c["query"], c["budget"])
        actual = _actual_outcome(result)
        expected = c["expected"]
        correct = actual == expected if expected != "no_match" else actual == "no_match"
        rows.append({"case_id": c["case_id"], "query": c["query"], "expected": expected, "actual": actual,
                      "correct": correct, "no_match_reason": result["no_match_reason"]})

    for intent in intents:
        result = search_catalog(intent["query_text"], intent["stated_budget_inr"])
        actual = _actual_outcome(result)
        expected = intent["expected_shelf_outcome"].split(",")[0]  # i18 has a compound expectation
        correct = actual == expected if expected.startswith("match:") else actual == "no_match"
        rows.append({"case_id": intent["intent_id"], "query": intent["query_text"], "expected": expected,
                      "actual": actual, "correct": correct, "no_match_reason": result["no_match_reason"]})

    total = len(rows)
    correct_count = sum(r["correct"] for r in rows)
    summary = {
        "total_cases": total,
        "correct": correct_count,
        "match_rate": correct_count / total,
        "rows": rows,
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "shelf_batch_results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Shelf batch: {correct_count}/{total} correct ({summary['match_rate']:.1%})")
    for r in rows:
        if not r["correct"]:
            print(f"  MISMATCH {r['case_id']}: expected={r['expected']} actual={r['actual']} ({r['no_match_reason']})")
    return summary


if __name__ == "__main__":
    run()
