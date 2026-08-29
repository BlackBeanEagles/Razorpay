"""Wires Shelf, Parity, and Guardrail as tools for the buying agent (BUILD_SPEC.md section 7).

run_chat_turn() is the orchestration entry point the API layer (/api/chat) calls: it runs
Shelf -> Parity -> Guardrail in order for one chat message, and returns a natural-language
reply plus structured pipeline_trace/product_card/purchase_result data for the frontend.

The reply is composed with plain templates -- this deterministic path is the fallback used
when GROQ_API_KEY isn't set (see api/routes/chat.py); the real LLM tool-calling agent lives in
agent/llm_agent.py (Groq, OpenAI-compatible tool-calling API) and is what /api/chat actually
uses whenever a real key is configured. Both call the exact same underlying Shelf/Parity/
Guardrail functions -- only who decides which to call, and in what order, differs.

Run standalone (`python agent/agent.py`) to process every intent in intents.json without an
API server, for a quick terminal demo.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shelf.shelf import search_catalog
from parity.parity import check_price_fairness
from guardrail import guardrail

INTENTS_PATH = os.path.join(os.path.dirname(__file__), "intents.json")

_BUDGET_PATTERNS = [
    re.compile(r"under\s*(?:rs\.?|inr|₹)?\s*(\d{2,6})", re.I),
    re.compile(r"budget\s*(?:of)?\s*(?:rs\.?|inr|₹)?\s*(\d{2,6})", re.I),
    re.compile(r"(?:less than|below)\s*(?:rs\.?|inr|₹)?\s*(\d{2,6})", re.I),
]


def parse_budget_inr(message: str):
    for pattern in _BUDGET_PATTERNS:
        m = pattern.search(message)
        if m:
            return int(m.group(1))
    return None


def run_chat_turn(message: str, customer_id: str, mandate_id: str) -> dict:
    """Shelf -> Parity -> Guardrail for one chat message. Returns the /api/chat response shape.

    The Guardrail step uses resolve_purchase(), which hands off to a real, human-verified
    Razorpay Checkout when real credentials are configured (status "checkout_required" --
    purchase_result stays None, "checkout" carries what the frontend needs to open Checkout;
    the turn finishes for real once the frontend calls /api/purchase/confirm), or completes
    immediately via the automated mock flow otherwise."""
    trace = []
    product_card = None
    purchase_result = None
    checkout = None
    budget = parse_budget_inr(message)

    shelf_result = search_catalog(message, budget)
    if not shelf_result["matches"]:
        trace.append({"stage": "shelf", "status": "no_match", "detail": shelf_result})
        reply = f"I couldn't find a matching product. {shelf_result['no_match_reason']}"
        return {"agent_reply": reply, "pipeline_trace": trace, "product_card": None, "purchase_result": None, "purchase_results": [], "checkout": None}

    if len(shelf_result["matches"]) > 1:
        # No exact match -- Shelf fell back to typo/partial-word tolerant matching and isn't
        # confident enough to pick just one. Surface the range and stop here rather than
        # guessing which one to buy; the customer needs to say which they meant.
        trace.append({"stage": "shelf", "status": "ambiguous", "detail": shelf_result})
        options = "; ".join(f"{m['name']} (Rs.{m['price_inr']})" for m in shelf_result["matches"])
        reply = f"I didn't find an exact match, but a few things look close: {options}. Which one did you mean?"
        return {"agent_reply": reply, "pipeline_trace": trace, "product_card": None, "purchase_result": None, "purchase_results": [], "checkout": None}

    product = shelf_result["matches"][0]
    trace.append({"stage": "shelf", "status": "ok", "detail": shelf_result})
    product_card = {
        "product_id": product["product_id"], "name": product["name"],
        "price_inr": product["price_inr"], "rating": product["rating"], "reason": product["reason"],
    }

    fairness = check_price_fairness(product["product_id"], customer_id, product["price_inr"])
    trace.append({"stage": "parity", "status": fairness["verdict"], "detail": fairness})
    if fairness["verdict"] == "flagged":
        reply = (
            f"I found {product['name']} at Rs.{product['price_inr']}, but the price looks inconsistent "
            f"with what similar customers were offered -- flagged for review. {fairness['reason']} "
            "I've stopped here rather than proceeding with a purchase."
        )
        return {"agent_reply": reply, "pipeline_trace": trace, "product_card": product_card, "purchase_result": None, "purchase_results": [], "checkout": None}

    try:
        # requesting_customer_id enforces ownership -- this customer can only spend against a
        # mandate they own, or a shared/demo mandate (owner None, e.g. m_default).
        token = guardrail.get_mandate_token(mandate_id, requesting_customer_id=customer_id)
    except KeyError:
        reply = f"I found {product['name']} at Rs.{product['price_inr']}, but mandate '{mandate_id}' doesn't exist."
        trace.append({"stage": "guardrail", "status": "blocked", "detail": {"reason": "unknown mandate_id"}})
        return {"agent_reply": reply, "pipeline_trace": trace, "product_card": product_card, "purchase_result": None, "purchase_results": [], "checkout": None}

    purchase = guardrail.resolve_purchase(token, product["product_id"], product["price_inr"], requesting_customer_id=customer_id)
    trace.append({"stage": "guardrail", "status": purchase["status"], "detail": purchase})

    if purchase["status"] == "checkout_required":
        checkout = purchase["checkout"]
        reply = (
            f"I found {product['name']} at Rs.{product['price_inr']} and it's within your mandate -- "
            "complete payment via Razorpay to finish the purchase."
        )
    elif purchase["status"] == "success":
        purchase_result = purchase
        reply = (
            f"Done -- bought {product['name']} for Rs.{product['price_inr']}. "
            f"Verified against Razorpay (order {purchase['razorpay_order_id']}); charged, expected, "
            "and settled amounts all matched."
        )
    elif purchase["status"] == "blocked":
        purchase_result = purchase
        reply = f"I found {product['name']} at Rs.{product['price_inr']}, but the purchase was blocked: {purchase['reason']}"
    else:  # failed_verification
        purchase_result = purchase
        reply = (
            f"I found {product['name']} at Rs.{product['price_inr']} and forwarded the purchase, but "
            f"verification failed -- not reporting success. {purchase['reason']}"
        )

    return {"agent_reply": reply, "pipeline_trace": trace, "product_card": product_card,
            "purchase_result": purchase_result, "purchase_results": [purchase_result] if purchase_result else [],
            "checkout": checkout}


def run_chat_turn_stream(message: str, customer_id: str, mandate_id: str):
    """Generator wrapper around the deterministic run_chat_turn, matching llm_agent.py's
    streaming event shape ({"type": "stage", ...} then one {"type": "final"|"checkout", ...})
    -- used as the SSE fallback when GROQ_API_KEY isn't set, so /api/chat/stream behaves
    consistently either way. The deterministic path has no real per-step latency to stream, so
    all stage events arrive together followed immediately by the closing one."""
    result = run_chat_turn(message, customer_id, mandate_id)
    for stage_event in result["pipeline_trace"]:
        yield {"type": "stage", **stage_event}
    if result["checkout"]:
        yield {"type": "checkout", "agent_reply": result["agent_reply"], "checkout": result["checkout"],
               "pipeline_trace": result["pipeline_trace"], "purchase_results": result["purchase_results"]}
    else:
        yield {
            "type": "final",
            "agent_reply": result["agent_reply"],
            "pipeline_trace": result["pipeline_trace"],
            "product_card": result["product_card"],
            "purchase_result": result["purchase_result"],
            "purchase_results": result["purchase_results"],
        }


def run_all_intents():
    # Isolated from the real guardrail/mandates.json and guardrail/ledger.json -- this is a
    # standalone `python agent/agent.py` CLI demo, run directly by a person, not through the
    # API server. reset_mandate_store() rebuilds the mandate store from seed fixtures,
    # discarding anything not in that seed -- against the real store, running this demo would
    # wipe any actual in-progress mandate (a real customer's, or an external AI buyer's
    # mid-checkout purchase over MCP). See guardrail.use_isolated_store's docstring -- exactly
    # this class of bug was found and fixed in every other test/batch entry point this session;
    # this standalone demo runner had the same exposure and was the last one still open to it.
    mandate_path = os.path.join(os.path.dirname(__file__), "_isolated_demo_mandates.json")
    ledger_path = os.path.join(os.path.dirname(__file__), "_isolated_demo_ledger.json")
    guardrail.use_isolated_store(mandate_path, ledger_path)
    guardrail.reset_mandate_store()
    with open(INTENTS_PATH, encoding="utf-8") as f:
        intents = json.load(f)
    results = []
    for intent in intents:
        r = run_chat_turn(intent["query_text"], intent["customer_id"], intent["mandate_id"])
        outcome = r["purchase_result"]["status"] if r["purchase_result"] else \
                  ("flagged" if any(t["stage"] == "parity" and t["status"] == "flagged" for t in r["pipeline_trace"]) else "no_match")
        print(f"[{intent['intent_id']}] \"{intent['query_text']}\" -> {outcome}")
        results.append(r)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="Use the real LLM tool-calling agent instead of this deterministic runner.")
    args = parser.parse_args()

    if args.llm:
        raise SystemExit(
            "The real LLM agent (agent/llm_agent.py, Groq-based) doesn't have a standalone batch-intents "
            "runner like this one -- it's driven by chat turns via /api/chat, not a fixed intents.json list. "
            "Run without --llm for this deterministic pipeline demo, or hit /api/chat with GROQ_API_KEY set "
            "to use the real agent."
        )

    run_all_intents()
