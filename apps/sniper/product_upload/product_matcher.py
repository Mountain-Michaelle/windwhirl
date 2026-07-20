"""
product_matcher.py

Turns a messy, free-text Excel product description into zero or more
sellable CRM product names, the way an experienced staff member reads it:
strip promotional freebies, split multiple purchased items apart, and
fuzzy-match whatever's left against the real CRM product list.

Nothing here talks to the browser. It's pure text logic so it can be
unit-tested without Playwright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional

try:
    from rapidfuzz import fuzz as _rf_fuzz
    _HAVE_RAPIDFUZZ = True
except ImportError:
    _HAVE_RAPIDFUZZ = False


# ==============================================================================
# CONFIDENCE THRESHOLDS  (business logic spec, section 18)
# ==============================================================================
HIGH_CONFIDENCE = 0.85
MEDIUM_CONFIDENCE = 0.60
# below MEDIUM_CONFIDENCE => reject, log failure, never guess


# ==============================================================================
# PROMOTIONAL / NON-SELLABLE ITEMS  (business logic spec, section 6)
# ==============================================================================
# Phrases here are never a real CRM product. Anything that normalizes to one
# of these (or is a very close fuzzy match to one) is dropped before matching
# ever starts.
PROMO_PHRASES = [
    "free gift",
    "free soap",
    "free sample",
    "hand cream",
    "scar cream",
    "scar repair cream",
    "face towel",
    "free scar repair cream",
    "1 free scar repair cream",
    "2 free scar repair cream",
    "free spa facial mask",
    "free doorstep delivery",
    "2 free spa facial mask",
    "2 free spa facial masks",
    "1 free spa facial mask",
    "2 free spa facial masks",
    "2 free scar repair creams",
    "2 free scar repair cream",
    "free collagen hand cream",
    "1 free collagen hand cream",
    "1 free collagen hand creams",
    "2 free collagen hand cream",
    "1 free collagen hand creams",    
    "bonus",
    "promo",
    "sample",
    "gift",
    "freebie",
    "free spa"
]

# ------------------------------------------------------------------------------
# Trailing price stripping
# ------------------------------------------------------------------------------
# The exported Product cell has the order's total price glued onto the end,
# e.g. "... Free Collagen Hand Cream — ₦28,500". The comma inside that price
# is indistinguishable from a real item-separator comma once split_line_items
# runs -- "28,500" becomes two fake line items, "28" and "500", the second of
# which then fails to match anything and (per the "reject the whole order on
# any unmatched fragment" rule) sinks an otherwise-fine order. Stripping the
# price suffix before any splitting happens removes that comma entirely.
#
# Matches a dash/em-dash/en-dash or "=" at the END of the string, followed by
# an optional naira sign or "#", then a run of digits with optional comma
# thousands-separators. Anchored to $ so it never touches a mid-string dash
# like the one in "Combo set -(1 serum & 1 Cream)".
_PRICE_SUFFIX_RE = re.compile(r"[-\u2013\u2014=]\s*[\u20a6#]\s*[\d][\d,]*(?:\.\d+)?\s*$")


def strip_trailing_price(text: str) -> str:
    return _PRICE_SUFFIX_RE.sub("", text).strip()


# ------------------------------------------------------------------------------
# Known aliases  (business decision, not a text-matching problem)
# ------------------------------------------------------------------------------
# Keys MUST be the exact output of normalize_product_text(<Excel phrase>) --
# lowercase, punctuation stripped, "&"->"and", duplicate words collapsed.
# To add a new one: call normalize_product_text("your Excel phrase") to get
# the key, then set the value to the CRM product's exact label text.
#
# An alias hit skips fuzzy scoring entirely and returns confidence 1.0 --
# so only add a line here once you've actually confirmed the mapping is
# correct. A wrong guess here ships a real order under the wrong product;
# a wrong fuzzy-match guess just gets rejected as low-confidence and lands
# in manual_check for a human to catch. That asymmetry is why these start
# empty/marked GUESS rather than me filling in every one I can pattern-match.
#
# Entries below marked GUESS are my best read of your Excel<->CRM pairing
# discussion -- confirm each one against the live #selpro dropdown before
# trusting it. Entries marked CONFIRMED are near-exact text matches.
KNOWN_ALIASES: dict[str, str] = {
    # -- Advanced Collagen Body Lotion bundle (qty 1/2/3) --------------------
    "1 advanced collagen body lotion": "Sadoer Collagen Combo + Body Lotion", 
    "2 advanced collagen body lotions": "Sadoer Collagen Combo + Body Lotion", 
    "3 advanced collagen body lotions": "Sadoer Collagen Combo + Body Lotion",     
    "collagen body lotion": "Sadoer Collagen Combo + Body Lotion",    
    
    "1 advanced salisylic acid": "Sadoer Salicylic Acne Repair Set", 
    "Sadoer collagen face cream and serum": "Sadoer Collagen Combo Set",   
    "sadoer collagen face serum": "Sadoer Collagen Serum", 
    "sadoer collagen face cream": "Sadoer Collagen Serum",  # GUESS (misspelling: "Sadoer" vs "Sadeor")    
    
    "1 cumbo salicylic acid":"Sadoer Salicylic Acne Repair Set + Body Lotion NEW",
    "salicylic acid": "Sadoer Salicylic Acne Repair Set + Body Lotion NEW",  # GUESS (misspelling: "salisylic")
    "1 Combo set -(1 serum & 1 Cream)": "Sadoer Collagen Combo Set",  # GUESS (misspelling: "Combo set" vs "Collagen Combo Set")
    
    # -- "Combo set" (1 serum & 1 cream), incl. misspellings ------------------
    "1 combo set serum and cream": "Sadoer Collagen Combo Set",   # GUESS
    "1 cumbo set serum and cream": "Sadoer Collagen Combo Set",   # GUESS (misspelling: "Cumbo")
    "2 combo set": "Sadoer Collagen Combo Set",                   # GUESS
    "1sadeor collagen combo set": "Sadoer Collagen Combo Set",    # GUESS (misspelling: "Sadeor")

    # -- Near-exact text matches ----------------------------------------------
    "sadoer collagen serum": "Sadoer Collagen Serum",             # CONFIRMED (exact text match)

    # -- Intentionally NOT mapped yet: no confident target in the catalog
    # list you sent (it may just be past where the dropdown paste got cut
    # off). Left out on purpose so these keep failing into manual_check
    # instead of silently landing on a guessed product:
    #   "sadoer collagen face cream": "???",
    #   "sadoer collagen face cream and serum": "???",
}


# Business rule: Excel descriptions often start with a leading quantity
# number ("1 Advanced...", "2 Cumbo..."). Extract it; default to 1 if
# there's no leading number. Runs on RAW text (before normalization) so
# it sees the number exactly as written.
_LEADING_QTY_RE = re.compile(r"^\s*(\d+)\b")

def extract_quantity(raw_line_item: str) -> int:
    if not raw_line_item:
        return 1
    m = _LEADING_QTY_RE.match(raw_line_item.strip())
    if m:
        try:
            qty = int(m.group(1))
            return qty if qty > 0 else 1
        except ValueError:
            return 1
    return 1


def _find_catalog_label(target: str, crm_catalog: list[str]) -> Optional[str]:
    """Case/whitespace-insensitive lookup of an alias's target name against
    the LIVE-scraped catalog, so an alias hit still uses whatever exact
    string is currently in the #selpro dropdown (source of truth) rather
    than whatever was hardcoded in KNOWN_ALIASES -- if the CRM ever renames
    the product, this returns None and match_product falls back to normal
    fuzzy matching instead of submitting a stale/wrong label."""
    target_norm = " ".join(target.split()).lower()
    for name in crm_catalog:
        if " ".join(name.split()).lower() == target_norm:
            return name
    return None


@dataclass
class MatchResult:
    excel_text: str          # the raw candidate line item as it appeared in Excel
    normalized: str          # normalized form used for matching
    matched_product: Optional[str]   # CRM product name, or None if rejected
    confidence: float
    confidence_band: str     # "high" | "medium" | "low"
    reason: str = ""         # populated when rejected
    reason: str = ""          # (existing line, unchanged)
    quantity: int = 1         # NEW -- appended last, default-only, backward compatible


# ------------------------------------------------------------------------------
# Normalization  (business logic spec, section 5)
# ------------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[^\w\s]")
_MULTISPACE_RE = re.compile(r"\s+")


def normalize_product_text(text: str) -> str:
    """lowercase, strip punctuation/brackets, '&' -> 'and', collapse spaces,
    drop duplicate words (keeping first occurrence / word order)."""
    if not text:
        return ""
    t = text.lower()
    t = t.replace("&", " and ")
    t = _PUNCT_RE.sub(" ", t)
    t = _MULTISPACE_RE.sub(" ", t).strip()

    seen = set()
    deduped_words = []
    for word in t.split(" "):
        if word not in seen:
            seen.add(word)
            deduped_words.append(word)
    return " ".join(deduped_words)


# ------------------------------------------------------------------------------
# Splitting multiple purchased items apart  (business logic spec, section 7)
# ------------------------------------------------------------------------------
# Only split on "+" and "," -- NOT on "&"/"and", since real CRM product names
# legitimately contain "&" (e.g. "Whitening Face & Body Combo"). Splitting on
# those would shred a single product into two garbage fragments.
_SPLIT_RE = re.compile(r"[+,]")


def split_line_items(raw_excel_product_text: str) -> list[str]:
    if not raw_excel_product_text:
        return []
    parts = _SPLIT_RE.split(raw_excel_product_text)
    return [p.strip() for p in parts if p.strip()]


# ------------------------------------------------------------------------------
# Promotional item filtering  (business logic spec, section 6)
# ------------------------------------------------------------------------------
def _promo_similarity(normalized_candidate: str) -> float:
    """Highest similarity between the candidate and any known promo phrase."""
    best = 0.0
    for phrase in PROMO_PHRASES:
        if phrase in normalized_candidate:
            return 1.0
        best = max(best, SequenceMatcher(None, normalized_candidate, phrase).ratio())
    return best


def is_promotional_item(raw_line_item: str, threshold: float = 0.80) -> bool:
    normalized = normalize_product_text(raw_line_item)
    return _promo_similarity(normalized) >= threshold


def strip_promotional_items(line_items: Iterable[str]) -> list[str]:
    return [item for item in line_items if not is_promotional_item(item)]


# ------------------------------------------------------------------------------
# Matching engine  (business logic spec, section 4)
# ------------------------------------------------------------------------------
def _token_overlap_score(candidate_norm: str, catalog_norm: str) -> float:
    """Containment-oriented score: how much of the (usually shorter) Excel
    description's meaning is present in the CRM product name. Falls back to
    per-token fuzzy matching for spelling variations / abbreviations
    (e.g. 'gluta' ~ 'glutathione')."""
    cand_tokens = candidate_norm.split()
    cat_tokens = set(catalog_norm.split())
    if not cand_tokens:
        return 0.0

    matched = 0.0
    for tok in cand_tokens:
        if tok in cat_tokens:
            matched += 1.0
            continue
        # abbreviation / spelling-variation tolerance
        best_tok_score = max(
            (SequenceMatcher(None, tok, cat_tok).ratio() for cat_tok in cat_tokens),
            default=0.0,
        )
        prefix_bonus = any(
            len(tok) >= 4 and (cat_tok.startswith(tok) or tok.startswith(cat_tok))
            for cat_tok in cat_tokens
        )
        if prefix_bonus:
            best_tok_score = max(best_tok_score, 0.85)
        if best_tok_score >= 0.6:
            matched += best_tok_score
    return matched / len(cand_tokens)


def _full_string_score(candidate_norm: str, catalog_norm: str) -> float:
    if _HAVE_RAPIDFUZZ:
        # token_set_ratio tolerates reordering, missing/extra words -- exactly
        # the tolerance the business spec asks for.
        return _rf_fuzz.token_set_ratio(candidate_norm, catalog_norm) / 100.0
    return SequenceMatcher(None, candidate_norm, catalog_norm).ratio()


def score_product_candidate(candidate_norm: str, catalog_norm: str) -> float:
    """Blend containment score (good for 'Collagen Combo' -> 'Sadoer Collagen
    Combo Set') with a full-string fuzzy score (catches reordering /
    misspellings), weighted toward containment since Excel descriptions are
    almost always shorter than the real CRM name."""
    containment = _token_overlap_score(candidate_norm, catalog_norm)
    full = _full_string_score(candidate_norm, catalog_norm)
    return 0.7 * containment + 0.3 * full


def match_product(raw_line_item: str, crm_catalog: list[str]) -> MatchResult:
    """Match one already-split, already-filtered line item against the CRM
    product catalog. `crm_catalog` is the list of real, sellable CRM product
    names (exactly as they appear in the #selpro dropdown)."""
    normalized = normalize_product_text(raw_line_item)

    if not normalized:
        return MatchResult(raw_line_item, normalized, None, 0.0, "low",
                            reason="empty after normalization")
        
    # NEW -- check KNOWN_ALIASES before any fuzzy scoring happens.
    alias_target = KNOWN_ALIASES.get(normalized)
    if alias_target is not None:
        catalog_label = _find_catalog_label(alias_target, crm_catalog)
        if catalog_label is not None:
            return MatchResult(raw_line_item, normalized, catalog_label, 1.0, "high",
                                reason=f"matched via known alias -> {catalog_label!r}")
        # alias target isn't in the live catalog right now (renamed/removed) --
        # fall through to normal fuzzy matching rather than silently failing.     

    scored = sorted(
        ((crm_name, score_product_candidate(normalized, normalize_product_text(crm_name)))
         for crm_name in crm_catalog),
        key=lambda pair: pair[1],
        reverse=True,
    )
    if not scored:
        return MatchResult(raw_line_item, normalized, None, 0.0, "low",
                            reason="empty CRM catalog")

    best_name, best_score = scored[0]

    if best_score >= HIGH_CONFIDENCE:
        return MatchResult(raw_line_item, normalized, best_name, best_score, "high")

    if best_score >= MEDIUM_CONFIDENCE:
        # Secondary matching pass (business spec section 18): recompute with
        # a different algorithm; only keep the match if it agrees.
        secondary_best_name, secondary_score = max(
            ((name, _full_string_score(normalized, normalize_product_text(name)))
             for name in crm_catalog),
            key=lambda pair: pair[1],
        )
        if secondary_best_name == best_name and secondary_score >= MEDIUM_CONFIDENCE:
            return MatchResult(raw_line_item, normalized, best_name, best_score, "medium")
        return MatchResult(
            raw_line_item, normalized, None, best_score, "low",
            reason=(f"medium-confidence match ({best_name!r}, {best_score:.2f}) "
                    "did not agree with secondary matching pass -- rejected"),
        )

    return MatchResult(
        raw_line_item, normalized, None, best_score, "low",
        reason=f"low confidence (best candidate {best_name!r} scored {best_score:.2f})",
    )


def parse_order_products(raw_excel_product_text, crm_catalog: list[str]) -> list[MatchResult]:
    """Full pipeline, in the order a human would actually read the text:

    0. Strip the trailing "— ₦28,500"-style price off the end (see
       strip_trailing_price docstring) BEFORE anything else touches the
       text -- otherwise the comma inside the price gets treated as an
       item separator.
    1. Try matching the WHOLE (price-stripped) string as a single product
       first. Some real CRM products contain "+"/"," in their own name
       (e.g. "Sadoer Collagen Combo + Body Lotion"), so an Excel
       description like "Collagen Combo + Lotion" must NOT be blindly
       split -- it's one bundled product, not two.
    2. Only if the whole string doesn't confidently match anything, fall
       back to splitting on "+"/"," and filtering out promotional items --
       this is what correctly handles "Collagen Combo + Hand Cream + Free
       Soap" (three fragments, two of which are freebies to discard).
    """
    # pandas hands back NaN (a float) for an empty Excel cell, not "" --
    # isinstance check first so that never reaches .strip() and crashes.
    if not isinstance(raw_excel_product_text, str) or not raw_excel_product_text.strip():
        return []

    cleaned_text = strip_trailing_price(raw_excel_product_text)
    if not cleaned_text:
        return []

    whole_match = match_product(cleaned_text, crm_catalog)
    whole_match.quantity = extract_quantity(cleaned_text)          # NEW
    if whole_match.matched_product is not None and whole_match.confidence_band == "high":
        return [whole_match]

    line_items = split_line_items(cleaned_text)
    sellable_items = strip_promotional_items(line_items)
    results = [match_product(item, crm_catalog) for item in sellable_items]
    for r, item in zip(results, sellable_items):                   # NEW
        r.quantity = extract_quantity(item)                        # NEW
    return results