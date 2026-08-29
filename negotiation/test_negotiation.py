"""Acceptance tests for the agent-to-agent negotiation engine."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from negotiation.negotiation import propose_price, _floor_price, MAX_ROUNDS


def test_a_fair_offer_is_accepted_immediately_at_the_offered_price():
    r = propose_price("p001", 1799, "c999", round_number=1)
    assert r["verdict"] == "accept"
    assert r["agreed_price_inr"] == 1799  # never silently substituted


def test_a_lowball_offer_gets_a_principled_counter_not_a_flat_rejection():
    r = propose_price("p001", 1000, "c999", round_number=1)
    assert r["verdict"] == "counter"
    floor, baseline = _floor_price("p001", "c999")
    assert r["counter_price_inr"] >= floor  # never counters below its own fairness floor
    assert r["baseline_price_inr"] == round(baseline)


def test_offering_back_the_exact_quoted_counter_price_is_accepted():
    # The real bug found while building this: rounding the floor DOWN quoted a counter-price
    # that was itself a hair below the actual fairness floor, so accepting the counter by
    # offering back that exact number got countered again for the same price, forever.
    first = propose_price("p001", 1000, "c999", round_number=1)
    assert first["verdict"] == "counter"
    second = propose_price("p001", first["counter_price_inr"], "c999", round_number=2)
    assert second["verdict"] == "accept"
    assert second["agreed_price_inr"] == first["counter_price_inr"]


def test_gold_tier_customer_gets_a_lower_floor_than_no_factor():
    gold_floor, _ = _floor_price("p001", "c001")  # c001 is gold-tier
    none_floor, _ = _floor_price("p001", "c999")  # unknown customer, no factor
    assert gold_floor < none_floor


def test_gold_tier_offer_within_their_real_discount_cap_is_accepted_outright():
    r = propose_price("p001", 900, "c001", round_number=1)  # within gold's 50% cap
    assert r["verdict"] == "accept"
    assert r["agreed_price_inr"] == 900


def test_negotiation_never_concedes_past_max_rounds():
    r = propose_price("p001", 1, "c999", round_number=MAX_ROUNDS + 1)
    assert r["verdict"] == "reject"
    assert "reject" == r["verdict"]


def test_counter_price_is_never_below_the_baseline_times_deviation_threshold_for_no_factor_customers():
    # For a customer with no legitimate discount factor, the floor must never go below the
    # ordinary no-factor-needed fair band -- that would mean the merchant conceding a discount
    # nobody actually qualifies for, just to close a deal.
    from parity.parity import DEVIATION_THRESHOLD
    floor, baseline = _floor_price("p001", "c999")
    assert floor >= baseline * (1 - DEVIATION_THRESHOLD) - 1  # -1 for rounding slack


def test_an_offer_already_above_the_floor_is_accepted_even_if_parity_flags_it():
    # Edge case: an offer parity would technically flag (e.g. a hair above the tight fair band)
    # but that's still at or above this merchant's own computed floor -- no principled reason to
    # counter an offer that good.
    floor, baseline = _floor_price("p001", "c999")
    # An offer right at the floor should be accepted (matches the exact-counter-price test's
    # logic path, verified independently here at round 1 instead of round 2).
    import math
    r = propose_price("p001", math.ceil(floor), "c999", round_number=1)
    assert r["verdict"] == "accept"
