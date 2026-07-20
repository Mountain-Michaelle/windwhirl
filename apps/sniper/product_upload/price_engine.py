"""
price_engine.py

Implements business logic spec section 8 (price decision engine) and
section 9 (quantity rules).

After a product is selected in #selpro, SniperCRM populates #pricevar with
one option per default price/quantity bundle for that product (e.g.
"2 30ml & 100g NGN52000"), plus a literal "Custom" option. This module reads
those live options from the page and decides, exactly the way a human
staff member would:

  * Excel price matches a bundle's CRM price -> select that bundle,
    NEVER touch custom price.
  * Excel price matches nothing -> select "Custom", then fill quantity and
    price with EXACTLY what's in Excel (never invented, never rounded).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from apps.sniper.utils.field_selectors import CUSTOM_PRICE_OPTION_LABEL

_PRICE_RE = re.compile(r"NGN\s?([\d,]+)", re.IGNORECASE)


@dataclass
class PriceVarOption:
    label: str
    value: str
    price: Optional[int]  # None if the option has no parseable NGN price (e.g. "Select", "Custom")


@dataclass
class PriceDecision:
    use_custom: bool
    option_label: str          # the #pricevar option to select
    option_value: Optional[str]
    custom_qty: Optional[str] = None    # only set when use_custom
    custom_price: Optional[str] = None  # only set when use_custom
    reason: str = ""


def parse_pricevar_option(label: str, value: str) -> PriceVarOption:
    m = _PRICE_RE.search(label)
    price = int(m.group(1).replace(",", "")) if m else None
    return PriceVarOption(label=label, value=value, price=price)


def read_pricevar_options(page, selector: str = "#pricevar") -> list[PriceVarOption]:
    """Reads the live <option> list out of the #pricevar dropdown. Must be
    called AFTER selecting the product and after both network calls that
    populate it have resolved (see add_products.select_product_line)."""
    raw = page.eval_on_selector_all(
        f"{selector} option",
        "els => els.map(e => ({label: e.textContent.trim(), value: e.value}))",
    )
    return [parse_pricevar_option(o["label"], o["value"]) for o in raw]


def decide_price(
    excel_price,
    excel_qty,
    options: list[PriceVarOption],
    always_custom: bool = True,      # NEW -- default matches your request
) -> PriceDecision:
    """Business rule 8, applied to whatever bundles the CRM actually offers
    for this product right now (never a static/stale price list)."""
    try:
        excel_price_int = int(round(float(str(excel_price).replace(",", "").strip())))
    except (TypeError, ValueError):
        return PriceDecision(
            use_custom=False, option_label="", option_value=None,
            reason=f"could not parse Excel price {excel_price!r}",
        )

    if not always_custom:            # NEW guard around your existing loop
        for opt in options:
            if opt.price is not None and opt.price == excel_price_int:
                return PriceDecision(
                    use_custom=False,
                    option_label=opt.label,
                    option_value=opt.value,
                    reason=f"Excel price {excel_price_int} matches CRM default bundle {opt.label!r}",
                )
                
    custom_opt = next(
        (o for o in options if o.label.strip().lower() == CUSTOM_PRICE_OPTION_LABEL.lower()),
        None,
    )
    if custom_opt is None:
        return PriceDecision(
            use_custom=False, option_label="", option_value=None,
            reason="no CRM default bundle matched Excel price, and no 'Custom' "
                   "option was found in #pricevar -- cannot safely proceed",
        )

    qty_str = str(excel_qty).strip() if excel_qty not in (None, "") else None
    if qty_str is None:
        return PriceDecision(
            use_custom=True, option_label=custom_opt.label, option_value=custom_opt.value,
            reason="Excel price did not match any default bundle, but Excel qty "
                   "is missing -- cannot fill Qty_C[] without inventing a value",
        )

    return PriceDecision(
        use_custom=True,
        option_label=custom_opt.label,
        option_value=custom_opt.value,
        custom_qty=qty_str,
        custom_price=str(excel_price_int),
        reason=f"Excel price {excel_price_int} did not match any CRM default bundle "
               f"-- using Custom with qty={qty_str}, price={excel_price_int}",
    )