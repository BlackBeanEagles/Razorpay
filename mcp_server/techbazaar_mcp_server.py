"""TechBazaar's own MCP server -- makes this merchant transactable by an external AI buyer
agent end to end, not just through the storefront's own chat UI.

The internal chat agent (agent/llm_agent.py) already exposes search_catalog/
check_price_fairness/execute_purchase as LLM tool-calling functions, but only reachable via
Groq talking to OUR OWN chat panel -- an outside AI buyer agent has no way to discover or call
into TechBazaar at all. This server exposes the exact same underlying, already-hardened
Shelf/Parity/Guardrail functions as a real MCP server any MCP-compatible agent can connect to
directly (the same protocol pattern guardrail/razorpay_mcp_client.py already uses as a CLIENT,
here TechBazaar is the server instead).

Deliberately reuses the real functions, not a re-implementation: search_catalog is the exact
same fuzzy/synonym/feature-filter engine with 29 passing tests, purchase goes through the exact
same mandate-enforced, race-free Guardrail path with the concurrency fixes proven earlier this
session. An external AI buyer gets no shortcut around any of Guardrail's rules.

Identity: an external agent has no browser session/cookie, so tools take an explicit
ai_buyer_id rather than deriving identity from a web session. register_ai_buyer() mints a
dedicated identity (id-prefixed "ai_buyer_", never reusing a real human customer's id) so every
autonomous purchase is distinguishable from a human's in the audit trail -- directly serving
the "every money action explainable" bar: an admin looking at the dashboard can always tell an
AI-buyer transaction from a human one at a glance.

Run standalone: `python mcp_server/techbazaar_mcp_server.py` (stdio transport, the standard way
an MCP client like Claude Desktop or another agent framework spawns and talks to a server).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mcp.server.fastmcp import FastMCP

from shelf.shelf import search_catalog as _search_catalog
from parity.parity import check_price_fairness as _check_price_fairness
from guardrail import guardrail
from growth.upsell import suggest_complementary as _suggest_complementary
from api.routes.catalog import catalog_overview
from api.file_lock import file_lock

CUSTOMER_PROFILES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "customer_profiles.json")
_PROFILES_LOCK_PATH = CUSTOMER_PROFILES_PATH + ".lock"

mcp = FastMCP(
    name="techbazaar",
    instructions=(
        "TechBazaar is a Razorpay test-mode storefront. Call register_ai_buyer once to get an "
        "identity, then create_mandate to set a bounded spending limit before shopping -- "
        "purchase() will never let you spend past it. Always search_catalog before purchasing, "
        "and always check_price_fairness on the matched product before purchase -- a flagged "
        "price should not be bought. After a successful purchase, call get_upsell_suggestions "
        "on the product you bought and offer the buyer a relevant add-on if one exists and "
        "still fits their mandate -- don't just stop at one item."
    ),
)


@mcp.tool()
def register_ai_buyer(name: str) -> dict:
    """Registers a new AI buyer identity. Call this once before shopping -- every purchase
    needs an ai_buyer_id, and this id is what makes the transaction distinguishable from a
    human customer's in the merchant's audit trail."""
    with file_lock(_PROFILES_LOCK_PATH):
        with open(CUSTOMER_PROFILES_PATH, encoding="utf-8") as f:
            profiles = json.load(f)
        existing_ai_ids = [
            int(p["customer_id"].split("_")[-1]) for p in profiles
            if p["customer_id"].startswith("ai_buyer_") and p["customer_id"].split("_")[-1].isdigit()
        ]
        next_num = max(existing_ai_ids, default=0) + 1
        ai_buyer_id = f"ai_buyer_{next_num:03d}"
        profiles.append({
            "customer_id": ai_buyer_id, "loyalty_tier": "none", "is_first_time": True,
            "typical_order_size": "single", "display_name": name, "is_ai_buyer": True,
        })
        tmp_path = f"{CUSTOMER_PROFILES_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=2)
        os.replace(tmp_path, CUSTOMER_PROFILES_PATH)
    return {"ai_buyer_id": ai_buyer_id, "name": name}


@mcp.tool()
def create_mandate(ai_buyer_id: str, max_amount_inr: int, expires_in_seconds: int = 3600, single_use: bool = False) -> dict:
    """Creates a bounded, expiring spending mandate owned by this AI buyer -- purchase() will
    refuse to spend a rupee past max_amount_inr or after expiry, regardless of what the buyer
    agent itself asks for. This is the "bounded and gated" guarantee: the limit is enforced by
    the merchant's own Guardrail, not by trusting the calling agent's self-restraint."""
    return guardrail.issue_and_store_mandate(
        merchant_id="merchant_ai_buyer", max_amount_inr=max_amount_inr,
        expires_in_seconds=expires_in_seconds, single_use=single_use, owner_customer_id=ai_buyer_id,
    )


@mcp.tool()
def search_catalog(query: str, max_budget_inr: int = None) -> dict:
    """Searches the real TechBazaar catalog for a product matching a natural-language buying
    intent and optional budget. The same engine the storefront's own chat agent uses --
    typo/synonym-tolerant, honest "no match" reasons, and returns more than one candidate when
    genuinely ambiguous rather than guessing."""
    return _search_catalog(query, max_budget_inr)


@mcp.tool()
def check_price_fairness(product_id: str, ai_buyer_id: str, offered_price_inr: int) -> dict:
    """Checks whether a price is consistent with what other customers were offered for the same
    product. Call this on the product search_catalog matched, before ever purchasing -- a
    "flagged" verdict means the price looks inconsistent and should not be bought as-is."""
    return _check_price_fairness(product_id, ai_buyer_id, offered_price_inr)


@mcp.tool()
def purchase(product_id: str, amount_inr: int, ai_buyer_id: str, mandate_id: str) -> dict:
    """Attempts to purchase product_id for amount_inr against the given mandate. Enforced by
    the exact same race-free Guardrail path the storefront uses -- blocked if it would exceed
    the mandate, expired, or already single-used; if real Razorpay credentials are configured,
    returns a checkout_required handoff for a human to complete real Checkout (an autonomous
    AI buyer cannot complete real payment itself, only the mock/automated path can resolve
    immediately) -- never claims success unless independently verified against Razorpay."""
    try:
        token = guardrail.get_mandate_token(mandate_id, requesting_customer_id=ai_buyer_id)
    except KeyError:
        return {"status": "blocked", "reason": f"Unknown or inaccessible mandate_id: {mandate_id}"}
    return guardrail.resolve_purchase(token, product_id, amount_inr, requesting_customer_id=ai_buyer_id)


@mcp.tool()
def confirm_purchase(mandate_id: str, product_id: str, amount_inr: int, razorpay_order_id: str, ai_buyer_id: str) -> dict:
    """Step 2 of a real-money purchase, called after real Razorpay Checkout for razorpay_order_id
    has actually been completed (by whatever completed it -- a human, or another automated
    flow this agent orchestrates). Independently re-verifies with Razorpay what was actually
    captured before ever marking anything as success or updating mandate spend -- never trusts
    the caller's say-so that payment succeeded. Idempotent: confirming the same
    razorpay_order_id twice returns the original result rather than double-counting spend."""
    return guardrail.confirm_purchase(mandate_id, product_id, amount_inr, razorpay_order_id, requesting_customer_id=ai_buyer_id)


@mcp.tool()
def get_upsell_suggestions(product_id: str, ai_buyer_id: str) -> dict:
    """Real, in-stock products commonly paired with product_id's category (e.g. a charger for
    earbuds) -- grounded in the actual catalog, not a fabricated "customers also bought" claim.
    Useful right after a purchase: an agent optimizing for the merchant's revenue, not just its
    own shopping task, can offer the buyer a relevant add-on instead of stopping at one item."""
    return _suggest_complementary(product_id, requesting_customer_id=ai_buyer_id)


@mcp.tool()
def get_store_overview() -> dict:
    """Category counts and top-rated picks -- for general "what do you sell" style questions,
    not a substitute for search_catalog when the buyer has a specific product in mind."""
    return catalog_overview()


if __name__ == "__main__":
    mcp.run(transport="stdio")
