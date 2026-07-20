"""
workflow_recorder.py

BusinessWorkflowRecorder attaches to an already-open Playwright `page`
and produces a concise, human-readable log of business actions only.

It does NOT know anything about sessions/login -- that's session_manager.py.
It does NOT parse raw DOM mutation dumps -- the noise filtering happens
in recorder.js, in the browser, before anything reaches Python. This
file is only responsible for:

  1. Wiring the JS recorder up (expose_function + add_init_script)
  2. Filtering network requests down to business-relevant endpoints
  3. Collapsing duplicate/near-duplicate steps across the whole session
     (e.g. repeated dropdown opens, or a submit immediately after a
     Save click)
  4. Formatting the final step list as human-readable text + structured JSON
  5. (bonus) emitting a replayable Playwright script from the steps
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

from important_fields import (
    NETWORK_INCLUDE_PATTERNS,
    NETWORK_EXCLUDE_EXTENSIONS,
    NETWORK_EXCLUDE_DOMAINS,
)

_RECORDER_JS_PATH = Path(__file__).with_name("recorder.js")

_NETWORK_INCLUDE_RE = re.compile("|".join(NETWORK_INCLUDE_PATTERNS), re.IGNORECASE)

# A step within DEDUPE_WINDOW_SECONDS of the previous one, with the same
# description + action_type, is treated as a repeat of the same business
# action (this is what collapses "3x Clicked dropdown" -> nothing, and
# stray double-fires -> one step).
DEDUPE_WINDOW_SECONDS = 1.5


@dataclass
class Step:
    step_number: int
    action_type: str          # e.g. "Search Customer", "Select Product", "Save Order"
    description: str          # human label of the field/control
    value: Optional[str]
    best_selector: Optional[str]
    fallback_selector: Optional[str]
    wait_condition: Optional[str]
    success_condition: Optional[str] = None
    field_tag: Optional[str] = None  # "select" | "input" | "textarea" | None (for actions)
    timestamp: float = field(default_factory=time.time)

    def to_human_block(self) -> str:
        lines = [f"STEP {self.step_number}", "", self.action_type]
        if self.value:
            lines += ["", "Value:", str(self.value)]
        if self.best_selector:
            lines += ["", "Selector:", self.best_selector]
        if self.fallback_selector and self.fallback_selector != self.best_selector:
            lines += ["Fallback:", self.fallback_selector]
        if self.wait_condition:
            lines += ["", "Wait:", self.wait_condition]
        if self.success_condition:
            lines += ["", "Result:", self.success_condition]
        return "\n".join(lines)


# Maps a raw (kind, action_type, description-keyword) to a friendly action label.
_ACTION_LABELS = {
    ("field", "type", "customer"): "Search Customer",
    ("field", "select", "customer"): "Select Customer",
    ("field", "type", "phone"): "Fill Phone",
    ("field", "select", "phone"): "Fill Phone",
    ("field", "type", "whatsapp"): "Fill WhatsApp",
    ("field", "select", "whatsapp"): "Fill WhatsApp",
    ("field", "type", "address"): "Fill Address",
    ("field", "select", "address"): "Fill Address",
    ("field", "select", "product"): "Choose Product",
    ("field", "select", "variant"): "Choose Variant",
    ("field", "select", "price"): "Choose Variant",
    ("field", "select", "state"): "Choose State",
    ("field", "select", "payment"): "Payment Method",
    ("field", "select", "tag"): "Add Tag",
    ("action", "click", "save"): "Save Order",
    ("action", "click", "submit"): "Submit",
    ("action", "submit", "form"): "Submit Form",
    ("action", "click", "delete"): "Delete",
    ("action", "click", "confirm"): "Confirm",
    ("action", "click", "cancel"): "Cancel",
    ("action", "click", "search"): "Search",
}


def _friendly_label(kind: str, action_type: str, description: str) -> str:
    desc_lower = (description or "").lower()
    for (k, a, keyword), label in _ACTION_LABELS.items():
        if k == kind and a == action_type and keyword in desc_lower:
            return label
    # Fall back to a Title Case version of the raw description.
    if kind == "field":
        return f"Fill {description.strip().title()}" if description else "Fill Field"
    return description.strip().title() if description else action_type.title()


class BusinessWorkflowRecorder:
    def __init__(self, page, output_dir: str = "recordings", session_name: str = "session",
                 debug: bool = False, target_url_prefix: Optional[str] = None):
        self.page = page
        self.output_dir = output_dir
        self.session_name = session_name
        self.debug = debug
        # If set, capturing is strictly limited to pages whose URL starts
        # with this prefix -- e.g. only https://app.snipercrm.io/add_multi_order,
        # never a login page the site might bounce you to mid-session.
        # Enforced independently in both the browser (recorder.js) and
        # here in Python (belt-and-braces: network/navigation events are
        # Python-native and don't pass through the JS importance gate).
        self.target_url_prefix = target_url_prefix
        self.steps: List[Step] = []
        self._last_signature: Optional[tuple] = None
        self._last_signature_time: float = 0.0
        self._pending_success_target: Optional[int] = None  # step_number awaiting an outcome
        self._last_outcome: Optional[str] = None

        os.makedirs(self.output_dir, exist_ok=True)

    def _in_scope(self) -> bool:
        if not self.target_url_prefix:
            return True
        try:
            return self.page.url.startswith(self.target_url_prefix)
        except Exception:
            return False

    # ---------- setup ----------

    def start(self) -> None:
        """Wire up the JS recorder. Call once, right after the page (or
        session) is ready. Because we use add_init_script, this survives
        reloads/navigations automatically."""
        if not _RECORDER_JS_PATH.exists():
            raise FileNotFoundError(
                f"recorder.js not found at {_RECORDER_JS_PATH}. "
                "It must sit in the same folder as workflow_recorder.py "
                "(alongside it, not in a subfolder). Copy recorder.js there "
                "and try again."
            )

        scope_script = ""
        if self.target_url_prefix:
            escaped = self.target_url_prefix.replace("\\", "\\\\").replace("'", "\\'")
            scope_script += f"window.__recorderTargetPrefix = '{escaped}';"
        if self.debug:
            scope_script += "window.__recorderDebug = true;"
        if scope_script:
            self.page.add_init_script(script=scope_script)

        self.page.expose_function("__recordStep", self._on_raw_step)
        self.page.add_init_script(path=str(_RECORDER_JS_PATH))

        # Re-inject into the *current* document too, since add_init_script
        # only affects future navigations, not the already-loaded page.
        # Order matters: scope/debug flags must be set before recorder.js runs.
        if scope_script:
            self.page.evaluate(scope_script)
        self.page.evaluate(_RECORDER_JS_PATH.read_text())

        self.page.on("request", self._on_request)
        self.page.on("framenavigated", self._on_navigated)

        if self.debug:
            print("  [debug mode ON] every click/input/change will be reported, "
                  "important or not.\n", flush=True)
        if self.target_url_prefix:
            print(f"  Recording is scoped to: {self.target_url_prefix}"
                  " (nothing captured on any other page)\n", flush=True)

    # ---------- JS bridge ----------

    def _on_raw_step(self, raw_json: str) -> None:
        try:
            evt = json.loads(raw_json)
        except json.JSONDecodeError:
            return

        kind = evt.get("kind")

        if kind == "debug":
            # Debug events are diagnostic only (values already masked for
            # password fields in recorder.js) -- always shown, regardless
            # of scope, so you can see what happened on the "wrong" page too.
            print(f"  [debug] {evt.get('action_type')}: {evt.get('description')}"
                  + (f" = {evt.get('value')!r}" if evt.get("value") else ""), flush=True)
            return

        if not self._in_scope():
            # Second layer of the scope guard: recorder.js already gates
            # this, but if the page navigated between the JS check and
            # this callback actually running, don't record it here either.
            return

        if kind == "outcome":
            self._attach_outcome(evt)
            return

        action_type = evt.get("action_type", "")
        description = evt.get("description", "") or ""
        label = _friendly_label(kind, action_type, description)

        signature = (kind, action_type, description.strip().lower())
        now = evt.get("timestamp", time.time() * 1000) / 1000.0

        if (
            signature == self._last_signature
            and (now - self._last_signature_time) < DEDUPE_WINDOW_SECONDS
        ):
            # Same control fired twice in a row (e.g. click-to-open then
            # a stray change with identical value) -- treat as one step.
            self._last_signature_time = now
            return

        self._last_signature = signature
        self._last_signature_time = now

        step = Step(
            step_number=len(self.steps) + 1,
            action_type=label,
            description=description,
            value=evt.get("value"),
            best_selector=evt.get("best_selector"),
            fallback_selector=evt.get("fallback_selector"),
            wait_condition=evt.get("wait_condition"),
            field_tag=evt.get("tag"),
        )
        self.steps.append(step)
        self._print_live(step)
        self._autosave()

        if action_type in ("submit",) or "save" in description.lower():
            self._pending_success_target = step.step_number

    def _print_live(self, step: "Step") -> None:
        if step.value:
            print(f"  [{step.step_number}] {step.action_type}: {step.value}", flush=True)
        else:
            print(f"  [{step.step_number}] {step.action_type}", flush=True)

    def _attach_outcome(self, evt: dict) -> None:
        text = evt.get("description", "")
        outcome = f"{'Error' if evt.get('action_type') == 'error' else 'Success'}: {text}"
        if outcome == self._last_outcome:
            return  # duplicate toast (e.g. from a double-submit) -- already recorded
        self._last_outcome = outcome
        print(f"      -> {outcome}", flush=True)
        if self._pending_success_target is not None:
            for s in self.steps:
                if s.step_number == self._pending_success_target:
                    s.success_condition = outcome
                    break
            self._pending_success_target = None
        elif self.steps:
            self.steps[-1].success_condition = outcome
        self._autosave()

    # ---------- network filtering ----------

    def _on_request(self, request) -> None:
        if not self._in_scope():
            return

        url = request.url
        if any(domain in url for domain in NETWORK_EXCLUDE_DOMAINS):
            return
        if url.lower().split("?")[0].endswith(NETWORK_EXCLUDE_EXTENSIONS):
            return
        if not _NETWORK_INCLUDE_RE.search(url):
            return
        if request.method not in ("POST", "PUT", "DELETE"):
            return

        step = Step(
            step_number=len(self.steps) + 1,
            action_type="Network Call",
            description=f"{request.method} {self._short_url(url)}",
            value=None,
            best_selector=None,
            fallback_selector=None,
            wait_condition="response received",
        )
        self.steps.append(step)
        self._print_live(step)
        self._pending_success_target = step.step_number
        self._autosave()

    @staticmethod
    def _short_url(url: str) -> str:
        return url.split("://", 1)[-1].split("?", 1)[0]

    def _on_navigated(self, frame) -> None:
        if frame != self.page.main_frame:
            return
        # Gate by the DESTINATION url, not the current page.url (which
        # hasn't updated yet during this callback) -- this is what keeps
        # "navigated away to the login page" out of the business log
        # entirely, rather than logging the departure and then staying
        # silent, which would be confusing.
        if self.target_url_prefix and not frame.url.startswith(self.target_url_prefix):
            return
        # Only log navigations that actually change the path -- a same-URL
        # reload after Save is meaningful ("Order Saved" already captured
        # via the toast observer, this just confirms the page reset).
        step = Step(
            step_number=len(self.steps) + 1,
            action_type="Navigation",
            description=f"Navigated to {self._short_url(frame.url)}",
            value=None,
            best_selector=None,
            fallback_selector=None,
            wait_condition="domcontentloaded",
        )
        # Don't spam: skip if identical to the previous navigation step.
        if self.steps and self.steps[-1].action_type == "Navigation" \
                and self.steps[-1].description == step.description:
            return
        self.steps.append(step)
        self._print_live(step)
        self._autosave()

    # ---------- output ----------

    def _autosave(self) -> None:
        """Write current progress to disk immediately after every captured
        step. This is what guarantees the file is never behind reality --
        if Ctrl+C/process-exit timing ever races ahead of message delivery
        again, whatever was captured up to the last successfully-processed
        event is already safely on disk, not sitting only in memory."""
        try:
            self._write_files(list(self.steps))
        except OSError:
            pass  # never let a save hiccup crash the recording itself

    def _write_files(self, steps: List["Step"]) -> dict:
        numbered = list(steps)
        for i, step in enumerate(numbered, start=1):
            step.step_number = i

        base = os.path.join(self.output_dir, self.session_name)
        text_path = f"{base}.txt"
        json_path = f"{base}.json"

        with open(text_path, "w", encoding="utf-8") as f:
            f.write(self._render_human_log(numbered))

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in numbered], f, indent=2)

        return {"text_path": text_path, "json_path": json_path}

    def stop_and_save(self) -> dict:
        """Final save on exit. Functionally the same as the autosave that
        already ran after the last step, kept separate mainly so the
        caller has an explicit, guaranteed-final write to report paths from."""
        return self._write_files(self.steps)

    def _render_human_log(self, steps: Optional[List["Step"]] = None) -> str:
        blocks = [step.to_human_block() for step in (steps if steps is not None else self.steps)]
        separator = "\n\n" + "-" * 32 + "\n\n"
        return separator.join(blocks) + "\n"

    def generate_playwright_script(self) -> str:
        """Bonus: turn the recorded steps into a runnable Playwright
        script, the way Selenium IDE / Testim / Cypress Studio do."""
        lines = [
            "from playwright.sync_api import sync_playwright",
            "",
            "with sync_playwright() as p:",
            "    browser = p.chromium.launch(headless=False)",
            "    page = browser.new_page()",
            "",
        ]
        for step in self.steps:
            if not step.best_selector:
                continue
            if step.action_type in ("Save Order", "Submit", "Submit Form", "Confirm",
                                     "Delete", "Cancel", "Search"):
                lines.append(f"    {step.best_selector}.click()  # {step.action_type}")
            elif step.value is not None:
                safe_value = str(step.value).replace('"', '\\"')
                if step.field_tag == "select":
                    lines.append(
                        f'    {step.best_selector}.select_option(label="{safe_value}")  # {step.action_type}'
                    )
                else:
                    lines.append(f'    {step.best_selector}.fill("{safe_value}")  # {step.action_type}')
        lines.append("")
        lines.append("    browser.close()")
        return "\n".join(lines)