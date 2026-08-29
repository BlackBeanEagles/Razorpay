"""Parity: pricing fairness check. check_price_fairness(product_id, customer_id, offered_price_inr)."""
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from audit.audit_log import log_event

PRICING_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "pricing_log.json")
CUSTOMER_PROFILES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "customer_profiles.json")
CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "catalog.json")

DEVIATION_THRESHOLD = 0.095  # >9.5% deviation from baseline triggers a closer look

# A legitimate factor (loyalty tier, first-time promo) justifies SOME discount, but not an
# unbounded one -- without a cap, a first-time customer offered a product for 1 INR would
# still pass as "fair" purely because the deviation is negative and a factor exists. Caps are
# tier-differentiated -- a gold-tier loyalty discount is a stronger, more established
# relationship than a one-off first-time promo, so it reasonably earns more room. The real
# seeded gold-tier discount case in this dataset is ~20% below baseline, so 50% leaves
# generous headroom there while still catching near-zero/abusive pricing at every tier.
LEGITIMATE_FACTOR_CAPS = {
    "loyalty_tier_gold_discount": 0.50,
    "loyalty_tier_silver_discount": 0.30,
    "first_time_customer_promo": 0.20,
}

# How far back (in days) a pricing_log entry counts toward the baseline, when it carries a
# "timestamp" field -- a price offered a year ago shouldn't weigh the same as one offered
# last week when deciding what "normal" looks like today. Entries without a timestamp (all
# of today's seed data) are unaffected and count equally, exactly as before this existed --
# this only activates once real timestamped history accumulates.
RECENCY_WINDOW_DAYS = 90


def _load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _customer_profile(customer_id: str, profiles: list) -> dict | None:
    for c in profiles:
        if c["customer_id"] == customer_id:
            return c
    return None


def _applicable_factors_for_customer(profile: dict) -> list:
    """Every legitimate discount factor this customer's profile qualifies for -- a customer
    can qualify for more than one (e.g. a first-time gold-tier customer), and each carries its
    own discount cap."""
    if profile is None:
        return []
    factors = []
    if profile.get("loyalty_tier") == "gold":
        factors.append("loyalty_tier_gold_discount")
    if profile.get("loyalty_tier") == "silver":
        factors.append("loyalty_tier_silver_discount")
    if profile.get("is_first_time"):
        factors.append("first_time_customer_promo")
    return factors


def _best_factor_for_deviation(profile: dict):
    """The most generous applicable factor -- if a customer qualifies for more than one, the
    one with the highest cap is what should justify (or fail to justify) the discount, not
    whichever happened to be checked first. Returns (factor_name, cap) or (None, 0)."""
    factors = _applicable_factors_for_customer(profile)
    if not factors:
        return None, 0
    best = max(factors, key=lambda f: LEGITIMATE_FACTOR_CAPS[f])
    return best, LEGITIMATE_FACTOR_CAPS[best]


def _recency_filtered_prices(entries: list) -> list:
    """Prices to compute the baseline from. If entries carry a "timestamp" (unix epoch), only
    the last RECENCY_WINDOW_DAYS count -- a genuine price change over time shouldn't be
    dragged toward stale, no-longer-representative history. Entries without a timestamp (all
    pre-existing seed data) all count equally, same as before recency-awareness existed,
    since there's no time information available to filter by."""
    timestamped = [e for e in entries if e.get("timestamp") is not None]
    if not timestamped:
        return [e["offered_price_inr"] for e in entries]
    cutoff = time.time() - RECENCY_WINDOW_DAYS * 86400
    recent = [e for e in timestamped if e["timestamp"] >= cutoff]
    return [e["offered_price_inr"] for e in (recent or timestamped)]


def check_price_fairness(product_id: str, customer_id: str, offered_price_inr: int) -> dict:
    pricing_log = _load(PRICING_LOG_PATH)
    profiles = _load(CUSTOMER_PROFILES_PATH)

    product_entries = [e for e in pricing_log if e["product_id"] == product_id]
    product_prices = _recency_filtered_prices(product_entries)

    using_catalog_fallback = False
    if product_prices:
        baseline = statistics.median(product_prices)
    else:
        # No purchase history yet -- rather than skipping the check entirely (which would let
        # a brand-new product be offered at literally any price with zero scrutiny), fall back
        # to the product's own listed catalog price as the comparison baseline.
        catalog = _load(CATALOG_PATH)
        catalog_entry = next((p for p in catalog if p["product_id"] == product_id), None)
        if catalog_entry is None:
            result = {
                "verdict": "fair",
                "reason": "No pricing history or catalog listing exists for this product; nothing to compare against.",
                "comparison_baseline": None,
                "explained_by": None,
            }
            log_event("parity", "fairness_check",
                       {"product_id": product_id, "customer_id": customer_id, "offered_price_inr": offered_price_inr},
                       result, "ok")
            return result
        baseline = catalog_entry["price_inr"]
        using_catalog_fallback = True

    deviation = (offered_price_inr - baseline) / baseline if baseline else 0
    baseline_label = "the product's listed catalog price" if using_catalog_fallback else f"the baseline median ({baseline})"

    profile = _customer_profile(customer_id, profiles)
    expected_factor, factor_cap = _best_factor_for_deviation(profile)

    if abs(deviation) <= DEVIATION_THRESHOLD:
        result = {
            "verdict": "fair",
            "reason": f"Offered price {offered_price_inr} is within {DEVIATION_THRESHOLD:.0%} of {baseline_label}.",
            "comparison_baseline": baseline,
            "explained_by": None,
        }
    elif deviation < 0 and expected_factor is not None and abs(deviation) <= factor_cap:
        result = {
            "verdict": "fair",
            "reason": f"Offered price {offered_price_inr} is below baseline ({baseline}), justified by {expected_factor} for this customer.",
            "comparison_baseline": baseline,
            "explained_by": expected_factor,
        }
    elif deviation < 0 and expected_factor is not None:
        result = {
            "verdict": "flagged",
            "reason": (
                f"Offered price {offered_price_inr} is {abs(deviation):.1%} below baseline ({baseline}) -- "
                f"{expected_factor} applies for this customer, but no legitimate factor justifies a discount "
                f"this large (cap: {factor_cap:.0%})."
            ),
            "comparison_baseline": baseline,
            "explained_by": None,
        }
    elif deviation < 0:
        result = {
            "verdict": "flagged",
            "reason": f"Offered price {offered_price_inr} is {abs(deviation):.1%} below baseline ({baseline}) with no legitimate factor in this customer's profile to justify a discount.",
            "comparison_baseline": baseline,
            "explained_by": None,
        }
    else:
        result = {
            "verdict": "flagged",
            "reason": f"Offered price {offered_price_inr} is {deviation:.1%} above baseline ({baseline}), which no legitimate factor justifies (loyalty/first-time factors only justify lower prices, not higher).",
            "comparison_baseline": baseline,
            "explained_by": None,
        }

    log_event("parity", "fairness_check",
               {"product_id": product_id, "customer_id": customer_id, "offered_price_inr": offered_price_inr},
               result, "ok" if result["verdict"] == "fair" else "flagged")
    return result
