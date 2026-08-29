"""Shelf: catalog search. search_catalog(query, max_budget_inr) -> matches + reasoning."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from audit.audit_log import log_event

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "catalog.json")

STOPWORDS = {
    "get", "me", "buy", "the", "a", "an", "you", "can", "find", "prefer", "preferred",
    "good", "under", "rupees", "rupee", "at", "least", "if", "possible", "nothing",
    "fancy", "even", "costs", "cost", "more", "than", "budget", "flexible", "care",
    "about", "dont", "i", "for", "whatever", "whatevers", "any", "kind", "cheapest",
    "same", "permission", "using", "expired", "already", "used", "once", "simulate",
    "settlement", "mismatch", "testing", "immediately", "then", "is", "of", "with",
    "ratings", "rating", "or", "higher", "no", "stated", "don", "t", "s", "to", "on",
    "want", "would", "like", "please", "need", "some", "and", "without",
    # Soft price-sentiment words -- these describe a preference, not a product feature, so
    # they're stripped from query_terms (a product's own text will never contain the word
    # "cheap") and instead read separately by _prefers_cheap() to bias ranking toward
    # rating-per-rupee instead of raw rating.
    "cheap", "affordable", "inexpensive", "economical", "friendly", "low",
}

# Maps a colloquial/alternate word to the catalog's actual vocabulary, applied to both the
# query and product text so either spelling matches. Kept intentionally small and specific --
# each entry only fires on an exact word, so it can't introduce the kind of cross-category
# collision the length-gated fuzzy matcher above guards against (e.g. NOT mapping "phone"
# alone to anything, since that's shared across genuinely different products).
SYNONYMS = {
    "earphones": "earbuds", "earphone": "earbuds", "headset": "earbuds", "headsets": "earbuds",
    "notebook": "laptop", "notebooks": "laptop",
    "mobile": "phone", "cellphone": "phone", "smartphone": "phone",
    "cordless": "wireless",
    "watch": "smartwatch", "watches": "smartwatch",
    "cover": "case", "covers": "case",
    "charging": "charger", "chargers": "charger",
    "mic": "microphone",
    "gym": "fitness", "workout": "fitness", "exercise": "fitness",
    # Catalog descriptions use the industry abbreviation "ANC", not the words a shopper
    # actually types -- without this, "noise cancelling earbuds" finds nothing even though
    # Wireless Earbuds Pro Max is explicitly an ANC product.
    "noise": "anc", "cancelling": "anc", "canceling": "anc", "cancellation": "anc",
}


def _normalize(token: str) -> str:
    return SYNONYMS.get(token, token)


def _load_catalog():
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _tokenize(text: str) -> set:
    # .lower() here means case never matters -- "WIRELESS", "Wireless", and "wireless" all
    # tokenize identically, so capitalization/mixed case was never actually a problem.
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return {_normalize(t) for t in raw if t not in STOPWORDS}


def _doc_tokens(product: dict) -> set:
    """All searchable tokens for a product (name + category + description)."""
    text = f"{product['name']} {product['category'].replace('-', ' ')} {product['description']}"
    return _tokenize(text)


_CHEAP_PATTERN = re.compile(
    r"\bcheap\b|\baffordable\b|\binexpensive\b|\bbudget[- ]friendly\b|\blow[- ]cost\b|\beconomical\b",
    re.I,
)

# Everything after "with"/"without" up to whichever of these comes first -- a second
# with/without clause, a budget mention, a comparative-price clause, rating-floor language, or
# the end of the sentence. Deliberately lazy (*?) so it stops at the EARLIEST boundary, not the
# last one. The rating-floor words (above/at least/minimum/min/rated/stars/rating/higher) are
# required here too -- without them, "smartwatch with GPS above 4 stars" reads "above" and
# "stars" as literal required feature words (neither is a stopword), which no product's text
# will ever contain, silently zeroing the catalog -- confirmed as a real bug, not theoretical:
# reordering to "smartwatch above 4 stars with GPS" correctly found a match while the first
# phrasing didn't, purely because of which clause the boundary happened to hit first.
_CLAUSE_BOUNDARY = (
    r"(?=[.!?]|$|\bunder\b|\bbudget\b|\brupees?\b|\bprefer\b|\bwithout\b|\bwith\b|\bcheaper\b|\bthan\b"
    r"|\babove\b|\bat\s+least\b|\bminimum\b|\bmin\b|\brated\b|\bstars?\b|\brating\b|\bhigher\b)"
)
_WITH_PATTERN = re.compile(r"\bwith\s+([a-z0-9][a-z0-9,\s]*?)" + _CLAUSE_BOUNDARY, re.I)
_WITHOUT_PATTERN = re.compile(r"\bwithout\s+([a-z0-9][a-z0-9,\s]*?)" + _CLAUSE_BOUNDARY, re.I)


def _split_clause_into_phrases(clause: str) -> list:
    """"GPS, heart rate, and SOS button" -> ["GPS", "heart rate", "SOS button"] -- a with/
    without clause can name more than one feature, joined by "and" or commas."""
    return [p.strip() for p in re.split(r"\band\b|,", clause) if p.strip()]


def _phrase_feature_terms(phrase: str) -> list:
    """Turns one feature phrase ("heart rate", "GPS") into its required/excluded terms. If the
    phrase's first word is a stopword ("a phone case", "any hassle"), it's almost certainly
    not naming a feature at all -- ordinary sentence grammar -- so nothing is extracted rather
    than wrongly treating "a"/"any" as a hard requirement."""
    words = re.findall(r"[a-z0-9]+", phrase.lower())
    if not words:
        return []
    first = _normalize(words[0])
    if first in STOPWORDS or len(first) < 3:
        return []
    return [w for w in (_normalize(x) for x in words) if w not in STOPWORDS and len(w) >= 3]


def _extract_feature_filters(query: str) -> tuple:
    """Reads "with X"/"without X" as a hard requirement on X, not just a scoring bonus --
    "smartwatch with GPS" must have GPS, "smartwatch without GPS" must not. A clause can name
    multiple features ("with GPS and SpO2"), each captured independently."""
    require, exclude = set(), set()
    for m in _WITH_PATTERN.finditer(query):
        for phrase in _split_clause_into_phrases(m.group(1)):
            require.update(_phrase_feature_terms(phrase))
    for m in _WITHOUT_PATTERN.finditer(query):
        for phrase in _split_clause_into_phrases(m.group(1)):
            exclude.update(_phrase_feature_terms(phrase))
    return require, exclude


_CHEAPER_THAN_PATTERN = re.compile(r"cheaper than (?:the |a |an )?([a-z0-9 ]+?)(?=\s*(?:,|\.|!|\?|$|\band\b|\bbut\b))", re.I)


def _resolve_referenced_product(text: str, catalog: list):
    """Best-effort match of a free-text product reference ("the Smartwatch Pro" in "cheaper
    than the Smartwatch Pro") against real catalog product names, by token overlap against
    each product's own name (not its full description, to avoid a stray word coincidentally
    matching the wrong product). Returns None -- not a guess -- if nothing plausibly matches,
    so an unrecognized reference is silently ignored rather than corrupting the budget."""
    ref_terms = _tokenize(text)
    if not ref_terms:
        return None
    best, best_overlap = None, 0
    for p in catalog:
        overlap = len(ref_terms & _tokenize(p["name"]))
        if overlap > best_overlap:
            best, best_overlap = p, overlap
    return best


_QUANTITY_PHRASES = {
    "a pair of": 2, "pair of": 2, "a couple of": 2, "couple of": 2,
    "two": 2, "three": 3, "four": 4, "five": 5,
}
# A bare number right before one of these words is a rating floor ("4 stars"), not a
# quantity ("4 chargers") -- _extract_quantity's bare-number heuristic must not treat it as
# an item count, or "above 4 stars" would wrongly report "you asked for 4".
_RATING_WORDS = {"star", "stars", "rating", "ratings"}


def _extract_quantity(query: str) -> int:
    """How many units the shopper actually asked for -- "two wireless earbuds", "a pair of
    earbuds", "3 phone cases" -- defaulting to 1 when nothing is stated. Purely a signal
    surfaced on the result (quantity + a reason note); this function does not, by itself,
    make the purchase pipeline buy more than one unit -- see the reason text for why."""
    lowered = query.lower()
    for phrase, n in sorted(_QUANTITY_PHRASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            return n
    # A bare number only counts near the start of the request ("2 chargers", "get me 3
    # phone cases") -- a number found deep in the query is far more likely to be part of a
    # referenced product's own name ("...cheaper than the Smartwatch Fit 2 Pro") than a real
    # quantity, and the value is bounded so a budget figure ("2000 rupees") can't qualify.
    # _RATING_WORDS catches "4 stars" by checking the very next word, but that's not enough for
    # "rated 4 or higher" -- the word right after "4" there is "or", not a rating word. Cross-
    # checking directly against _extract_rating_floor's own parse (rather than trying to
    # enumerate every possible word that can follow a rating threshold) catches that case and
    # any future rating phrasing without needing to keep two guards in sync by hand.
    rating_floor = _extract_rating_floor(query)
    words = re.findall(r"\S+", lowered)
    for i, w in enumerate(words[:4]):
        if w.isdigit() and 1 <= int(w) <= 20 and i + 1 < len(words):
            if rating_floor is not None and float(w) == rating_floor:
                continue
            next_word = words[i + 1].strip(".,!?")
            if next_word[:1].isalpha() and next_word not in _RATING_WORDS:
                return int(w)
    return 1


_RATING_FLOOR_PATTERN = re.compile(
    r"(?:above|at\s+least|minimum(?:\s+of)?|min)\s+(\d(?:\.\d)?)\s*(?:stars?|rating)?"
    r"|(\d(?:\.\d)?)\s*\+\s*stars?"
    r"|(\d(?:\.\d)?)\s*stars?\s*(?:or\s+(?:higher|above)|and\s+above)"
    r"|rated\s+(\d(?:\.\d)?)\s*(?:or\s+(?:higher|above))?",
    re.I,
)


def _extract_rating_floor(query: str):
    """A hard minimum rating, if the query states one explicitly ("above 4 stars", "4.5+
    rating", "rated 4 or higher") -- previously "prefer good ratings" was pure stopword noise
    that enforced nothing; an explicit numeric floor like this is a real constraint a product
    can fail, not just a soft preference, so it's a hard filter like with/without, not a
    scoring nudge."""
    m = _RATING_FLOOR_PATTERN.search(query)
    if not m:
        return None
    value = next(g for g in m.groups() if g is not None)
    return float(value)


def _prefers_cheap(query: str) -> bool:
    """True for soft price language ("cheap but good", "affordable") that should bias the
    winner toward rating-per-rupee rather than raw rating -- distinct from is_cheapest_sort's
    "cheapest"/"lowest price", which means pick the outright lowest price, quality aside."""
    return bool(_CHEAP_PATTERN.search(query))


def _match_score(query_terms: set, product: dict) -> tuple:
    """(baseline, name_bonus) -- baseline is the plain count of distinct query terms that
    appear anywhere in the product (name, category, or description; this is the original,
    long-proven matching rule, unchanged). name_bonus only breaks ties *within* an equal
    baseline: how many of those matched terms are in the product's own name specifically,
    which is a stronger relevance signal than a word only mentioned in passing in the
    description. Deliberately NOT folded into one summed number -- an earlier version did
    that and let a narrow, name-concentrated match (2 terms, both in the name) outrank a
    broader match (3 distinct terms, mostly in the description) it shouldn't have. Tuple
    comparison means baseline always wins first; name_bonus is purely a tiebreaker."""
    doc_tokens = _doc_tokens(product)
    matched = query_terms & doc_tokens
    name_tokens = _tokenize(product["name"])
    return (len(matched), len(matched & name_tokens))


def _levenshtein(a: str, b: str) -> int:
    """Classic edit distance -- how many single-character insert/delete/substitute steps turn
    a into b. Used to tolerate typos: "wireles" vs "wireless" is distance 1."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev_row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr_row = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr_row.append(min(
                prev_row[j] + 1,        # deletion
                curr_row[j - 1] + 1,    # insertion
                prev_row[j - 1] + cost,  # substitution
            ))
        prev_row = curr_row
    return prev_row[-1]


