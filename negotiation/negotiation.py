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
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from parity.parity import check_price_fairness, get_baseline_and_factor, DEVIATION_THRESHOLD
from audit.audit_log import log_event
from api.file_lock import file_lock

MAX_ROUNDS = 3

_ROUNDS_PATH = os.path.join(os.path.dirname(__file__), "negotiation_rounds.json")
_ROUNDS_LOCK_PATH = _ROUNDS_PATH + ".lock"


def _round_key(product_id: str, customer_id: str) -> str:
    return f"{product_id}|{customer_id}"


def _load_rounds() -> dict:
    try:
        with open(_ROUNDS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_rounds(store: dict) -> None:
    tmp_path = f"{_ROUNDS_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp_path, _ROUNDS_PATH)


def _reserve_round(product_id: str, customer_id: str, caller_round_number: int) -> int:
    """Returns the round number to actually enforce MAX_ROUNDS against. This closes a real gap
    where caller_round_number alone (an ordinary function argument, not authenticated state) was
    the ONLY thing enforcing "never haggles forever": a caller that always sends round_number=1
    got a fresh, uncapped counter/reject computation -- with its own file reads (pricing log,
    customer profiles, catalog) -- every single call, forever.

    Tracks a persistent per-(product_id, customer_id) attempt count, incremented on every call,
    reset to 0 whenever the PREVIOUS call for that same key ended the negotiation (see
    _mark_resolved) -- so a genuinely new negotiation for the same pair still gets a fresh
    MAX_ROUNDS budget, exactly like before this existed. The round enforced is
    max(caller_round_number, server_tracked_count): an honest caller's own round_number is still
    respected (still enough on its own to trigger an early reject), but an adversarial caller
    that keeps re-sending round_number=1 mid-negotiation can no longer dodge the cap that way."""
    key = _round_key(product_id, customer_id)
    with file_lock(_ROUNDS_LOCK_PATH):
        store = _load_rounds()
        record = store.get(key)
        prior_count = record["count"] if record and not record.get("resolved", True) else 0
        new_count = prior_count + 1
        store[key] = {"count": new_count, "resolved": False}
        _save_rounds(store)
    return max(caller_round_number, new_count)


def _mark_resolved(product_id: str, customer_id: str) -> None:
    """Called once propose_price has decided this call's verdict is "accept" or "reject" (the
    negotiation is over) -- the next call for this same (product_id, customer_id) pair should
    start a fresh MAX_ROUNDS budget rather than inheriting this one's count."""
    key = _round_key(product_id, customer_id)
    with file_lock(_ROUNDS_LOCK_PATH):
        store = _load_rounds()
        if key in store:
            store[key]["resolved"] = True
            _save_rounds(store)


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
    # The round actually enforced below is server-tracked, not just whatever round_number the
    # caller happened to send -- see _reserve_round's docstring for why trusting the caller's
    # own count alone let "never haggles forever" be dodged by a caller that just kept resending
    # round_number=1.
    round_number = _reserve_round(product_id, customer_id, round_number)
    log_input = {"product_id": product_id, "customer_id": customer_id, "offered_price_inr": offered_price_inr, "round": round_number}

    if round_number > MAX_ROUNDS:
        result = {"verdict": "reject", "round": round_number,
                   "reason": f"Negotiation ended after {MAX_ROUNDS} rounds without an agreement."}
        log_event("negotiation", "propose_price", log_input, result, "blocked")
        _mark_resolved(product_id, customer_id)
        return result

    fairness = check_price_fairness(product_id, customer_id, offered_price_inr)
    if fairness["verdict"] == "fair":
        result = {"verdict": "accept", "agreed_price_inr": offered_price_inr, "round": round_number,
                   "reason": f"Offer accepted -- {fairness['reason']}"}
        log_event("negotiation", "propose_price", log_input, result, "ok")
        _mark_resolved(product_id, customer_id)
        return result

    floor, baseline = _floor_price(product_id, customer_id)
    if floor is None:
        # No baseline to negotiate against -- check_price_fairness itself would have reported
        # this exact case as "fair" (nothing to compare against), so this branch is a defensive,
        # honestly-labeled fallback rather than something assumed unreachable.
        result = {"verdict": "accept", "agreed_price_inr": offered_price_inr, "round": round_number,
                   "reason": "No pricing baseline exists for this product -- accepted as offered."}
        log_event("negotiation", "propose_price", log_input, result, "ok")
        _mark_resolved(product_id, customer_id)
        return result

    if offered_price_inr >= floor:
        # Fairness said "not fair" (e.g. slightly above the tight fair band) but the offer is
        # still at/above our own floor -- no principled reason to counter an offer this good.
        result = {"verdict": "accept", "agreed_price_inr": offered_price_inr, "round": round_number,
                   "reason": "Offer is already at or above our floor price."}
        log_event("negotiation", "propose_price", log_input, result, "ok")
        _mark_resolved(product_id, customer_id)
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
