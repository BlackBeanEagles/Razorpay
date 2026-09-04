"""Real LLM tool-calling agent (Groq, OpenAI-compatible chat completions + tools API).

This replaces the deterministic regex router in agent.py's run_chat_turn for live chat
traffic: instead of pattern-matching "add product ..." / "what's available" / a buy intent,
the model itself decides which tool(s) to call and in what order, from natural language.

The underlying tools are the exact same real functions used everywhere else in this codebase
(shelf.search_catalog, parity.check_price_fairness, guardrail.execute_purchase,
catalog.add_product_to_catalog, catalog.catalog_overview) -- the LLM only chooses which to
call and composes the reply; it never touches money, mandate enforcement, or verification
logic directly. customer_id and mandate_id are injected server-side into tool execution, not
taken from the model's tool-call arguments, so the model can't spoof identity or mandate.

Falls back cleanly: if GROQ_API_KEY isn't set, callers should use agent.run_chat_turn instead
(see api/routes/chat.py). Batch test accuracy is unaffected either way -- the batch scripts
call search_catalog/check_price_fairness/execute_purchase directly, never through the agent.
"""
import json
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shelf.shelf import search_catalog
from parity.parity import check_price_fairness
from guardrail import guardrail
from growth.upsell import suggest_complementary
from negotiation.negotiation import propose_price
from api.routes.catalog import add_product_to_catalog, catalog_overview
from api.customer_auth import get_or_create_razorpay_customer_id
from agent.groq_client import chat_completion

MAX_TOOL_ITERATIONS = 6

# Per-session conversation memory. Without this, every message started a brand-new conversation
# with only the system prompt and that one message -- the model had no way to know what "add to
# cart" or "yes" referred to, even though the system prompt itself explicitly describes handling
# "a yes/confirmation reply to a product YOU just proposed" (a real, found-via-testing gap
# between what the prompt assumed and what the code actually gave it). Keyed by customer_id +
# session_id (the frontend already generates and sends session_id on every message; it just
# wasn't being used) so one customer's open tabs don't bleed into each other's history and a
# guessed/reused session_id can't replay a different customer's conversation. In-memory only --
# resets on server restart, which is an acceptable tradeoff for a hackathon demo, the same one
# guardrail/mandates.json's file-backed-but-unencrypted store already makes elsewhere.
_SESSION_HISTORY: dict[str, list] = {}
_MAX_HISTORY_TURNS = 6  # user+assistant exchanges kept -- bounds both memory and Groq's per-minute token budget


def _history_key(customer_id: str, session_id: str) -> str:
    return f"{customer_id}:{session_id}"


def _get_history(customer_id: str, session_id: str) -> list:
    if not session_id:
        return []
    return _SESSION_HISTORY.get(_history_key(customer_id, session_id), [])


def _append_history(customer_id: str, session_id: str, user_message: str, agent_reply: str) -> None:
    if not session_id:
        return
    history = _SESSION_HISTORY.setdefault(_history_key(customer_id, session_id), [])
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": agent_reply})
    del history[:-_MAX_HISTORY_TURNS * 2]  # keep only the most recent N exchanges

