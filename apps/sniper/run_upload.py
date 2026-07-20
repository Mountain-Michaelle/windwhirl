"""
run_automation.py

Entry point for the Product Order Automation. Mirrors run_recorder.py's
structure on purpose: restore session -> for each row, run add_products
-> log results. Does NOT touch session_manager.py, nav_utils.py, or any
authentication/navigation logic -- it only plugs into them.

Session persistence: SessionManager (session_manager.py) handles this via
storage_state save/restore, unchanged. On a valid saved session it goes
straight to TARGET_URL. On no/invalid session, manual_login() waits until
the URL leaves the login page -- which lands on /cshome right after a
successful login -- then this file's own safe_goto(page, TARGET_URL)
call right after carries it from /cshome to the actual work page.

Input file: auto-detected as the single .csv/.xlsx/.xls file in
customers/data/ (relative to this script). See orders_template.csv for
the expected columns.

Run:
    pip install -r requirements.txt
    playwright install chromium
    python run_automation.py
"""

from __future__ import annotations

import dataclasses
import sys
import time

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
# ==============================================================

_SCRIPT_DIR = Path(__file__).resolve().parent
CUSTOMERS_DIR = _SCRIPT_DIR / "customers"
CUSTOMERS_DATA_DIR = CUSTOMERS_DIR / "data"
MANUAL_CHECK_DIR = CUSTOMERS_DIR / "manual_check"
MANUAL_CHECK_PATH = MANUAL_CHECK_DIR / "failed_exported.csv"

ORDER_FILE_EXTENSIONS = (".csv", ".xlsx", ".xls")


import pandas as pd
from playwright.sync_api import sync_playwright

from session_manager import SessionManager
from nav_utils import safe_goto
from apps.sniper.product_upload.add_products import AutomationConfig, process_order, to_failed_order
from apps.sniper.product_upload.failure_logger import FailureLogger, FailedOrder
import apps.sniper.utils.field_selectors as sel

LOGIN_URL = "https://app.snipercrm.io/index.php"
TARGET_URL = "https://app.snipercrm.io/add_multi_order"

LOGGED_IN_SELECTOR = "#selpro"
LOGIN_INDICATOR_SELECTOR = "#login-form"

COLUMN_MAP = {
    "Customer Name": "customer_name",
    "Phone Number": "phone_number",
    "WhatsApp Number": "whatsapp_number",
    "Address": "address",
    "Product": "product",
    "Price (₦)": "price",
}


def find_orders_file() -> Path:
    """Auto-detects the single order file in customers/data/. Errors out
    (rather than guessing) if there are zero or more than one match, per
    business requirement: this directory is expected to hold exactly one
    order file at a time."""
    candidates = sorted(
        p for p in CUSTOMERS_DATA_DIR.glob("*")
        if p.is_file() and p.suffix.lower() in ORDER_FILE_EXTENSIONS
    )

    if not candidates:
        raise FileNotFoundError(
            f"No .csv/.xlsx/.xls file found in {CUSTOMERS_DATA_DIR}. "
            f"Place exactly one order file there and re-run."
        )
    if len(candidates) > 1:
        listed = "\n  ".join(str(p) for p in candidates)
        raise RuntimeError(
            f"Expected exactly one order file in {CUSTOMERS_DATA_DIR}, found {len(candidates)}:\n"
            f"  {listed}\n"
            f"Remove the extras and re-run."
        )
    return candidates[0]


def _is_blank(value) -> bool:
    """True for NaN, None, or a string that's empty after stripping
    whitespace -- catches the trailing blank rows Excel exports often
    leave behind."""
    if value is None:
        return True
    if isinstance(value, float) and pd.isna(value):
        return True
    return str(value).strip() == ""


def load_orders(path: str) -> list[dict]:
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    missing = [c for c in COLUMN_MAP if c not in df.columns]
    if missing:
        raise ValueError(
            f"Input file is missing expected column(s): {missing}. "
            f"Found columns: {list(df.columns)}"
        )

    orders = []
    skipped_blank = 0
    for _, row in df.iterrows():
        order = {internal: row[excel_col] for excel_col, internal in COLUMN_MAP.items()}
        if all(_is_blank(v) for v in order.values()):
            skipped_blank += 1
            continue
        orders.append(order)

    if skipped_blank:
        print(f"Skipped {skipped_blank} blank row(s) in the input file.")

    return orders


