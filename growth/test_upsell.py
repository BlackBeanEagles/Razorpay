"""Acceptance tests for the upsell/cross-sell suggestion engine."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from growth.upsell import suggest_complementary, COMPLEMENTARY_CATEGORIES, _load_catalog


def test_earbuds_get_an_accessory_suggestion():
    # p001 Wireless Earbuds X200 is category "audio" -> accessories.
    result = suggest_complementary("p001")
    assert len(result["suggestions"]) > 0
    for s in result["suggestions"]:
        assert s["category"] == "accessories"


def test_suggestions_never_include_the_source_product():
    result = suggest_complementary("p009")  # Phone Case Slim, accessories
    ids = {s["product_id"] for s in result["suggestions"]}
    assert "p009" not in ids


def test_suggestions_are_ranked_by_rating_then_price():
    result = suggest_complementary("p001", limit=10)
    ratings = [s["rating"] for s in result["suggestions"]]
    assert ratings == sorted(ratings, reverse=True)


def test_suggestions_are_always_in_stock():
    catalog = {p["product_id"]: p for p in _load_catalog()}
    result = suggest_complementary("p006", limit=10)  # Smartwatch Fit 2, wearables
    for s in result["suggestions"]:
        assert catalog[s["product_id"]]["stock"] > 0


def test_unknown_product_returns_honest_empty_result():
    result = suggest_complementary("p999")
    assert result["suggestions"] == []
    assert "Unknown product_id" in result["reason"]


def test_limit_is_respected():
    result = suggest_complementary("p001", limit=1)
    assert len(result["suggestions"]) <= 1


def test_every_category_with_a_mapping_produces_grounded_suggestions():
    # Coverage check: every category that COMPLEMENTARY_CATEGORIES claims to support should
    # actually be able to produce a real suggestion from the current catalog -- otherwise the
    # mapping is making a promise the data can't back up.
    catalog = _load_catalog()
    for category in COMPLEMENTARY_CATEGORIES:
        sample = next(p for p in catalog if p["category"] == category)
        result = suggest_complementary(sample["product_id"])
        assert len(result["suggestions"]) > 0, f"category {category!r} (via {sample['product_id']}) got no suggestions"