SYSTEM_PROMPT = """You are the Guardrail shopping agent for TechBazaar, a test-mode e-commerce store. You're helpful and personable, not robotic -- talk like a sharp, honest salesperson who'd rather lose a sale than mislead someone, not like a form printing out tool results.

You have seven tools. First decide whether the message is BROWSING, an explicit BUY, or a NEGOTIATE:
- BUY: it uses a clear purchase verb/phrase -- "buy", "purchase", "order", "get me", "I'll take it", "add to cart", "I want to buy", "go ahead and get it", or a yes/confirmation reply to a product YOU just proposed.
- NEGOTIATE: the customer explicitly wants to haggle -- "make an offer", "negotiate", "would you take X for it", "can I get it cheaper than that".
- BROWSING: everything else that names or implies a product interest with no purchase verb -- "wireless earbuds under 1000", "earbuds", "something for the gym", "what about a smartwatch rated 4.4+". This is the common case: most people describing what they want are asking you to find and show it, not authorizing a real charge on the spot.

For BROWSING, call search_catalog ONLY, then STOP: present the match (name, price, rating, and why it matched) and ask if they'd like you to buy it. Do NOT call check_price_fairness or execute_purchase for a browsing message -- there is nothing more pushy or confusing than a shopper casually mentioning "earbuds" and getting back "sorry, that purchase was blocked" for a purchase they never asked you to attempt.

For NEGOTIATE, after search_catalog has matched exactly one product: call negotiate_price with the customer's offer (round_number starting at 1). If it says "accept", tell them the deal is on and call execute_purchase at agreed_price_inr (not the original listed price). If it says "counter", relay the counter price and reason plainly, and ask if they want to accept it (call negotiate_price again with that exact counter price to close, round_number+1) or try another number. If it says "reject", say plainly that no agreement was reached and offer to buy at the listed price instead. Never invent your own counter-offer or claim a price is agreed unless negotiate_price itself said "accept".

For BUY, run the full pipeline in order:
1. search_catalog -- typo/partial-word tolerant, so it can return more than one product when it isn't sure ("possible match" in the reason field) instead of one confident exact match. If it returns no matches, stop and tell the user why (use no_match_reason verbatim). If it returns MORE THAN ONE match, stop and list them for the user (name and price each) and ask which one they meant -- never guess by picking one yourself, and never call check_price_fairness or execute_purchase until they've confirmed a single product.
2. check_price_fairness -- call this next for the one matched product before ever purchasing. If verdict is "flagged", stop here and explain why (quote the reason) -- never proceed to purchase a flagged price.
3. execute_purchase -- only call this if fairness passed. It enforces the customer's spending mandate and verifies the result against Razorpay itself; report its status and reason honestly, including if it's blocked or verification failed. Never claim a purchase succeeded unless execute_purchase's status is literally "success".
4. get_upsell_suggestions -- call this right after execute_purchase returns "success" for a single-item purchase (NOT "checkout_required" -- that means real payment isn't done yet, so nothing was actually bought to upsell alongside), passing the product_id that was just bought. If it returns a real suggestion, mention ONE of them naturally in your reply as an optional add-on ("Since you got the earbuds, a lot of people grab a charger too -- want me to add the Fast Charger 65W at ₹1,199?") -- offer it, never add it to the order yourself; only execute_purchase on it if the user says yes in a later message. Skip this step entirely for multi-item turns (don't offer an upsell while other items in the same request are still being processed) and don't mention it at all if suggestions is empty.

If a BUY request names MORE THAN ONE item ("buy earbuds and a phone case", "get me a laptop stand, also a mouse"), treat each item as its own search_catalog -> check_price_fairness -> execute_purchase sequence, one item fully at a time (don't call search_catalog twice before resolving the first item's fairness/purchase). If execute_purchase for one item returns status "checkout_required", stop there for this turn -- only one real Razorpay Checkout can be open at a time, so say which item is going to checkout and that the rest will be handled once that one is confirmed; never call execute_purchase again in the same turn after a checkout_required result. Summarize multi-item results item by item in the reply (what matched, price, outcome), don't just report the last one.

For "list a product" / "add product" / "sell X" requests, call add_product with whatever details the user gave. If required fields (name, category, price_inr, stock, rating) are missing, ask the user for exactly the missing ones in your reply -- do not call add_product until you have all required fields. category must be one of: audio, wearables, accessories, computer-accessories.

For general questions ("what's available", "what do you sell", "help", greetings), call get_store_overview and summarize it conversationally -- do not run a product search for these. get_store_overview is ONLY for questions about the store itself. If the user names or implies a need for a specific kind of item -- even vaguely ("something for the gym", "a gift for my mom", "nothing too fancy") -- that's a BROWSING message per the rule above: call search_catalog with their own words as the query and present the match, don't jump to purchasing it. Never recommend a specific product, or call it "affordable"/"cheap"/"good value", from get_store_overview's summary alone -- that tool doesn't check price, fairness, or budget fit, so a recommendation from it isn't grounded in anything real.

Never fabricate a product, price, or outcome that didn't come from a tool result. Be concise -- normally 1-3 sentences, up to 4 when naturally mentioning an upsell offer -- but concise means efficient, not curt: use the product's actual name and a concrete detail (price, rating, or the specific reason) instead of a bare status word, so a reply reads like a person who actually looked, not a status code. If a purchase is blocked or a price is flagged, always state the specific reason from the tool result, never just "something went wrong", and say plainly what the user could do differently (raise the mandate, pick a cheaper item, etc.) when that's obvious from the reason."""

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "search_catalog",
            "description": "Search the merchant catalog for a product matching a natural-language buying intent and optional budget.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The user's own product request, as close to their original wording as possible -- keep price/quality language like \"cheap\", \"affordable\", \"budget-friendly\" and feature phrases like \"with GPS\"/\"without GPS\" verbatim; the search itself reads those words to bias its ranking and apply hard feature filters, so paraphrasing them away (e.g. shortening \"cheap but good earbuds\" to just \"earbuds\") silently discards real user intent."},
                    "max_budget_inr": {"type": ["integer", "null"], "description": "Maximum price in INR, or null if unspecified."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_price_fairness",
            "description": "Check whether the price offered for a specific product is fair relative to what other customers were offered. Call after search_catalog, before execute_purchase.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "offered_price_inr": {"type": "integer"},
                },
                "required": ["product_id", "offered_price_inr"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_purchase",
            "description": "Execute a purchase against the customer's spending mandate. Enforces the mandate and independently verifies the result with Razorpay. Only call after fairness check passed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "amount_inr": {"type": "integer"},
                },
                "required": ["product_id", "amount_inr"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "negotiate_price",
            "description": "Propose a price for a product instead of buying at the listed price -- only call this when the customer explicitly asks to negotiate/haggle/make an offer. Returns accept (proceed to execute_purchase at agreed_price_inr), counter (call again with a new offer, round_number+1, to keep going), or reject (buy at listed price or give up). Never invents a counter price itself -- it's Razorpay's own fairness engine's real floor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "offered_price_inr": {"type": "number"},
                    "round_number": {"type": "integer", "description": "1 for the opening offer, incrementing each subsequent round."},
                },
                "required": ["product_id", "offered_price_inr"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_product",
            "description": "List a new product in the catalog on the customer's behalf (marketplace-style listing). Only call once all required fields are known.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string", "enum": ["audio", "wearables", "accessories", "computer-accessories"]},
                    "price_inr": {"type": "integer"},
                    "stock": {"type": "integer"},
                    "rating": {"type": "number"},
                    "description": {"type": "string"},
                },
                "required": ["name", "category", "price_inr", "stock", "rating"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_store_overview",
            "description": "Get a summary of the store: product counts per category and top-rated items. Use for general questions about what's available.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_upsell_suggestions",
            "description": "Real, in-stock products commonly paired with a product's category (e.g. a charger for earbuds). Call right after a successful purchase, passing the product_id just bought, to offer one relevant add-on.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "string"}},
                "required": ["product_id"],
            },
        },
    },
]


