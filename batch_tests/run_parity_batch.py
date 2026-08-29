"""Runs Parity against the full 68-entry pricing_log.json; reports recall and false-positive rate."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from parity.parity import check_price_fairness
from audit.audit_log import set_source

set_source("batch_test")  # keeps this run out of the dashboard's live-activity stats

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def run():
    with open(os.path.join(DATA_DIR, "pricing_log.json"), encoding="utf-8") as f:
        log = json.load(f)

    rows = []
    tp = fn = fp = tn = 0
    for e in log:
        r = check_price_fairness(e["product_id"], e["customer_id"], e["offered_price_inr"])
        flagged = r["verdict"] == "flagged"
        if e["is_seeded_unfair"]:
            tp += flagged
            fn += not flagged
        else:
            fp += flagged
            tn += not flagged
        rows.append({"entry_id": e["entry_id"], "product_id": e["product_id"], "customer_id": e["customer_id"],
                      "offered_price_inr": e["offered_price_inr"], "is_seeded_unfair": e["is_seeded_unfair"],
                      "verdict": r["verdict"], "reason": r["reason"]})

    recall = tp / (tp + fn) if (tp + fn) else None
    false_positive_rate = fp / (fp + tn) if (fp + tn) else None

    summary = {
        "total_entries": len(log),
        "seeded_unfair_count": tp + fn,
        "legitimate_count": fp + tn,
        "true_positives": tp,
        "false_negatives": fn,
        "false_positives": fp,
        "true_negatives": tn,
        "recall": recall,
        "false_positive_rate": false_positive_rate,
        "rows": rows,
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "parity_batch_results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Parity batch: recall={recall:.1%} ({tp}/{tp+fn} seeded-unfair caught), "
          f"false_positive_rate={false_positive_rate:.1%} ({fp}/{fp+tn} legitimate flagged)")
    return summary


if __name__ == "__main__":
    run()
