"""
run_recorder.py

Entry point. Wires SessionManager + BusinessWorkflowRecorder together.
Neither module knows about the other -- this is the only place that does.

Flow:
    1. Ask SessionManager for a saved, still-valid session.
    2a. If valid: browser opens straight on add_multi_order, login skipped.
    2b. If not: run the normal login flow, then save the new session.
    3. Start the recorder and let the user work the form normally.
    4. On exit (Ctrl+C or the "done" prompt), save the human-readable
       + JSON logs.

Run:
    pip install playwright
    playwright install chromium
    python run_recorder.py
"""

from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

from session_manager import SessionManager
from workflow_recorder import BusinessWorkflowRecorder
from nav_utils import safe_goto

LOGIN_URL = "https://app.snipercrm.io/index.php"
TARGET_URL = "https://app.snipercrm.io/add_multi_order"

# CSS selectors used only to *verify* login state -- adjust these two
# to match real elements on the SniperCRM pages if they differ.
LOGGED_IN_SELECTOR = "#selpro"           # present on add_multi_order once logged in
LOGIN_INDICATOR_SELECTOR = "#login-form" # present on the login page

def manual_login(page) -> None:
    """Placeholder for the real login flow. Left interactive/manual by
    default so credentials are never hard-coded into this script; swap
    in page.fill(...)/page.click(...) calls here if you want it automated."""
    safe_goto(page, LOGIN_URL, wait_until="commit")
    print("Please log in manually in the opened browser window.")
    print("Waiting for the app to reach a logged-in page...")
    page.wait_for_url(lambda url: "login" not in url, timeout=0)
    # Login often triggers a short chain of redirects (login -> dashboard
    # -> SPA route settle). Give it a moment before we navigate away
    # ourselves, or our own goto can lose the race and abort.
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass  # some apps never go fully idle (polling, websockets) -- not fatal


def main() -> None:
    session = SessionManager(
        storage_path="session_state.json",
        login_url=LOGIN_URL,
        target_url=TARGET_URL,
        logged_in_selector=LOGGED_IN_SELECTOR,
        login_indicator_selector=LOGIN_INDICATOR_SELECTOR,
        max_age_seconds=12 * 3600,  # re-validate/expire after 12h, tune as needed
    )

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

        recorder = BusinessWorkflowRecorder(
            page,
            output_dir="recordings",
            session_name=f"add_multi_order_{int(time.time())}",
            debug=("--debug" in sys.argv),
            target_url_prefix=TARGET_URL,
        )
        recorder.start()

        # Watches for the site itself bouncing us to the login page mid-
        # recording (an expired session). Just flags it -- the actual
        # recovery (waiting for re-login, then navigating back) happens
        # on the main thread in the loop below, not inside this callback.
        session_dropped = {"flag": False}

        def _watch_for_midsession_logout(frame) -> None:
            if frame != page.main_frame:
                return
            if "login" in frame.url.lower() and "login" not in TARGET_URL.lower():
                session_dropped["flag"] = True

        page.on("framenavigated", _watch_for_midsession_logout)

        print("Recording started. Work the form normally in the browser.")
        print("Each captured field/action will print here as it happens.")
        print("Press Ctrl+C in this terminal when you're done to save the log.\n")

        elapsed = 0
        warned_empty = False
        try:
            while True:
                time.sleep(1)
                elapsed += 1

                if session_dropped["flag"]:
                    print(
                        "\n  Session expired mid-recording -- you were redirected "
                        "to the login page. Recording is paused (nothing on the "
                        "login page is ever captured). Please log in again in "
                        "the browser window; you'll be returned to "
                        f"{TARGET_URL} automatically and recording will resume.\n",
                        flush=True,
                    )
                    while "login" in page.url.lower():
                        time.sleep(1)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    session.save_session(context, page)  # cookies rotated on re-login
                    safe_goto(page, TARGET_URL, wait_until="commit")
                    session_dropped["flag"] = False
                    elapsed = 0
                    warned_empty = False
                    print(f"  Logged in again -- back on {TARGET_URL}, recording resumed.\n",
                          flush=True)
                    continue

                if elapsed == 20 and not recorder.steps and not warned_empty:
                    warned_empty = True
                    print(
                        "\n  ...20s in, 0 steps captured yet. If you've already "
                        "started filling the form, this usually means the page "
                        "hasn't loaded the recorder script (try refreshing the "
                        "page once), or the fields aren't being classified as "
                        "important. Stop this (Ctrl+C) and re-run with:\n"
                        "      python run_recorder.py --debug\n"
                        "  to see every click/input/change reported live, "
                        "important or not -- that will show exactly what's "
                        "happening. Still listening for now.\n"
                    )
        except KeyboardInterrupt:
            pass

        # Force any events already sent from the browser but not yet
        # delivered to our Python callback to actually arrive before we
        # report final results. A round-trip call (page.evaluate) can only
        # return after every earlier message on the same connection --
        # including any pending __recordStep calls -- has been processed.
        print("\nFinishing up (letting any in-flight events land)...", flush=True)
        try:
            for _ in range(5):
                page.evaluate("1")
                time.sleep(0.3)
        except Exception:
            pass  # page/browser may already be gone -- fine, autosave already covers us

        paths = recorder.stop_and_save()
        print(f"\nSaved human-readable log: {paths['text_path']}", flush=True)
        print(f"Saved structured log:     {paths['json_path']}", flush=True)

        if recorder.steps:
            print(f"\n{len(recorder.steps)} step(s) recorded:\n", flush=True)
            print(open(paths["text_path"], encoding="utf-8").read(), flush=True)
        else:
            print(
                "\nWARNING: 0 steps were recorded. This usually means no "
                "important-field interactions were detected -- e.g. the "
                "browser window was closed before any typing/selecting "
                "happened, or the form's field names/ids differ from what "
                "important_fields.py expects. The .txt/.json files above "
                "were still written, just empty.",
                flush=True,
            )

        # The browser window may already be gone by this point (closed
        # manually, or it exited on its own after Ctrl+C) -- that's not
        # an error condition worth crashing over, since the log is
        # already safely written to disk above.
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