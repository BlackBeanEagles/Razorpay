"""Acceptance tests for Shelf (spec section 4)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shelf.shelf import search_catalog


def test_clear_match_within_budget():
    r = search_catalog("wireless earbuds under 2000 rupees, prefer good ratings", 2000)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] == "p001"
    assert r["matches"][0]["reason"]


def test_multiple_candidates_consistent_rule():
    r = search_catalog("Get me the fitness smartwatch with GPS", 5000)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] == "p007"


def test_no_match_below_cheapest():
    r = search_catalog("Get me a laptop stand under 1000 rupees", 1000)
    assert r["matches"] == []
    assert r["no_match_reason"] is not None


def test_out_of_stock_excluded():
    r = search_catalog("Get me a power bank, at least 20000mAh", 2000)
    assert r["matches"] == []
    assert "stock" in r["no_match_reason"].lower()


def test_category_mismatch_no_hallucination():
    r = search_catalog("Buy me a car", 2000)
    assert r["matches"] == []
    assert r["no_match_reason"] is not None


def test_case_insensitive():
    lower = search_catalog("wireless earbuds under 2000", 2000)
    upper = search_catalog("WIRELESS EARBUDS UNDER 2000", 2000)
    mixed = search_catalog("WiReLeSs EarBuds under 2000", 2000)
    assert lower["matches"][0]["product_id"] == upper["matches"][0]["product_id"] == mixed["matches"][0]["product_id"] == "p001"


def test_typo_tolerance_still_finds_a_result():
    # "wireles"/"erbuds" are both one edit away from "wireless"/"earbuds" -- exact matching
    # alone would find nothing; the fuzzy fallback should surface earbuds products anyway.
    r = search_catalog("wireles erbuds", 2000)
    assert len(r["matches"]) >= 1
    assert all("earbuds" in m["name"].lower() for m in r["matches"])


def test_ambiguous_typo_match_returns_a_range_not_a_guess():
    r = search_catalog("smartwach", 5000)
    assert len(r["matches"]) > 1  # not confident enough to silently pick just one
    assert all(m["reason"].startswith("possible match") for m in r["matches"])


def test_short_words_dont_fuzzy_match_unrelated_products():
    # "car" is one edit away from "card" (present in a USB hub's description) -- short
    # words should never fuzzy-match, or nearly anything could collide with anything.
    r = search_catalog("buy me a car", 2000)
    assert r["matches"] == []


def test_implicit_cheap_preference_picks_best_value_not_priciest():
    # No explicit budget or "cheapest" -- but "cheap but good" should still bias toward
    # rating-per-rupee, not just the single highest-rated (and priciest) earbuds.
    r = search_catalog("I want cheap but good earbuds", None)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] == "p002"  # Wireless Earbuds Lite, best value


def test_feature_required_excludes_products_without_it():
    r = search_catalog("Get me a smartwatch without GPS", 5000)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] == "p006"  # Smartwatch Fit 2, no GPS


def test_feature_with_stopword_does_not_break_matching():
    # "with good ratings" should not be read as requiring the literal word "good" in a
    # product's text -- that would wrongly zero out every match.
    r = search_catalog("wireless earbuds under 2000 rupees, with good ratings", 2000)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] == "p001"


def test_two_word_feature_extracted_fully():
    # No product has "waterproof lighting" -- confirms both words of the two-word feature
    # were actually read (a single-word capture would only require "waterproof", which a
    # real product -- Bluetooth Speaker Mini -- has, so this alone wouldn't distinguish the
    # two cases -- the real check is the no_match_reason naming both words below).
    r = search_catalog("Get me headphones with waterproof lighting", 10000)
    assert r["matches"] == []
    assert "waterproof" in r["no_match_reason"] and "lighting" in r["no_match_reason"]


def test_feature_filter_gives_specific_no_match_reason():
    r = search_catalog("Get me headphones with waterproof lighting", 10000)
    assert "feature constraint" in r["no_match_reason"]
    assert "category or description" not in r["no_match_reason"]  # not the generic fallback message


def test_anc_synonym_connects_noise_cancelling_to_catalog_term():
    # The catalog describes Wireless Earbuds Pro Max as "ANC" (the industry abbreviation),
    # not the words a shopper actually types -- this is a real match, not an adversarial one.
    r = search_catalog("Get me wireless earbuds with noise cancelling", 10000)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] == "p003"


def test_feature_filter_requires_overlap_with_core_query_terms():
    # "Fast Charger 65W" satisfies "with fast charging" on its own, but a phone case with
    # fast charging isn't a real product -- the feature filter must not let an unrelated
    # product win just because it happens to match the feature words alone.
    r = search_catalog("Get me a phone case with fast charging", 10000)
    assert r["matches"] == []


def test_comparative_price_excludes_the_referenced_product_itself():
    # "cheaper than the Wireless Earbuds Pro Max" implies not-Pro-Max -- without excluding
    # it, the query's own mention of its name would make it win the keyword match and then
    # get excluded by the very budget cap it set, silently returning no results at all.
    r = search_catalog("Get me earbuds cheaper than the Wireless Earbuds Pro Max", None)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] != "p003"
    assert r["matches"][0]["price_inr"] < 3499


def test_quantity_detected_and_surfaced_not_silently_ignored():
    r = search_catalog("I want two wireless earbuds under 2000 rupees", 2000)
    assert r["matches"][0]["quantity_requested"] == 2
    assert "2" in r["matches"][0]["reason"]


def test_quantity_not_confused_with_a_digit_in_a_referenced_product_name():
    # The "2" in "Smartwatch Fit 2 Pro" is part of a product name, not a quantity -- this
    # exact case previously misfired and returned quantity_requested=2.
    r = search_catalog("Get me a smartwatch cheaper than the Smartwatch Fit 2 Pro", None)
    assert r["matches"][0]["quantity_requested"] == 1


def test_with_a_common_word_does_not_hard_filter():
    # "with a phone case" -- "a" right after "with" is a stopword, so this must NOT be read
    # as a hard requirement for "phone"; the laptop stand should still win normally.
    r = search_catalog("Get me a laptop stand along with a phone case", 5000)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] == "p015"


def test_with_clause_captures_multiple_and_separated_features():
    # Smartwatch Fit 2 Pro is the only product with BOTH GPS and SpO2 -- a single "with GPS
    # and SpO2" clause must require both, not just the first feature named.
    r = search_catalog("Get me a smartwatch with GPS and SpO2", 10000)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] == "p007"


def test_with_clause_and_separated_features_no_match_when_none_qualify():
    # Smartwatch Fit 2 has heart-rate but no GPS; Fit 2 Pro has GPS but no heart-rate --
    # no product in this catalog genuinely has both, so this must honestly fail, not silently
    # match on just one of the two required features.
    r = search_catalog("Get me a smartwatch with heart rate and GPS", 10000)
    assert r["matches"] == []


def test_rating_floor_is_a_hard_filter_not_a_soft_preference():
    # Wireless Earbuds X200 (4.3) and Lite (3.9) would otherwise be in the running, but an
    # explicit "above 4.5 stars" must exclude both -- only Pro Max (4.6) qualifies.
    r = search_catalog("wireless earbuds above 4.5 stars", 10000)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] == "p003"


def test_rating_floor_impossible_gives_honest_no_match():
    r = search_catalog("wireless earbuds above 4.9 stars", 10000)
    assert r["matches"] == []
    assert "4.9" in r["no_match_reason"]


def test_rating_floor_alternate_phrasings():
    assert search_catalog("smartwatch 4.4+ rating", 10000)["matches"][0]["product_id"] == "p007"
    assert search_catalog("earbuds rated 4.5 or higher", 10000)["matches"][0]["product_id"] == "p003"


def test_rating_floor_number_not_misread_as_quantity():
    # "4 stars" must not be read as "quantity 4" the way "4 chargers" would be.
    r = search_catalog("wireless earbuds above 4 stars", 10000)
    assert r["matches"][0]["quantity_requested"] == 1


def test_feature_clause_and_rating_floor_combine_regardless_of_order():
    # Real bug found by stress-testing: "with GPS above 4 stars" used to read "above"/"stars"
    # as bogus required feature words (neither is a stopword), zeroing the catalog -- while the
    # reordered "above 4 stars with GPS" worked fine, purely because of clause boundary luck.
    # Both orderings must now agree on the same, correct answer.
    order_a = search_catalog("smartwatch with GPS above 4 stars", 10000)
    order_b = search_catalog("smartwatch above 4 stars with GPS", 10000)
    assert order_a["matches"] and order_a["matches"][0]["product_id"] == "p007"
    assert order_b["matches"] and order_b["matches"][0]["product_id"] == "p007"


def test_rated_or_higher_phrasing_inside_a_with_clause():
    r = search_catalog("smartwatch with GPS rated 4 or higher", 10000)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["product_id"] == "p007"


def test_rated_or_higher_number_not_misread_as_quantity():
    # "rated 4 or higher" -- the word right after "4" is "or", not a rating word, so the
    # existing next-word-only guard didn't catch this specific phrasing.
    r = search_catalog("smartwatch rated 4 or higher", 10000)
    assert r["matches"][0]["quantity_requested"] == 1


if __name__ == "__main__":
    test_clear_match_within_budget()
    test_multiple_candidates_consistent_rule()
    test_no_match_below_cheapest()
    test_out_of_stock_excluded()
    test_category_mismatch_no_hallucination()
    test_case_insensitive()
    test_typo_tolerance_still_finds_a_result()
    test_ambiguous_typo_match_returns_a_range_not_a_guess()
    test_short_words_dont_fuzzy_match_unrelated_products()
    test_implicit_cheap_preference_picks_best_value_not_priciest()
    test_feature_required_excludes_products_without_it()
    test_feature_with_stopword_does_not_break_matching()
    test_two_word_feature_extracted_fully()
    test_feature_filter_gives_specific_no_match_reason()
    test_anc_synonym_connects_noise_cancelling_to_catalog_term()
    test_feature_filter_requires_overlap_with_core_query_terms()
    test_comparative_price_excludes_the_referenced_product_itself()
    test_quantity_detected_and_surfaced_not_silently_ignored()
    test_quantity_not_confused_with_a_digit_in_a_referenced_product_name()
    test_with_a_common_word_does_not_hard_filter()
    test_rating_floor_is_a_hard_filter_not_a_soft_preference()
    test_rating_floor_impossible_gives_honest_no_match()
    test_rating_floor_alternate_phrasings()
    test_rating_floor_number_not_misread_as_quantity()
    test_feature_clause_and_rating_floor_combine_regardless_of_order()
    test_rated_or_higher_phrasing_inside_a_with_clause()
    test_rated_or_higher_number_not_misread_as_quantity()
    test_with_clause_captures_multiple_and_separated_features()
    test_with_clause_and_separated_features_no_match_when_none_qualify()
    print("All Shelf tests passed.")
