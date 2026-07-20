"""
add_products.py

Core order-filling engine. Given one already-loaded add_multi_order page and
one parsed Excel order row, this fills the form exactly the way a trained
staff member would: match products, decide default-vs-custom price, fill
customer info, infer state, submit, wait like a human, verify success, and
report back a structured OrderResult.

Does NOT own the browser, the session, or the input file -- those are
run_automation.py's job. This module only knows how to do one order at a
time on a page it's handed.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Optional

import apps.sniper.utils.field_selectors as sel
from apps.sniper.utils.field_selectors import locator
from apps.sniper.product_upload.product_matcher import parse_order_products, MatchResult
from apps.sniper.product_upload.price_engine import read_pricevar_options, decide_price
from apps.sniper.product_upload.state_engine import infer_state
from apps.sniper.product_upload.failure_logger import FailedOrder


# ==============================================================================
# CONFIG
# ==============================================================================
@dataclass
class AutomationConfig:
    crm_catalog: list[str]                       # real CRM product names for matching
    typing_delay_ms: tuple[int, int] = (40, 120)  # per-keystroke jitter
    step_delay_s: tuple[float, float] = (0.4, 1.2)  # between discrete actions
    post_save_delay_s: tuple[int, int] = (1, 20)  # business spec section 13
    network_wait_timeout_ms: int = 20000
    success_wait_timeout_ms: int = 15000
    max_retries_on_inconclusive: int = 1
    always_custom_price: bool = True   # NEW -- always use the Custom #pricevar
                                        # option instead of trying to match a
                                        # default bundle price that might not
                                        # exist for this product

     # NEW -- single global multiplier for input pacing only.
    # 1.0 = current speed, 0.5 = 2x faster, 2.0 = 2x slower.
    # Does NOT touch network/success timeouts, post-save delay, or retries.
    speed_multiplier: float = 1.0
    
    def __post_init__(self):
        lo, hi = self.typing_delay_ms
        self.typing_delay_ms = (lo * self.speed_multiplier, hi * self.speed_multiplier)
        lo, hi = self.step_delay_s
        self.step_delay_s = (lo * self.speed_multiplier, hi * self.speed_multiplier)

    # Tune these once against the live site -- see detect_save_outcome().
    success_selectors: tuple[str, ...] = (
        ".swal2-success", ".toast-success", ".alert-success",
        "text=Order added successfully", "text=Order saved successfully",
    )
    error_selectors: tuple[str, ...] = (
        ".swal2-error", ".toast-error", ".alert-danger", ".alert-error",
        "text=already exists", "text=validation error",
    )


@dataclass
class OrderResult:
    success: bool
    row_number: Optional[int]
    reason: str = ""
    matched_products: list[str] = field(default_factory=list)


# ==============================================================================
# HUMAN-LIKE PACING  (business logic spec section 13)
# ==============================================================================
def _human_pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _human_type(page, target_locator, text: str, cfg: AutomationConfig) -> None:
    lo, hi = cfg.typing_delay_ms
    target_locator.click()
    target_locator.fill("")  # clear first, then type character by character
    target_locator.type(str(text), delay=random.randint(lo, hi))


def _wait_network(page, timeout_ms: int) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        # A slow/never-idle page (polling, websockets) isn't necessarily a
        # failure -- the caller still verifies the actual DOM state next.
        pass


# ==============================================================================
# PRODUCT LINES
# ==============================================================================
def _add_one_product_line(page, match: MatchResult, excel_qty, excel_price,
                           cfg: AutomationConfig) -> Optional[str]:
    """Fills one #selpro/#pricevar block for an already-matched product.
    Returns an error reason string, or None on success."""
    product_field = locator(page, sel.SELECT_PRODUCT)
    product_field.wait_for(state="visible", timeout=cfg.network_wait_timeout_ms)
    product_field.select_option(label=match.matched_product)
    _human_pause(*cfg.step_delay_s)

    # Two network calls populate #pricevar after a product is chosen
    # (recorded steps 2-3 / 6-7). `options` MUST be read AFTER these settle
    # and after #pricevar is confirmed visible -- reading it any earlier
    # returns a stale/empty list that's missing even the "Custom" option,
    # which is what was producing "no 'Custom' option was found in
    # #pricevar" even on products where Custom is always there live.
    _wait_network(page, cfg.network_wait_timeout_ms)
    _wait_network(page, cfg.network_wait_timeout_ms)

    pricevar_field = locator(page, sel.SELECT_PRICEVAR)
    pricevar_field.wait_for(state="visible", timeout=cfg.network_wait_timeout_ms)

    options = read_pricevar_options(page, sel.SELECT_PRICEVAR.selector)
    if not options:
        return f"'{match.matched_product}': #pricevar had no options after selection"

    decision = decide_price(excel_price, excel_qty, options, always_custom=cfg.always_custom_price)
    if decision.option_value is None and not decision.use_custom:
        return f"'{match.matched_product}': {decision.reason}"

    pricevar_field.select_option(value=decision.option_value or decision.option_label)
    _human_pause(*cfg.step_delay_s)

    if decision.use_custom:
        if decision.custom_qty is None or decision.custom_price is None:
            return f"'{match.matched_product}': {decision.reason}"
        qty_field = locator(page, sel.QTY_CUSTOM)
        qty_field.wait_for(state="visible", timeout=cfg.network_wait_timeout_ms)
        _human_type(page, qty_field, decision.custom_qty, cfg)
        _human_pause(*cfg.step_delay_s)

        price_field = locator(page, sel.PRICE_CUSTOM)
        price_field.wait_for(state="visible", timeout=cfg.network_wait_timeout_ms)
        _human_type(page, price_field, decision.custom_price, cfg)
        _human_pause(*cfg.step_delay_s)

    return None


# ==============================================================================
# CUSTOMER INFO  (business logic spec section 10)
# ==============================================================================
def _clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN check (NaN != NaN is
        return ""                                    # always True) -- avoids
                                                       # needing a pandas import
                                                       # here just for this
    return " ".join(str(value).split())


def _clean_phone(value) -> str:
    """Like _clean_text, but numeric-safe for phone/WhatsApp columns.
    Excel often stores long phone numbers as actual NUMBERS rather than
    text, so pandas hands them back as a float -- e.g. a cell showing
    "2347017367451" comes back as the float 2347017367451.0. Python's
    str() on a whole-number float ALWAYS appends a trailing ".0"
    ('2347017367451.0'), which is exactly the kind of malformed value a
    phone-format validator on the CRM side would reject or silently
    strip -- looking, from the outside, exactly like "the field just
    won't take a value" even though typing itself succeeded. Converting
    a whole-number float through int() first removes the ".0" entirely."""
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value.is_integer():
            value = int(value)
    return " ".join(str(value).split())


def _type_and_verify(page, field_def, value: str, cfg: AutomationConfig, label: str,
                      max_attempts: int = 3) -> Optional[str]:
    """Types into a text field and immediately reads the value back,
    retrying the whole click+clear+type sequence if it comes back empty.
    This is what phone specifically needed: `.type()` was completing
    without raising, but something on the CRM side (an on-blur/on-change
    formatter or duplicate-check handler) was wiping the field right
    after -- the later end-of-form check caught it, but only after the
    ENTIRE rest of the form (product, price, name, address, state) had
    already been filled. Verifying right after each field, with a retry,
    catches it immediately instead of wasting the whole flow first."""
    loc = locator(page, field_def)
    loc.wait_for(state="visible", timeout=cfg.network_wait_timeout_ms)

    for attempt in range(1, max_attempts + 1):
        _human_type(page, loc, value, cfg)
        _human_pause(*cfg.step_delay_s)
        try:
            seen = loc.input_value().strip()
        except Exception as e:
            if attempt == max_attempts:
                return f"could not read back {label!r} after typing (attempt {attempt}): {e}"
            continue
        if seen:
            return None  # success

    return (f"{label!r} came back empty after {max_attempts} attempt(s) to type "
            f"{value!r} into it -- something on the page is rejecting or "
            "clearing this field")


def _select_and_verify(page, field_def, option_label: str, cfg: AutomationConfig,
                        label: str, max_attempts: int = 2) -> Optional[str]:
    """Same protection as _type_and_verify, for <select> dropdowns (state,
    payment method) -- selects, reads the value back, retries if empty."""
    loc = locator(page, field_def)
    loc.wait_for(state="visible", timeout=cfg.network_wait_timeout_ms)

    for attempt in range(1, max_attempts + 1):
        loc.select_option(label=option_label)
        _human_pause(*cfg.step_delay_s)
        try:
            seen = loc.input_value().strip()
        except Exception as e:
            if attempt == max_attempts:
                return f"could not read back {label!r} after selecting (attempt {attempt}): {e}"
            continue
        if seen:
            return None  # success

    return (f"{label!r} came back empty after {max_attempts} attempt(s) to "
            f"select {option_label!r}")


def _fill_customer_info(page, order: dict, cfg: AutomationConfig) -> Optional[str]:
    """Fill order: name, address, phone, whatsapp -- matches the required
    overall sequence (product, price, name, address, phone, whatsapp,
    state, payment, save). Returns an error string and stops at the FIRST
    field that fails to fill, instead of silently continuing through the
    rest of the form on a field that never actually took."""
    name = _clean_text(order.get("customer_name"))
    address = _clean_text(order.get("address"))
    phone = _clean_text(order.get("phone_number"))          # was "phone"


    # Fall back to phone when whatsapp is truly blank. Deliberately NOT
    # `order.get("whatsapp") or order.get("phone")` -- a blank Excel cell
    # comes back from pandas as NaN, and NaN is truthy in Python, so that
    # `or` would never actually fall through to phone for a genuinely
    # empty WhatsApp cell. Check the CLEANED (NaN-safe) value instead.
    whatsapp_clean = _clean_text(order.get("whatsapp_number"))   # was "whatsapp"
    whatsapp = whatsapp_clean if whatsapp_clean else phone

    for field_def, value, label in (
        (sel.CUSTOMER_NAME, name, "customer name"),
        (sel.CUSTOMER_ADDRESS, address, "address"),
        (sel.CUSTOMER_PHONE, phone, "phone"),
        (sel.CUSTOMER_WHATSAPP, whatsapp, "whatsapp"),
    ):
        err = _type_and_verify(page, field_def, value, cfg, label)
        if err:
            return err
    return None


def _require_all_filled(page, field_def, label: str, expected_count: int) -> Optional[str]:
    """Checks EVERY element matching this field's selector -- not just one
    -- right before a Save click. This is deliberately written to be
    correct regardless of which of two possible DOM shapes the live form
    actually uses for multi-product orders (single row vs one element per
    line), since that isn't something I can verify from here:

      - expected_count=1 (customer/state/payment fields, always singular)
        behaves like a plain single-field check.
      - expected_count=len(matches) for #selpro/#pricevar: if the form
        keeps one element per product line (the "product[]" array-style
        name in the HTML you sent earlier suggests this), every line gets
        checked individually -- so a product filled earlier in the loop
        that got silently wiped by a later re-render is caught by name,
        not just whatever the LAST line happens to show.

    IMPORTANT: if the live form actually reuses a single #selpro/#pricevar
    element for every product line (overwriting it each iteration, which
    was the original assumption in this file), this means a 2+ product
    order will always find only 1 matching element while expecting more --
    and will now correctly FAIL instead of silently submitting with only
    the last product recorded. That's a real behavior change: those orders
    used to "succeed" while quietly dropping earlier lines; now they'll
    fail loudly into manual_check instead. Worth watching your first
    multi-product order after this update to see which DOM shape it
    actually is."""
    locs = page.locator(field_def.selector)
    try:
        count = locs.count()
    except Exception as e:
        return f"could not count {label!r} elements before Save: {e}"

    if count < expected_count:
        return (f"expected {expected_count} {label!r} field(s) for this order's "
                f"{expected_count} product line(s), found {count} in the DOM -- "
                "refusing to submit")

    for i in range(expected_count):
        try:
            value = locs.nth(i).input_value().strip()
        except Exception as e:
            return f"could not read back {label!r} line {i + 1} before Save: {e}"
        if not value:
            return (f"{label!r} line {i + 1} of {expected_count} is empty right "
                     "before Save -- refusing to submit")

    return None


# ==============================================================================
# SAVE / SUCCESS / FAILURE DETECTION  (business logic spec sections 14-15)
# ==============================================================================
def detect_save_outcome(page, cfg: AutomationConfig) -> tuple[str, str]:
    """Returns ("success" | "failure" | "inconclusive", reason). Never
    assumes Save succeeded just because no exception was thrown."""
    start_url = page.url
    try:
        page.wait_for_load_state("networkidle", timeout=cfg.success_wait_timeout_ms)
    except Exception:
        pass

    for err_sel in cfg.error_selectors:
        try:
            loc = page.locator(err_sel)
            if loc.count() > 0 and loc.first.is_visible():
                text = loc.first.inner_text().strip()
                return "failure", f"error indicator matched ({err_sel!r}): {text}"
        except Exception:
            continue

    for ok_sel in cfg.success_selectors:
        try:
            loc = page.locator(ok_sel)
            if loc.count() > 0 and loc.first.is_visible():
                return "success", f"success indicator matched ({ok_sel!r})"
        except Exception:
            continue

    if page.url != start_url:
        return "success", f"URL changed after save: {start_url} -> {page.url}"

    return "inconclusive", "no success/error indicator found and URL unchanged"


# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================
def process_order(page, order: dict, cfg: AutomationConfig,
                   row_number: Optional[int] = None) -> OrderResult:
    """order is expected to have keys: customer_name, phone, whatsapp,
    address, product, price (see run_automation.py for the exact
    column-name mapping from the input spreadsheet). Quantity is NOT read
    from a spreadsheet column -- it's extracted per matched product line
    from the Excel product text itself (MatchResult.quantity), defaulting
    to 1 when there's no leading number."""

    # ---- 1. Parse & match products (spec sections 3, 4, 6, 7) ----
    matches = parse_order_products(order.get("product", ""), cfg.crm_catalog)
    if not matches:
        return OrderResult(False, row_number, reason="no sellable product found in Excel product text")

    rejected = [m for m in matches if m.matched_product is None]
    if rejected:
        reasons = "; ".join(f"{m.excel_text!r}: {m.reason}" for m in rejected)
        # Conservative per spec section 18/19: if any item in a multi-product
        # order can't be confidently matched, reject the whole order rather
        # than silently submitting a partial order.
        return OrderResult(False, row_number, reason=f"product matching failed: {reasons}")

    # ---- 2. State inference (spec section 11) ----
    state = infer_state(order.get("address", ""))
    if state is None:
        return OrderResult(False, row_number,
                            reason=f"could not infer state from address {order.get('address')!r}")

    matched_names = []
    try:
        # ---- 3. Product + price lines (spec sections 8, 9) ----
        for m in matches:
            err = _add_one_product_line(page, m, m.quantity, order.get("price"), cfg)
            if err:
                return OrderResult(False, row_number, reason=err)
            matched_names.append(m.matched_product)

        # ---- 4. Customer info (spec section 10) ----
        err = _fill_customer_info(page, order, cfg)
        if err:
            return OrderResult(False, row_number, reason=err, matched_products=matched_names)

        # ---- 5. State (spec section 11) ----
        err = _select_and_verify(page, sel.SELECT_STATE, state, cfg, "state")
        if err:
            return OrderResult(False, row_number, reason=err, matched_products=matched_names)

        # ---- 5b. Verify EVERYTHING up to this point actually took before
        # clicking Save -- required order is product, price, name, address,
        # phone, whatsapp, state. Product/price are checked per LINE
        # (against how many products this order actually matched), not
        # just a single snapshot -- see _require_all_filled's docstring.
        num_lines = len(matches)
        for field_def, label, expected_count in (
            (sel.SELECT_PRODUCT, "product", num_lines),
            (sel.SELECT_PRICEVAR, "price", num_lines),
            (sel.CUSTOMER_NAME, "customer name", 1),
            (sel.CUSTOMER_ADDRESS, "address", 1),
            (sel.CUSTOMER_PHONE, "phone", 1),
            (sel.CUSTOMER_WHATSAPP, "whatsapp", 1),
            (sel.SELECT_STATE, "state", 1),
        ):
            err = _require_all_filled(page, field_def, label, expected_count)
            if err:
                return OrderResult(False, row_number, reason=err, matched_products=matched_names)

        # ---- 6. First submit (reveals payment section, per recording) ----
        submit_btn = locator(page, sel.SUBMIT)
        submit_btn.wait_for(state="visible", timeout=cfg.network_wait_timeout_ms)
        submit_btn.click()
        _human_pause(*cfg.step_delay_s)

        # ---- 7. Payment method -- always Cash (spec section 12) ----
        err = _select_and_verify(page, sel.SELECT_PAYMENT, sel.PAYMENT_METHOD_VALUE, cfg, "payment method")
        if err:
            return OrderResult(False, row_number, reason=err, matched_products=matched_names)

        # ---- 8. Final submit ----
        submit_btn = locator(page, sel.SUBMIT)
        submit_btn.wait_for(state="visible", timeout=cfg.network_wait_timeout_ms)
        submit_btn.click()

        # ---- 9. Success detection with one retry on inconclusive results ----
        outcome, reason = detect_save_outcome(page, cfg)
        retries_left = cfg.max_retries_on_inconclusive
        while outcome == "inconclusive" and retries_left > 0:
            _human_pause(1.0, 2.5)
            outcome, reason = detect_save_outcome(page, cfg)
            retries_left -= 1

        if outcome == "success":
            _human_pause(*cfg.post_save_delay_s)  # spec section 13: 1-30s
            return OrderResult(True, row_number, reason=reason, matched_products=matched_names)

        return OrderResult(False, row_number, reason=f"[{outcome}] {reason}",
                            matched_products=matched_names)

    except Exception as e:
        return OrderResult(False, row_number, reason=f"unexpected error: {e}",
                            matched_products=matched_names)


def to_failed_order(order: dict, result: OrderResult) -> FailedOrder:
    return FailedOrder(
        customer=_clean_text(order.get("customer_name")),
        phone = _clean_text(order.get("phone_number")),          # was "phone"
        address=_clean_text(order.get("address")),
        product=_clean_text(order.get("product")),
        quantity=_clean_text(order.get("quantity")),
        intended_price=_clean_text(order.get("price")),
        reason=result.reason,
        row_number=result.row_number,
    )