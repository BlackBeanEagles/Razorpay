"""Growth: complementary-product suggestions -- the "grow the merchant's revenue" half of
Track 1's brief (the rest of this codebase covers the other half, "make them sellable to AI
buyers end to end").

Deliberately rule-based against real catalog data, not a fabricated "customers also bought"
claim: this merchant has no real cross-customer purchase-pair history to mine, so a
collaborative-filtering-style recommendation here would be inventing statistics rather than
reporting them. COMPLEMENTARY_CATEGORIES below is a hand-authored, explainable adjacency map
(earbuds pair with a charger, a keyboard pairs with a USB hub) -- every suggestion is
honestly reasoned ("commonly paired with {category} purchases"), grounded in real stock and
price, never claimed as a measured behavioral pattern.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from audit.audit_log import log_event

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "catalog.json")

# Category -> categories commonly bought alongside it. Not symmetric by design: an accessory
# is a natural add-on to nearly anything, but the reverse (suggesting a smartwatch to someone
# buying a USB cable) is a weaker, less honest pairing, so accessories fan out while other
# categories point narrowly back at accessories.
COMPLEMENTARY_CATEGORIES = {
    "audio": ["accessories"],
    "wearables": ["accessories"],
    "computer-accessories": ["accessories"],
    "accessories": ["audio", "wearables", "computer-accessories"],
}


def _load_catalog() -> list:
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def suggest_complementary(product_id: str, limit: int = 2, requesting_customer_id: str = None) -> dict:
    """Returns up to `limit` real, in-stock products commonly paired with product_id's category,
    ranked by rating then price. {"source_product_id", "suggestions": [...], "reason"} -- reason
    is None (and suggestions empty) when the source product is unknown or its category has no
    defined pairing, reported honestly rather than falling back to an arbitrary guess."""
    catalog = _load_catalog()
    source = next((p for p in catalog if p["product_id"] == product_id), None)
    if source is None:
        result = {"source_product_id": product_id, "suggestions": [], "reason": f"Unknown product_id: {product_id}"}
        log_event("growth", "upsell_suggested", {"product_id": product_id, "requesting_customer_id": requesting_customer_id}, result, "ok")
        return result

    target_categories = COMPLEMENTARY_CATEGORIES.get(source["category"], [])
    candidates = [
        p for p in catalog
        if p["category"] in target_categories and p["product_id"] != product_id and p["stock"] > 0
    ]
    candidates.sort(key=lambda p: (-p["rating"], p["price_inr"]))
    suggestions = [
        {"product_id": p["product_id"], "name": p["name"], "price_inr": p["price_inr"],
         "rating": p["rating"], "category": p["category"]}
        for p in candidates[:limit]
    ]
    result = {
        "source_product_id": product_id,
        "suggestions": suggestions,
        "reason": (
            f"Commonly paired with {source['category']} purchases." if suggestions
            else f"No in-stock complementary products found for category {source['category']!r}."
        ),
    }
    log_event("growth", "upsell_suggested",
              {"product_id": product_id, "requesting_customer_id": requesting_customer_id},
              result, "ok" if suggestions else "flagged")
    return result
