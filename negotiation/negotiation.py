"""Negotiation: agent-to-agent price haggling -- genuinely open territory for Track 1 (checked
against Razorpay's own Agentic Payments/Agent Studio/Agentic Platform docs before building this;
none of them cover an agent negotiating a price, only an agent triggering an already-approved
payment). Two real decision-makers reach a real, executable agreement: an AI buyer proposes a
price, and this module -- standing in for the merchant's own pricing agent -- accepts, counters,
or rejects, bounded the whole way by Parity's ALREADY-hardened fairness math, not a separately
invented negotiation rule that could drift from what fairness actually enforces.

The counter-offer is never arbitrary: it's exactly the lowest price parity.get_baseline_and_factor
says is still "fair" for this product/customer (their real discount-factor cap, or the no-factor-
needed fair band if they don't qualify for one) -- the merchant agent never concedes past its own
fairness rules just to close a deal, and never haggles forever (MAX_ROUNDS caps it, same "gated"
principle as everything else in this codebase).
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from parity.parity import check_price_fairness, get_baseline_and_factor, DEVIATION_THRESHOLD
from audit.audit_log import log_event

MAX_ROUNDS = 3


def _floor_price(product_id: str, customer_id: str):
    """The lowest price Parity would still call fair for this customer/product. Returns
    (floor_price, baseline), or (None, None) if there's no pricing history AND no catalog
    listing at all -- nothing to negotiate against."""
    info = get_baseline_and_factor(product_id, customer_id)
    if info is None:
        return None, None
    allowed_discount = info["factor_cap"] if info["factor"] else DEVIATION_THRESHOLD
    floor = info["baseline"] * (1 - allowed_discount)
    return floor, info["baseline"]


def propose_price(product_id: str, offered_price_inr: float, customer_id: str, round_number: int = 1) -> dict:
    """One round of negotiation for one product/customer.

    verdict "accept": the offer itself is genuinely fair (Parity agrees) -- agreed_price_inr is
      the buyer's own offer, never silently substituted for something else. Proceed straight to
      a real purchase at this price.
    verdict "counter": counter_price_inr is the real fairness floor for this product/customer --
      call propose_price again with a new offer (e.g. exactly counter_price_inr to accept it) to
      continue, incrementing round_number.
    verdict "reject": negotiation is over -- either MAX_ROUNDS was reached with no agreement, or
      there's no pricing baseline at all to negotiate against."""
    log_input = {"product_id": product_id, "customer_id": customer_id, "offered_price_inr": offered_price_inr, "round": round_number}

    if round_number > MAX_ROUNDS:
        result = {"verdict": "reject", "round": round_number,
                   "reason": f"Negotiation ended after {MAX_ROUNDS} rounds without an agreement."}
        log_event("negotiation", "propose_price", log_input, result, "blocked")
        return result

    fairness = check_price_fairness(product_id, customer_id, offered_price_inr)
    if fairness["verdict"] == "fair":
        result = {"verdict": "accept", "agreed_price_inr": offered_price_inr, "round": round_number,
                   "reason": f"Offer accepted -- {fairness['reason']}"}
        log_event("negotiation", "propose_price", log_input, result, "ok")
        return result

    floor, baseline = _floor_price(product_id, customer_id)
    if floor is None:
        # No baseline to negotiate against -- check_price_fairness itself would have reported
        # this exact case as "fair" (nothing to compare against), so this branch is a defensive,
        # honestly-labeled fallback rather than something assumed unreachable.
        result = {"verdict": "accept", "agreed_price_inr": offered_price_inr, "round": round_number,
                   "reason": "No pricing baseline exists for this product -- accepted as offered."}
        log_event("negotiation", "propose_price", log_input, result, "ok")
        return result

    if offered_price_inr >= floor:
        # Fairness said "not fair" (e.g. slightly above the tight fair band) but the offer is
        # still at/above our own floor -- no principled reason to counter an offer this good.
        result = {"verdict": "accept", "agreed_price_inr": offered_price_inr, "round": round_number,
                   "reason": "Offer is already at or above our floor price."}
        log_event("negotiation", "propose_price", log_input, result, "ok")
        return result

    # Rounded UP, not to nearest -- rounding a fractional floor DOWN (e.g. 1543.025 -> 1543)
    # would quote a counter-price that's a hair below the actual fairness floor, so a buyer who
    # takes the deal by offering back exactly that number would get countered again for the
    # exact same price, forever. Found by testing this exact scenario, not hypothetical.
    counter_price = math.ceil(floor)
    rounds_left = MAX_ROUNDS - round_number
    result = {
        "verdict": "counter", "counter_price_inr": counter_price, "round": round_number,
        "baseline_price_inr": round(baseline),
        "reason": (
            f"Rs.{offered_price_inr:.0f} is too far below what similar customers were charged "
            f"(baseline Rs.{round(baseline)}). The lowest we can go and still call it a fair "
            f"price is Rs.{counter_price}"
            + (f" -- {rounds_left} round{'s' if rounds_left != 1 else ''} left to agree." if rounds_left > 0 else " -- this is the final round.")
        ),
    }
    log_event("negotiation", "propose_price", log_input, result, "flagged")
    return result