def _groq_chat(messages: list) -> dict:
    return chat_completion(messages, tools=TOOLS_SCHEMA, temperature=0.3)


def _execute_tool(name: str, args: dict, customer_id: str, mandate_id: str):
    """Runs the real underlying function. Returns (tool_result_for_llm, trace_event_or_None)."""
    if name == "search_catalog":
        result = search_catalog(args.get("query", ""), args.get("max_budget_inr"))
        if not result["matches"]:
            status = "no_match"
        elif len(result["matches"]) > 1:
            status = "ambiguous"  # typo/partial-word fallback returned a range, not one confident match
        else:
            status = "ok"
        return result, {"stage": "shelf", "status": status, "detail": result}

    if name == "check_price_fairness":
        result = check_price_fairness(args["product_id"], customer_id, args["offered_price_inr"])
        return result, {"stage": "parity", "status": result["verdict"], "detail": result}

    if name == "negotiate_price":
        result = propose_price(args["product_id"], args["offered_price_inr"], customer_id, args.get("round_number", 1))
        return result, {"stage": "negotiation", "status": result["verdict"], "detail": result}

    if name == "execute_purchase":
        try:
            # requesting_customer_id enforces ownership -- this customer can only spend
            # against a mandate they own, or a shared/demo mandate (owner None).
            token = guardrail.get_mandate_token(mandate_id, requesting_customer_id=customer_id)
        except KeyError:
            result = {"status": "blocked", "razorpay_order_id": None, "verification": None, "reason": f"Unknown mandate_id: {mandate_id}"}
            return result, {"stage": "guardrail", "status": "blocked", "detail": result}
        # Enforced here in code, not just asked of the model via the system prompt: the prompt
        # tells the LLM to call check_price_fairness before execute_purchase, but a model that
        # skips a step (or a prompt-injected turn trying to jump straight to buying) shouldn't
        # be the only thing standing between a flagged price and a real purchase. Re-checked
        # regardless of whether check_price_fairness was already called this turn.
        fairness = check_price_fairness(args["product_id"], customer_id, args["amount_inr"])
        if fairness["verdict"] == "flagged":
            result = {"status": "blocked", "razorpay_order_id": None, "verification": None,
                       "reason": f"Price fairness check failed, purchase not attempted: {fairness['reason']}"}
            return result, {"stage": "guardrail", "status": "blocked", "detail": result}
        # resolve_purchase hands off to real, human-verified Razorpay Checkout when real
        # credentials are configured (status "checkout_required"), else completes immediately
        # via the automated mock flow -- see guardrail.py's docstring on resolve_purchase.
        # razorpay_customer_id (real, tokenized on Razorpay's side -- see
        # api.customer_auth.get_or_create_razorpay_customer_id) lets Checkout recognize a
        # returning customer and offer their saved card/UPI method; this app never sees the
        # actual card data either way, only Razorpay's opaque customer id.
        razorpay_customer_id = get_or_create_razorpay_customer_id(customer_id)
        result = guardrail.resolve_purchase(
            token, args["product_id"], args["amount_inr"],
            requesting_customer_id=customer_id, razorpay_customer_id=razorpay_customer_id,
        )
        return result, {"stage": "guardrail", "status": result["status"], "detail": result}

    if name == "add_product":
        try:
            result = add_product_to_catalog(
                args["name"], args["category"], args["price_inr"], args["stock"], args["rating"], args.get("description", "")
            )
        except ValueError as e:
            # e.g. a category outside the 4 real ones -- reported back as a normal tool
            # result the LLM can see and relay, not an uncaught exception that would crash
            # the whole chat turn.
            result = {"error": str(e)}
            return result, {"stage": "add_product", "status": "invalid", "detail": result}
        return result, {"stage": "add_product", "status": "ok", "detail": result}

    if name == "get_store_overview":
        result = catalog_overview()
        return result, None

    if name == "get_upsell_suggestions":
        result = suggest_complementary(args["product_id"], requesting_customer_id=customer_id)
        return result, {"stage": "growth", "status": "ok" if result["suggestions"] else "flagged", "detail": result}

    return {"error": f"Unknown tool: {name}"}, None