def _fuzzy_token_match(query_token: str, doc_token: str) -> bool:
    """True if query_token plausibly means doc_token -- either it's a half-typed prefix
    ("wireles" for "wireless", "keybord" for "keyboard"), or it's within a typo's distance
    of it (scaled by length). Deliberately does nothing for words under 5 letters: a single
    edit on a short word covers too much of the English language to be a safe signal --
    "car" is one inserted letter from "card", one substitution from "can" or "cap", etc. A
    5+ letter word narrows that down enough to be a genuine typo/partial-word signal instead
    of a coincidence."""
    if len(query_token) < 5 or len(doc_token) < 5:
        return False
    if doc_token.startswith(query_token) or query_token.startswith(doc_token):
        return True
    tolerance = 1 if len(query_token) <= 6 else 2
    return _levenshtein(query_token, doc_token) <= tolerance


def _fuzzy_score(query_terms: set, doc_terms: set) -> float:
    """Fractional score (weaker than an exact hit) for how many query terms fuzzy-match some
    doc term. Only ever consulted when exact matching (in search_catalog) found nothing at
    all, so it can't override or weaken an already-confident exact match."""
    score = 0.0
    for qt in query_terms:
        if any(_fuzzy_token_match(qt, dt) for dt in doc_terms):
            score += 0.5
    return score


