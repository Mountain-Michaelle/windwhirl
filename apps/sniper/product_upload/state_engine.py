"""
state_engine.py

Implements business logic spec section 11: the Excel "State" column is not
trusted. The state to select in the CRM's #state dropdown is inferred from
the free-text customer address instead, exactly like a human reading the
address would do it.
"""

from __future__ import annotations

import re
from typing import Optional

# Canonical state names, as they should appear as #state dropdown option
# text/values on SniperCRM. Adjust the canonical spelling here if the live
# dropdown uses different labels (e.g. "FCT" vs "Abuja").
NIGERIAN_STATES = [
    "Abia", "Adamawa", "Akwa Ibom", "Anambra", "Bauchi", "Bayelsa", "Benue",
    "Borno", "Cross River", "Delta", "Ebonyi", "Edo", "Ekiti", "Enugu",
    "Gombe", "Imo", "Jigawa", "Kaduna", "Kano", "Katsina", "Kebbi", "Kogi",
    "Kwara", "Lagos", "Nasarawa", "Niger", "Ogun", "Ondo", "Osun", "Oyo",
    "Plateau", "Rivers", "Sokoto", "Taraba", "Yobe", "Zamfara", "FCT",
]

# Common alternate spellings / ways a state gets written in a hand-typed
# address, mapped to the canonical name above.
_STATE_ALIASES = {
    "abuja": "FCT",
    "federal capital territory": "FCT",
    "akwaibom": "Akwa Ibom",
    "crossriver": "Cross River",
    "river state": "Rivers",  # common informal spelling, missing the "s"
}

# A small set of well-known cities / universities / landmarks that let us
# infer the state even when the address never spells the state name out.
# This is deliberately conservative -- it only covers unambiguous, well
# known places. Extend as real order data reveals more patterns.
_LANDMARK_TO_STATE = {
    "nsukka": "Enugu",
    "university of nigeria": "Enugu",
    "unn": "Enugu",
    "alvan ikoku": "Imo",
    "owerri": "Imo",
    "futo": "Imo",
    "ikeja": "Lagos",
    "lekki": "Lagos",
    "yaba": "Lagos",
    "surulere": "Lagos",
    "unilag": "Lagos",
    "ikorodu": "Lagos",
    "badagry": "Lagos",
    "ibadan": "Oyo",
    "ui ": "Oyo",
    "uniben": "Edo",
    "benin city": "Edo",
    "nnamdi azikiwe": "Anambra",
    "unizik": "Anambra",
    "awka": "Anambra",
    "obosi": "Anambra",
    "achina": "Anambra",
    "port harcourt": "Rivers",
    "portharcourt": "Rivers",
    "uniport": "Rivers",
    "omoku": "Rivers",
    "abuja": "FCT",
    "kano city": "Kano",
    "ahmadu bello": "Kaduna",
    "abu zaria": "Kaduna",
    "zaria": "Kaduna",
    "calabar": "Cross River",
    "unical": "Cross River",
    "ikom": "Cross River",
    "ilorin": "Kwara",
    "unilorin": "Kwara",
    "warri": "Delta",
    "asaba": "Delta",
    "agbor": "Delta",
    "ughoton": "Delta",
    "abakaliki": "Ebonyi",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def infer_state(address: str) -> Optional[str]:
    """Returns the canonical state name found in `address`, or None if no
    state could be confidently inferred. Never guesses -- an unresolved
    address should be treated as a failure to log, not a coin flip
    (business logic spec section 18)."""
    if not address:
        return None
    norm = _normalize(address)

    # 1. Explicit state name (with or without the word "state" after it)
    for state in NIGERIAN_STATES:
        pattern = r"\b" + re.escape(state.lower()) + r"\b"
        if re.search(pattern, norm):
            return state

    # 2. Known alternate spellings
    for alias, canonical in _STATE_ALIASES.items():
        if alias in norm:
            return canonical

    # 3. Well-known landmark / city fallback
    for landmark, canonical in _LANDMARK_TO_STATE.items():
        if landmark in norm:
            return canonical

    return None