def run_chat_turn_llm_stream(message: str, customer_id: str, mandate_id: str, session_id: str = None):
    """Generator yielding {"type": "stage", ...} events as tools complete, then one final
    {"type": "final", "agent_reply", "pipeline_trace", "product_card", "purchase_result",
    "purchase_results"}. purchase_results is the full list, in order, for multi-item turns
    ("earbuds and a phone case") -- purchase_result is kept as just the LAST one for simple
    single-item callers, but a caller rendering a result card per item should use
    purchase_results, not purchase_result, or every item but the last is silently invisible.

    session_id (optional) is what actually gives this turn access to earlier ones in the same
    conversation -- see _SESSION_HISTORY's docstring above. Without it (e.g. a caller that
    genuinely wants a one-shot, context-free turn), this behaves exactly as before."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *_get_history(customer_id, session_id),
        {"role": "user", "content": message},
    ]
    trace = []
    product_card = None
    purchase_results = []

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = _groq_chat(messages)
        except requests.RequestException as e:
            # chat_completion() already retried through a transient rate-limit -- reaching here
            # means it's still failing after those retries, so this is a real, sustained outage
            # (or a genuinely exhausted quota), not just a momentary blip worth another attempt.
            reply = (
                "The AI is getting rate-limited right now -- please wait a few seconds and try again."
                if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 429
                else "I couldn't reach the AI model just now -- please try again in a moment."
            )
            yield {"type": "final", "agent_reply": reply,
                   "pipeline_trace": trace, "product_card": product_card,
                   "purchase_result": purchase_results[-1] if purchase_results else None,
                   "purchase_results": purchase_results}
            return

        choice = response["choices"][0]["message"]
        messages.append(choice)
        tool_calls = choice.get("tool_calls")

        if not tool_calls:
            reply = choice.get("content") or "I'm not sure how to help with that."
            _append_history(customer_id, session_id, message, reply)
            yield {"type": "final", "agent_reply": reply,
                   "pipeline_trace": trace, "product_card": product_card,
                   "purchase_result": purchase_results[-1] if purchase_results else None,
                   "purchase_results": purchase_results}
            return

        for call in tool_calls:
            fn_name = call["function"]["name"]
            try:
                fn_args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            result, event = _execute_tool(fn_name, fn_args, customer_id, mandate_id)

            if event:
                trace.append(event)
                yield {"type": "stage", **event}
                if event["stage"] == "shelf" and event["status"] == "ok":
                    p = event["detail"]["matches"][0]
                    product_card = {"product_id": p["product_id"], "name": p["name"], "price_inr": p["price_inr"],
                                     "rating": p["rating"], "reason": p["reason"]}
                if event["stage"] == "guardrail":
                    if result.get("status") == "checkout_required":
                        # Pause here for real -- payment needs a human at actual Checkout.
                        # Doesn't feed back into the LLM loop; /api/purchase/confirm finishes
                        # this turn once the human completes it.
                        earlier_notes = []
                        for ev in trace[:-1]:  # every earlier item's outcome (excludes this item's own checkout_required event)
                            if ev["stage"] == "guardrail" and ev["status"] == "blocked":
                                earlier_notes.append(f"(also: {ev['detail'].get('reason', 'a previous item was blocked')})")
                            elif ev["stage"] == "guardrail" and ev["status"] == "success":
                                earlier_notes.append(f"(also: bought {ev['detail'].get('product_id')} successfully)")
                        prefix = " ".join(earlier_notes) + " " if earlier_notes else ""
                        checkout_reply = (
                            f"{prefix}Found {product_card['name'] if product_card else 'your item'} within your mandate -- "
                            "complete payment via Razorpay to finish the purchase."
                        )
                        _append_history(customer_id, session_id, message, checkout_reply)
                        yield {"type": "checkout", "agent_reply": checkout_reply,
                               "checkout": result["checkout"], "pipeline_trace": trace,
                               "purchase_results": purchase_results}
                        return
                    purchase_results.append(result)

            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, default=str)})

    exhausted_reply = "I wasn't able to finish this within a reasonable number of steps -- please try rephrasing."
    _append_history(customer_id, session_id, message, exhausted_reply)
    yield {"type": "final", "agent_reply": exhausted_reply,
           "pipeline_trace": trace, "product_card": product_card,
           "purchase_result": purchase_results[-1] if purchase_results else None,
           "purchase_results": purchase_results}


def run_chat_turn_llm(message: str, customer_id: str, mandate_id: str, session_id: str = None) -> dict:
    """Non-streaming convenience wrapper: runs the generator to completion, returns the
    terminal event (either "final" or "checkout")."""
    terminal = None
    for event in run_chat_turn_llm_stream(message, customer_id, mandate_id, session_id):
        if event["type"] in ("final", "checkout"):
            terminal = event
    terminal.pop("type", None)
    if "checkout" in terminal:
        terminal.setdefault("pipeline_trace", [])
        terminal.setdefault("product_card", None)
        terminal.setdefault("purchase_result", None)
        terminal.setdefault("purchase_results", [])
    return terminal