def search_catalog(query: str, max_budget_inr=None) -> dict:
    catalog = _load_catalog()
    query_terms = _tokenize(query)
    is_cheapest_sort = bool(re.search(r"cheapest|lowest.price|whatever.s cheapest", query.lower()))
    prefers_cheap = _prefers_cheap(query) and not is_cheapest_sort
    quantity = _extract_quantity(query)

    cheaper_than_match = _CHEAPER_THAN_PATTERN.search(query)
    if cheaper_than_match:
        ref_product = _resolve_referenced_product(cheaper_than_match.group(1), catalog)
        if ref_product is not None:
            implicit_cap = ref_product["price_inr"] - 1
            max_budget_inr = implicit_cap if max_budget_inr is None else min(max_budget_inr, implicit_cap)
            # The query text names the referenced product, so its own name would otherwise
            # dominate keyword matching and then get excluded by the very budget cap it set --
            # "cheaper than X" always implies not-X, so it's removed from candidates outright.
            catalog = [p for p in catalog if p["product_id"] != ref_product["product_id"]]

    require_terms, exclude_terms = _extract_feature_filters(query)
    if require_terms or exclude_terms:
        # Explicit "with X"/"without X" is a hard requirement, applied before any scoring --
        # a product missing a required feature (or carrying an excluded one) is never a
        # candidate at all, not just a lower-ranked one. Also requires overlap with whatever
        # query terms AREN'T part of the feature clause itself (when there are any) -- without
        # this, an unrelated product that merely happens to satisfy the feature words (e.g.
        # "Fast Charger 65W" satisfying "with fast charging" in a "phone case with fast
        # charging" query) could win despite having nothing to do with the actual product
        # being asked for.
        core_terms = query_terms - require_terms - exclude_terms
        catalog = [
            p for p in catalog
            if require_terms <= _doc_tokens(p) and not (exclude_terms & _doc_tokens(p))
            and (not core_terms or core_terms & _doc_tokens(p))
        ]
    feature_filtered_empty = (require_terms or exclude_terms) and not catalog

    rating_floor = _extract_rating_floor(query)
    if rating_floor is not None:
        catalog = [p for p in catalog if p["rating"] >= rating_floor]
    rating_filtered_empty = rating_floor is not None and not catalog and not feature_filtered_empty

    scored = []
    for p in catalog:
        score = _match_score(query_terms, p)
        if score[0] > 0:
            scored.append((score, p))

    fuzzy_fallback = False
    if not scored:
        # Nothing matched exactly -- try typo/half-typed-word tolerant matching before
        # giving up. This only ever runs when exact matching drew a total blank, so it can
        # never change the outcome of a query that already matches something for real.
        fuzzy_scored = []
        for p in catalog:
            score = _fuzzy_score(query_terms, _doc_tokens(p))
            if score > 0:
                fuzzy_scored.append((score, p))
        if fuzzy_scored:
            scored = fuzzy_scored
            fuzzy_fallback = True

    result = {"matches": [], "no_match_reason": None}

    if not scored:
        if feature_filtered_empty:
            parts = []
            if require_terms:
                parts.append(f"must include {', '.join(sorted(require_terms))}")
            if exclude_terms:
                parts.append(f"must not include {', '.join(sorted(exclude_terms))}")
            result["no_match_reason"] = (
                f"No products in the catalog satisfy the requested feature constraint ({'; '.join(parts)})."
            )
        elif rating_filtered_empty:
            result["no_match_reason"] = (
                f"No matching products have a rating of {rating_floor} or higher."
            )
        else:
            result["no_match_reason"] = "No products in the catalog match this request's category or description."
        log_event("shelf", "search", {"query": query, "max_budget_inr": max_budget_inr},
                   result, "ok")
        return result

    max_score = max(s for s, _ in scored)
    top_candidates = [p for s, p in scored if s == max_score]

    in_stock = [p for p in top_candidates if p["stock"] > 0]
    if not in_stock:
        names = ", ".join(p["name"] for p in top_candidates)
        result["no_match_reason"] = f"The best-matching product(s) for this request ({names}) are currently out of stock."
        log_event("shelf", "search", {"query": query, "max_budget_inr": max_budget_inr}, result, "ok")
        return result

    if max_budget_inr is not None:
        affordable = [p for p in in_stock if p["price_inr"] <= max_budget_inr]
    else:
        affordable = in_stock

    if not affordable:
        cheapest = min(in_stock, key=lambda p: p["price_inr"])
        names = ", ".join(f"{p['name']} ({p['price_inr']} INR)" for p in in_stock)
        result["no_match_reason"] = (
            f"The best-matching product(s) for this request ({names}) exceed the stated budget of "
            f"{max_budget_inr} INR; cheapest matching option is {cheapest['price_inr']} INR."
        )
        log_event("shelf", "search", {"query": query, "max_budget_inr": max_budget_inr}, result, "ok")
        return result

    if fuzzy_fallback:
        # Not confident enough to pick just one -- surface a short ranked range (up to 3) so
        # the agent can ask "did you mean X, Y, or Z?" instead of silently guessing.
        ranked = sorted(affordable, key=lambda p: p["rating"], reverse=True)[:3]
        result["matches"] = [{
            "product_id": p["product_id"],
            "name": p["name"],
            "price_inr": p["price_inr"],
            "rating": p["rating"],
            "reason": "possible match (closest spelling/partial match to your search, not an exact one)",
        } for p in ranked]
        log_event("shelf", "search", {"query": query, "max_budget_inr": max_budget_inr}, result, "ok")
        return result

    if is_cheapest_sort:
        winner = min(affordable, key=lambda p: p["price_inr"])
        reason = "lowest price among matching, in-stock, in-budget options (explicitly requested cheapest)"
    elif prefers_cheap:
        # "cheap but good" isn't "cheapest regardless of quality" -- rank by rating-per-rupee
        # instead of raw rating, so a well-rated budget option beats a marginally-better-rated
        # premium one.
        winner = max(affordable, key=lambda p: (p["rating"] / p["price_inr"], p["rating"]))
        reason = "best rating-for-price balance among matching, in-stock, in-budget products (you asked for something cheap but good)"
    else:
        winner = max(affordable, key=lambda p: (p["rating"], -p["price_inr"]))
        reason = "highest-rated option among matching, in-stock, in-budget products"

    if cheaper_than_match and max_budget_inr is not None:
        reason += f" (kept under {max_budget_inr + 1} INR, cheaper than the referenced item)"
    if quantity > 1:
        # Detected, not fulfilled -- the purchase pipeline (Guardrail/Parity) only ever
        # transacts a single unit per call, so being explicit here beats silently ignoring
        # the requested quantity, which is what happened before this existed.
        reason += (
            f" -- note: you asked for {quantity}, but only a single-unit purchase can be "
            "executed right now; price shown is per unit"
        )

    result["matches"] = [{
        "product_id": winner["product_id"],
        "name": winner["name"],
        "price_inr": winner["price_inr"],
        "rating": winner["rating"],
        "reason": reason,
        "quantity_requested": quantity,
    }]
    log_event("shelf", "search", {"query": query, "max_budget_inr": max_budget_inr}, result, "ok")
    return result
