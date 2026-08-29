"""Runs the upsell engine against every product in the catalog and reports coverage: what
fraction of products get a real, grounded complementary suggestion, and an honest list of the
ones that don't (uncategorized-for-upsell products, not silently skipped)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from growth.upsell import suggest_complementary, _load_catalog
from audit.audit_log import set_source

set_source("batch_test")  # keeps this run out of the dashboard's live-activity stats

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def run():
    catalog = _load_catalog()
    results = [suggest_complementary(p["product_id"]) for p in catalog]

    with_suggestions = [r for r in results if r["suggestions"]]
    without_suggestions = [
        {"product_id": r["source_product_id"], "reason": r["reason"]}
        for r in results if not r["suggestions"]
    ]

    summary = {
        "total_products": len(catalog),
        "with_suggestions": len(with_suggestions),
        "coverage_rate": len(with_suggestions) / len(catalog) if catalog else 0.0,
        "products_without_suggestions": without_suggestions,
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, "upsell_batch_results.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Upsell batch: {summary['with_suggestions']}/{summary['total_products']} products got a real suggestion ({summary['coverage_rate']:.1%})")
    if without_suggestions:
        print("  products with no complementary category mapped:")
        for p in without_suggestions:
            print(f"    {p['product_id']}: {p['reason']}")
    return summary


if __name__ == "__main__":
    run()
