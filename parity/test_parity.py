"""Acceptance tests for Parity (spec section 5)."""
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from parity.parity import check_price_fairness

PRICING_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "pricing_log.json")


def test_price_near_baseline_is_fair():
    r = check_price_fairness("p001", "c009", 1796)  # baseline ~1789, near-zero deviation
    assert r["verdict"] == "fair"


def test_price_below_baseline_explained_is_fair():
    r = check_price_fairness("p010", "c001", 895)  # c001 is gold tier; well below baseline (~1118.5)
    assert r["verdict"] == "fair"
    assert r["explained_by"] == "loyalty_tier_gold_discount"


def test_price_below_baseline_unexplained_is_flagged():
    r = check_price_fairness("p010", "c006", 860)  # seeded unfair, customer c006 has no loyalty tier
    assert r["verdict"] == "flagged"


def test_price_above_baseline_unexplained_is_flagged():
    r = check_price_fairness("p001", "c002", 2291)  # seeded unfair, well above baseline
    assert r["verdict"] == "flagged"


def test_legitimate_factor_does_not_justify_unbounded_discount():
    # c016 is first-time (a real legitimate factor), but 5 INR against a ~1118.5 baseline is a
    # 99%+ discount -- no genuine promo goes that far, so this must be flagged despite the
    # factor applying.
    r = check_price_fairness("p010", "c016", 5)
    assert r["verdict"] == "flagged"
    assert r["explained_by"] is None


def test_no_history_falls_back_to_catalog_price_not_blind_fair():
    # p026 has no pricing_log.json entries -- offered far above its actual catalog price
    # (899 INR) should now be flagged instead of unconditionally passing.
    r = check_price_fairness("p026", "c016", 3000)
    assert r["verdict"] == "flagged"
    assert r["comparison_baseline"] == 899


def test_no_history_at_catalog_price_is_fair():
    r = check_price_fairness("p026", "c016", 899)
    assert r["verdict"] == "fair"


def test_tier_caps_are_differentiated_silver_stricter_than_gold():
    # baseline for p010 is ~1118.5. A 35% discount is under the old flat 40% cap (would have
    # passed for ANY factor before this fix), but silver's own cap is now 30% -- this must be
    # flagged, while the same 35% discount for a gold customer (cap 50%) must stay fair.
    discounted_price = round(1118.5 * 0.65)  # ~35% below baseline
    r_silver = check_price_fairness("p010", "c003", discounted_price)  # c003 is silver
    assert r_silver["verdict"] == "flagged"
    r_gold = check_price_fairness("p010", "c001", discounted_price)  # c001 is gold
    assert r_gold["verdict"] == "fair"
    assert r_gold["explained_by"] == "loyalty_tier_gold_discount"


def test_multi_factor_customer_gets_the_most_generous_applicable_cap():
    # c016 is first-time only (cap 20%) in the real seed data; a synthetic gold+first-time
    # profile should be judged by gold's 50% cap, not first-time's stricter 20%, since the
    # customer genuinely qualifies for both.
    from parity import parity
    factor, cap = parity._best_factor_for_deviation({"loyalty_tier": "gold", "is_first_time": True})
    assert factor == "loyalty_tier_gold_discount"
    assert cap == 0.50


def test_recency_window_excludes_stale_timestamped_entries():
    import time
    from parity import parity
    now = time.time()
    entries = [
        {"offered_price_inr": 1000, "timestamp": now - 200 * 86400},  # stale, outside window
        {"offered_price_inr": 1000, "timestamp": now - 200 * 86400},
        {"offered_price_inr": 1500, "timestamp": now - 10 * 86400},   # recent
        {"offered_price_inr": 1550, "timestamp": now - 5 * 86400},    # recent
    ]
    assert sorted(parity._recency_filtered_prices(entries)) == [1500, 1550]


def test_recency_window_does_not_affect_untimestamped_seed_data():
    # None of today's real pricing_log.json entries carry a timestamp -- this must be a
    # complete no-op for them, or every existing ground-truth case could silently shift.
    from parity import parity
    entries = [{"offered_price_inr": 1000}, {"offered_price_inr": 2000}]
    assert parity._recency_filtered_prices(entries) == [1000, 2000]


def test_full_ground_truth_recall_and_false_positive_rate():
    log = json.load(open(PRICING_LOG_PATH, encoding="utf-8"))
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
    recall = tp / (tp + fn) if (tp + fn) else None
    fp_rate = fp / (fp + tn) if (fp + tn) else None
    print(f"recall={recall:.3f} ({tp}/{tp+fn}) false_positive_rate={fp_rate:.3f} ({fp}/{fp+tn})")
    assert recall >= 0.85
    assert fp_rate <= 0.05


if __name__ == "__main__":
    test_price_near_baseline_is_fair()
    test_price_below_baseline_explained_is_fair()
    test_price_below_baseline_unexplained_is_flagged()
    test_price_above_baseline_unexplained_is_flagged()
    test_legitimate_factor_does_not_justify_unbounded_discount()
    test_no_history_falls_back_to_catalog_price_not_blind_fair()
    test_no_history_at_catalog_price_is_fair()
    test_tier_caps_are_differentiated_silver_stricter_than_gold()
    test_multi_factor_customer_gets_the_most_generous_applicable_cap()
    test_recency_window_excludes_stale_timestamped_entries()
    test_recency_window_does_not_affect_untimestamped_seed_data()
    test_full_ground_truth_recall_and_false_positive_rate()
    print("All Parity tests passed.")