def scrape_crm_catalog(page) -> list[str]:
    labels = page.eval_on_selector_all(
        f"{sel.SELECT_PRODUCT.selector} option",
        "els => els.map(e => e.textContent.trim())",
    )
    return [l for l in labels if l and l.lower() not in ("select", "select product", "")]


def manual_login(page) -> None:
    safe_goto(page, LOGIN_URL, wait_until="commit")
    print("Please log in manually in the opened browser window.")
    print("Waiting for the app to reach a logged-in page...")
    page.wait_for_url(lambda url: "login" not in url, timeout=0)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass


def export_manual_check(failed_rows: list[FailedOrder]) -> Path:
    """Writes failed orders to customers/manual_check/failed_exported.csv
    -- always CSV, regardless of the input file's own format. Separate
    from -- and in addition to -- FailureLogger's recordings/ txt+jsonl
    output."""
    MANUAL_CHECK_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([dataclasses.asdict(row) for row in failed_rows])
    df.to_csv(MANUAL_CHECK_PATH, index=False)
    return MANUAL_CHECK_PATH


def main() -> None:
    orders_path = find_orders_file()
    orders = load_orders(str(orders_path))
    print(f"Loaded {len(orders)} order(s) from {orders_path}")

    session = SessionManager(
        storage_path="session_state.json",
        login_url=LOGIN_URL,
        target_url=TARGET_URL,
        logged_in_selector=LOGGED_IN_SELECTOR,
        login_indicator_selector=LOGIN_INDICATOR_SELECTOR,
        max_age_seconds=12 * 3600,
    )

    session_name = f"add_products_{int(time.time())}"
    failure_log = FailureLogger(output_dir="recordings", session_name=session_name)

    succeeded = 0
    failed = 0
    failed_rows: list[FailedOrder] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        context, page = session.load_session(browser)
        if page is None:
            print("No valid session found -- starting normal login flow.")
            context = browser.new_context()
            page = context.new_page()
            manual_login(page)
            session.save_session(context, page)
            safe_goto(page, TARGET_URL, wait_until="commit")
        else:
            print("Restored saved session -- skipped login, opened straight on target page.")

        page.wait_for_selector(sel.SELECT_PRODUCT.selector, timeout=20000)
        catalog = scrape_crm_catalog(page)
        if not catalog:
            print("ERROR: could not read any products from #selpro -- aborting. "
                  "Check that the page finished loading and the selector is still correct.")
            browser.close()
            sys.exit(1)
        print(f"Loaded {len(catalog)} sellable product(s) from the live CRM catalog.\n")

        cfg = AutomationConfig(crm_catalog=catalog, speed_multiplier=0.5) #2x Faster than default, but still human-like

        for i, order in enumerate(orders, start=1):
            print(f"[{i}/{len(orders)}] Processing {order.get('customer_name')!r}...", flush=True)

            result = process_order(page, order, cfg, row_number=i)

            if result.success:
                succeeded += 1
                print(f"    OK   -- matched: {result.matched_products} | {result.reason}", flush=True)
            else:
                failed += 1
                print(f"    FAIL -- {result.reason}", flush=True)
                failed_order = to_failed_order(order, result)
                failure_log.log(failed_order)
                failed_rows.append(failed_order)

            safe_goto(page, TARGET_URL, wait_until="commit")
            page.wait_for_selector(sel.SELECT_PRODUCT.selector, timeout=20000)

        print(f"\nDone. {succeeded} succeeded, {failed} failed.")
        if failed:
            print(f"Failed orders exported to: {failure_log.txt_path}")
            print(f"                      and: {failure_log.jsonl_path}")

            manual_check_path = export_manual_check(failed_rows)
            print(f"                      and: {manual_check_path}")

        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()