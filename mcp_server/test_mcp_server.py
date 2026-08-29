"""Acceptance tests for the TechBazaar MCP server's tools. Calls the tool functions directly
(FastMCP's @mcp.tool() decorator registers metadata but returns the original callable
unchanged, confirmed by type() -- these are the exact same functions an MCP client would
invoke over the protocol, just without the JSON-RPC transport in the way for a fast test)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from guardrail import guardrail
from mcp_server.techbazaar_mcp_server import (
    register_ai_buyer, create_mandate, search_catalog, check_price_fairness, purchase, get_store_overview,
    get_upsell_suggestions, negotiate_price, CUSTOMER_PROFILES_PATH,
)

# Isolated from the real guardrail/mandates.json and guardrail/ledger.json -- setup()'s
# reset_mandate_store() below rebuilds the mandate store from seed fixtures, discarding any real
# mandate not in that seed. See guardrail.use_isolated_store's docstring -- an isolated AI buyer's
# real, mid-checkout mandate was actually wiped this way during development.
_MANDATE_PATH = os.path.join(os.path.dirname(__file__), "_isolated_test_mandates.json")
_LEDGER_PATH = os.path.join(os.path.dirname(__file__), "_isolated_test_ledger.json")

_ids_before_this_run = set()


def setup_module():
    # use_isolated_store re-asserted HERE (an execution-phase hook), not at module import time
    # (a collection-phase event) -- when several test files each isolate at import time, pytest
    # collects (imports) ALL of them before ANY test executes, so whichever file was collected
    # LAST silently wins for every file's test execution. setup_module() runs right before THIS
    # file's tests actually execute, so it always re-points isolation back correctly regardless
    # of collection order. See guardrail.use_isolated_store's docstring for why isolation exists
    # at all -- a real AI buyer's mid-checkout mandate was wiped by this exact class of bug.
    guardrail.use_isolated_store(_MANDATE_PATH, _LEDGER_PATH)

    # register_ai_buyer writes to the real, shared customer_profiles.json -- the dashboard's
    # Connected AI Agents panel reads straight from it, so without this snapshot-and-diff every
    # pytest run would leave fixture identities ("Test Agent", "Intruder"...) behind for that
    # panel to show as if they were genuine buyer activity.
    global _ids_before_this_run
    with open(CUSTOMER_PROFILES_PATH, encoding="utf-8") as f:
        _ids_before_this_run = {p["customer_id"] for p in json.load(f)}


def teardown_module():
    with open(CUSTOMER_PROFILES_PATH, encoding="utf-8") as f:
        profiles = json.load(f)
    kept = [p for p in profiles if p["customer_id"] in _ids_before_this_run]
    if len(kept) != len(profiles):
        with open(CUSTOMER_PROFILES_PATH, "w", encoding="utf-8") as f:
            json.dump(kept, f, indent=2)


def setup():
    guardrail.reset_mandate_store()


def test_register_ai_buyer_gets_a_distinct_id_namespace():
    # AI buyer ids are prefixed distinctly from human customer ids (c001, c002...) so an
    # admin looking at the audit trail can always tell an autonomous purchase from a human's.
    buyer = register_ai_buyer("Test Agent")
    assert buyer["ai_buyer_id"].startswith("ai_buyer_")


def test_two_ai_buyers_get_different_ids():
    b1 = register_ai_buyer("Agent One")
    b2 = register_ai_buyer("Agent Two")
    assert b1["ai_buyer_id"] != b2["ai_buyer_id"]


def test_created_mandate_is_owned_by_the_ai_buyer():
    setup()
    buyer = register_ai_buyer("Mandate Owner Test")
    mandate = create_mandate(buyer["ai_buyer_id"], max_amount_inr=2000)
    assert mandate["state"]["owner_customer_id"] == buyer["ai_buyer_id"]


def test_purchase_respects_the_mandate_bound():
    setup()
    buyer = register_ai_buyer("Bounded Buyer")
    mandate = create_mandate(buyer["ai_buyer_id"], max_amount_inr=500)
    result = search_catalog("wireless earbuds", max_budget_inr=5000)
    product = result["matches"][0]  # will be priced above the 500 mandate limit
    outcome = purchase(product["product_id"], product["price_inr"], buyer["ai_buyer_id"], mandate["mandate_id"])
    assert outcome["status"] == "blocked"
    assert "exceed" in outcome["reason"].lower()


def test_a_different_ai_buyer_cannot_use_someone_elses_mandate():
    setup()
    owner = register_ai_buyer("Rightful Owner")
    intruder = register_ai_buyer("Intruder")
    mandate = create_mandate(owner["ai_buyer_id"], max_amount_inr=5000)
    result = search_catalog("phone case", max_budget_inr=1000)
    product = result["matches"][0]
    outcome = purchase(product["product_id"], product["price_inr"], intruder["ai_buyer_id"], mandate["mandate_id"])
    assert outcome["status"] == "blocked"
    assert "unknown" in outcome["reason"].lower() or "inaccessible" in outcome["reason"].lower()


def test_search_and_fairness_check_use_the_real_shared_engines():
    # Not a re-implementation -- same Shelf/Parity functions, same behavior as the internal
    # chat agent, including honest no_match_reason and fairness verdicts.
    result = search_catalog("wireless earbuds", max_budget_inr=2000)
    assert len(result["matches"]) == 1
    product = result["matches"][0]
    fairness = check_price_fairness(product["product_id"], "ai_buyer_test", product["price_inr"])
    assert fairness["verdict"] in ("fair", "flagged")


def test_store_overview_returns_real_catalog_data():
    overview = get_store_overview()
    assert overview["total_products"] > 0
    assert "category_counts" in overview


def test_upsell_suggestions_use_the_real_shared_engine():
    result = get_upsell_suggestions("p001", "ai_buyer_test")  # earbuds -> accessories
    assert len(result["suggestions"]) > 0
    assert all(s["category"] == "accessories" for s in result["suggestions"])


def test_negotiate_price_uses_the_real_shared_negotiation_engine():
    lowball = negotiate_price("p001", "ai_buyer_test", 1000, round_number=1)
    assert lowball["verdict"] == "counter"
    accepted = negotiate_price("p001", "ai_buyer_test", lowball["counter_price_inr"], round_number=2)
    assert accepted["verdict"] == "accept"
    assert accepted["agreed_price_inr"] == lowball["counter_price_inr"]


if __name__ == "__main__":
    test_register_ai_buyer_gets_a_distinct_id_namespace()
    test_two_ai_buyers_get_different_ids()
    test_created_mandate_is_owned_by_the_ai_buyer()
    test_purchase_respects_the_mandate_bound()
    test_a_different_ai_buyer_cannot_use_someone_elses_mandate()
    test_search_and_fairness_check_use_the_real_shared_engines()
    test_store_overview_returns_real_catalog_data()
    print("All MCP server tests passed.")
