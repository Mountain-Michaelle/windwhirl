/**
 * recorder.js
 *
 * Injected into the page via page.add_init_script(), so it re-attaches
 * automatically after every navigation/reload -- this is what lets the
 * recorder survive "Navigated away or reloaded" without the caller
 * having to re-wire anything.
 *
 * Design goal: do the noise-filtering IN THE BROWSER, at the moment an
 * event happens, rather than recording everything and cleaning it up
 * afterwards. That's why there are no MutationObservers on style/attr
 * changes here at all -- we never look at transforms, classes, or CSS.
 * We only ever look at: clicks on meaningful controls, final values of
 * inputs/selects (debounced), form submits, and a small allow-listed
 * set of "success/error toast" nodes.
 */
(() => {
  // Avoid double-installing if add_init_script fires more than once
  // for the same document.
  if (window.__bizRecorderInstalled) return;
  window.__bizRecorderInstalled = true;

  // Set via page.evaluate("window.__recorderDebug = true") from Python
  // (BusinessWorkflowRecorder(..., debug=True)) or by running
  // `python run_recorder.py --debug`. When on, every click/input/change
  // is reported -- including ones judged NOT important -- so you can see
  // exactly what the recorder saw and why it did or didn't capture it.
  const DEBUG = !!window.__recorderDebug;

  const IMPORTANT_KEYWORDS = [
    "customer", "recipient", "phone", "whatsapp", "email", "address",
    "state", "product", "variant", "quantity", "price", "discount",
    "delivery", "payment", "save", "submit", "delete", "confirm",
    "cancel", "search", "tag", "qty",
  ];

  const SUCCESS_SELECTORS = [
    ".swal2-success", ".swal2-icon-success",
    ".swal2-error", ".swal2-icon-error",
    ".toast-success", ".toast-error", ".toast-message",
    ".alert-success", ".alert-danger",
    "[role='alert']",
  ];

  const CLICK_DEDUPE_WINDOW_MS = 1200;   // collapse rapid repeat clicks
  const TYPE_DEBOUNCE_MS = 500;          // wait for typing to settle

  const lastClickSignature = { key: null, time: 0 };
  const pendingTypeTimers = new WeakMap();
  const dropdownOpenedNotYetSelected = new WeakSet();
  // Tracks the last value actually emitted per element, regardless of
  // *how* it was captured (debounced typing vs. change-on-blur). This is
  // what prevents "type" + "change" on the same plain input from ever
  // producing two steps, independent of timing.
  const lastEmittedValue = new WeakMap();

  const _sendQueue = [];
  let _flushTimer = null;

  function safeSend(event) {
    if (window.__recordStep) {
      try {
        window.__recordStep(JSON.stringify(event));
      } catch (e) {
        // Never let recorder errors break the page under automation.
      }
      return;
    }
    // __recordStep isn't wired up yet (rare timing race right at page
    // load) -- queue it and retry shortly instead of silently dropping it.
    _sendQueue.push(event);
    if (!_flushTimer) {
      _flushTimer = setInterval(() => {
        if (!window.__recordStep) return;
        clearInterval(_flushTimer);
        _flushTimer = null;
        while (_sendQueue.length) {
          try {
            window.__recordStep(JSON.stringify(_sendQueue.shift()));
          } catch (e) {
            /* ignore */
          }
        }
      }, 200);
    }
  }

  // ---------- Business-relevance classification ----------

  // Returns a cell's text only if it's a plain label cell -- i.e. it does
  // NOT itself contain another form control. Without this guard, a select's
  // rendered <option> text (e.g. every product name) leaks into what's
  // supposed to be a short label for the *neighboring* field.
  function cellLabelText(cell) {
    if (!cell) return "";
    if (cell.querySelector("input, select, textarea, button")) return "";
    return cell.textContent.trim();
  }

  function nearestRowLabel(el) {
    // Common pattern in this app: <tr><td>Label text</td><td><input></td></tr>
    const cell = el.closest("td");
    if (!cell) return "";
    let row = cell.parentElement;
    if (row && row.cells && row.cells.length) {
      // First cell in the row is usually the label.
      const first = row.cells[0];
      if (first && first !== cell) {
        const text = cellLabelText(first);
        if (text) return text;
      }
    }
    // Fallback: text immediately preceding the cell (a <label> or plain text node).
    const prevCell = cell.previousElementSibling;
    if (prevCell) {
      const text = cellLabelText(prevCell);
      if (text) return text;
    }
    return "";
  }

  function associatedLabelText(el) {
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl.textContent.trim();
    }
    const wrappingLabel = el.closest("label");
    if (wrappingLabel) return wrappingLabel.textContent.trim();
    return "";
  }

  // A <select>'s first, empty-value <option> is very often the real
  // label on sites like this one -- e.g. <option value="">Select Product</option>.
  function firstOptionText(el) {
    if (!el.options || !el.options.length) return "";
    const first = el.options[0];
    if (first && (!first.value || first.value === "")) {
      return first.textContent.trim().replace(/^Select\s*/i, "");
    }
    return "";
  }

  // Table column header aligned with this element's <td>, for tables
  // that label columns via a header row instead of a per-row label cell
  // (e.g. "Product | Qty/Price" headers above rows that start directly
  // with the input/select itself).
  function columnHeaderText(el) {
    const cell = el.closest("td");
    if (!cell) return "";
    const table = cell.closest("table");
    if (!table) return "";
    const row = cell.parentElement;
    const colIndex = Array.prototype.indexOf.call(row.children, cell);
    const headerRow = table.querySelector("thead tr") || table.querySelector("tr");
    if (!headerRow || headerRow === row) return "";
    const headerCells = headerRow.querySelectorAll("th, td");
    const headerCell = headerCells[colIndex];
    return headerCell ? cellLabelText(headerCell) : "";
  }

  function descriptiveText(el) {
    return [
      el.getAttribute("aria-label") || "",
      el.getAttribute("placeholder") || "",
      el.name || "",
      el.id || "",
      associatedLabelText(el),
      nearestRowLabel(el),
      firstOptionText(el),
      columnHeaderText(el),
      el.tagName === "BUTTON" || el.type === "submit" ? el.textContent.trim() : "",
    ].join(" ");
  }

  // Best-effort human label, in priority order. Falls back to a stable
  // positional description ("Field (row 2)") rather than ever returning
  // nothing -- an unlabeled captured value is still far more useful than
  // a silently dropped one.
  let unlabeledCounter = 0;
  const positionalLabel = new WeakMap();

  function elementLabel(el) {
    const found =
      el.getAttribute("aria-label") ||
      associatedLabelText(el) ||
      nearestRowLabel(el) ||
      columnHeaderText(el) ||
      firstOptionText(el) ||
      el.getAttribute("placeholder") ||
      el.name ||
      el.id;
    if (found) return found;

    if (!positionalLabel.has(el)) {
      unlabeledCounter += 1;
      const row = el.closest("tr");
      const rowInfo = row && row.parentElement
        ? ` (row ${Array.prototype.indexOf.call(row.parentElement.children, row) + 1})`
        : "";
      positionalLabel.set(el, `Field ${unlabeledCounter}${rowInfo}`);
    }
    return positionalLabel.get(el);
  }

  // Only capture on the page this recorder was configured for. Set via
  // page.evaluate("window.__recorderTargetPrefix = '...'") from Python
  // before recorder.js loads. Without this, if the site ever bounces the
  // browser to a login page mid-session (expired auth), the recorder --
  // which otherwise treats "any field inside a <form>" as fair game --
  // would happily capture the login form too, including the password.
  function inScope() {
    if (!window.__recorderTargetPrefix) return true; // no prefix configured -- don't restrict
    return location.href.indexOf(window.__recorderTargetPrefix) === 0;
  }

  // Capture scope: every input/select/textarea that lives inside the
  // actual business <form> counts as important -- that form IS the
  // order, there's no meaningful field inside it that's "noise". This
  // deliberately does NOT depend on keyword-matching id/name/label text,
  // because this app's real inputs often have none of those (bare
  // <input class="form-control">) and a keyword gate would silently
  // drop them, which is worse than capturing one extra field.
  // Buttons/links stay keyword-gated since a form can contain UI chrome
  // (tooltips, "Add More" rows, etc.) that isn't a business action.
  function isImportant(el) {
    if (!el || !el.tagName) return false;

    // Hard, unconditional rule -- never capture a password field's value,
    // on any page, regardless of scope or form membership. This is a
    // second, independent layer of protection on top of inScope(): even
    // if the scope check has a bug or a race, credentials never leak.
    if (el.type === "password") return false;

    if (!inScope()) return false;

    const tag = el.tagName.toLowerCase();

    if (["input", "select", "textarea"].includes(tag)) {
      if (el.type === "hidden" || el.disabled) return false;
      return !!el.closest("form");
    }

    if (tag === "button" || el.type === "submit") {
      const text = descriptiveText(el).toLowerCase();
      return el.type === "submit" || IMPORTANT_KEYWORDS.some((k) => text.includes(k));
    }

    if (tag === "a") {
      const text = descriptiveText(el).toLowerCase();
      return IMPORTANT_KEYWORDS.some((k) => text.includes(k));
    }

    return false;
  }

  // ---------- Selector ranking ----------
  // Priority: id > name > data-testid > aria-label > label-for >
  // placeholder > stable css (unique class combo) > xpath fallback.

  function cssEscapeAttr(v) {
    return v.replace(/"/g, '\\"');
  }

  function isUnique(selector) {
    try {
      return document.querySelectorAll(selector).length === 1;
    } catch (e) {
      return false;
    }
  }

  function buildXPath(el) {
    if (el.id) return `//*[@id="${el.id}"]`;
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.body) {
      let index = 1;
      let sibling = node.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === node.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(`${node.tagName.toLowerCase()}[${index}]`);
      node = node.parentElement;
    }
    return "//body/" + parts.join("/");
  }

  function bestSelector(el) {
    const candidates = [];

    if (el.id) {
      const sel = `#${CSS.escape(el.id)}`;
      candidates.push({ type: "id", selector: `page.locator("${sel}")`, ok: isUnique(sel) });
    }
    if (el.name) {
      const sel = `[name="${cssEscapeAttr(el.name)}"]`;
      candidates.push({ type: "name", selector: `page.locator('${sel}')`, ok: isUnique(sel) });
    }
    const testId = el.getAttribute("data-testid");
    if (testId) {
      candidates.push({
        type: "data-testid",
        selector: `page.get_by_test_id("${testId}")`,
        ok: true,
      });
    }
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel) {
      candidates.push({
        type: "aria-label",
        selector: `page.get_by_label("${ariaLabel.replace(/"/g, '\\"')}")`,
        ok: true,
      });
    }
    const labelText = associatedLabelText(el);
    if (labelText) {
      candidates.push({
        type: "label",
        selector: `page.get_by_label("${labelText.replace(/"/g, '\\"')}")`,
        ok: true,
      });
    }
    const placeholder = el.getAttribute("placeholder");
    if (placeholder) {
      candidates.push({
        type: "placeholder",
        selector: `page.get_by_placeholder("${placeholder.replace(/"/g, '\\"')}")`,
        ok: true,
      });
    }
    if (el.tagName.toLowerCase() === "button" || el.type === "submit") {
      const text = el.textContent.trim();
      if (text) {
        candidates.push({
          type: "text",
          selector: `page.get_by_text("${text.replace(/"/g, '\\"')}", exact=True)`,
          ok: true,
        });
      }
    }
    // Stable-looking single class (skip framework noise like "form-control"
    // when it's shared by many elements -- isUnique() naturally filters that out).
    if (el.classList && el.classList.length) {
      for (const cls of el.classList) {
        const sel = `.${CSS.escape(cls)}`;
        if (isUnique(sel)) {
          candidates.push({ type: "css-class", selector: `page.locator("${sel}")`, ok: true });
          break;
        }
      }
    }

    const best = candidates.find((c) => c.ok) || null;
    const xpath = `page.locator('xpath=${buildXPath(el)}')`;

    return {
      best: best ? best.selector : xpath,
      best_type: best ? best.type : "xpath",
      fallback: xpath,
    };
  }

  // ---------- Emitters ----------

  function emitFieldEvent(el, actionType, value) {
    // Skip if this exact value was already reported for this element,
    // no matter which listener (input-debounce vs change-on-blur) is
    // doing the reporting. This is the real fix for duplicate
    // type+change steps on plain text inputs.
    if (lastEmittedValue.get(el) === value) return;
    lastEmittedValue.set(el, value);

    const sel = bestSelector(el);
    safeSend({
      kind: "field",
      action_type: actionType,
      description: elementLabel(el),
      value: value,
      tag: el.tagName.toLowerCase(),
      best_selector: sel.best,
      best_selector_type: sel.best_type,
      fallback_selector: sel.fallback,
      wait_condition: "element visible & enabled",
      timestamp: Date.now(),
    });
  }

  function emitActionEvent(el, actionType) {
    const sel = bestSelector(el);
    safeSend({
      kind: "action",
      action_type: actionType,
      description: elementLabel(el) || el.textContent.trim(),
      value: null,
      best_selector: sel.best,
      best_selector_type: sel.best_type,
      fallback_selector: sel.fallback,
      wait_condition: "element clickable",
      timestamp: Date.now(),
    });
  }

  function clickSignature(el) {
    return el.id || el.name || elementLabel(el) || el.outerHTML.slice(0, 80);
  }

  // ---------- Debug reporting ----------
  // Only active when DEBUG is true. Reports the RAW element an event
  // fired on, before any importance filtering -- this is what lets you
  // see, from the Python terminal, exactly what the recorder saw and
  // whether it judged it important, for every single interaction.
  function debugDescribe(el) {
    if (!el || !el.tagName) return "(no element)";
    const tag = el.tagName.toLowerCase();
    const bits = [tag];
    if (el.id) bits.push(`id="${el.id}"`);
    if (el.name) bits.push(`name="${el.name}"`);
    if (el.className && typeof el.className === "string") {
      bits.push(`class="${el.className.slice(0, 60)}"`);
    }
    bits.push(`inForm=${!!el.closest("form")}`);
    bits.push(`inScope=${inScope()}`);
    bits.push(`important=${isImportant(el)}`);
    return bits.join(" ");
  }

  function sendDebug(eventName, el) {
    if (!DEBUG) return;
    // Never send a password field's actual value, even in debug mode --
    // debug reporting intentionally runs BEFORE isImportant()/inScope()
    // filtering so you can see what got filtered and why, which means it
    // must have its own independent guard against leaking credentials.
    let value = null;
    if (el && el.type === "password") {
      value = "[hidden]";
    } else if (el && "value" in el) {
      value = String(el.value).slice(0, 60);
    }
    safeSend({
      kind: "debug",
      action_type: eventName,
      description: debugDescribe(el),
      value,
      timestamp: Date.now(),
    });
  }

  // ---------- Event listeners (capture phase, so we see everything once) ----------

  document.addEventListener(
    "click",
    (e) => {
      const el = e.target.closest("input, select, textarea, button, a");
      sendDebug("raw-click", el || e.target);
      if (!el || !isImportant(el)) return;

      const tag = el.tagName.toLowerCase();

      // Dropdown-open clicks: don't emit yet, wait for the actual
      // "change" event so 3 clicks + 1 selection collapse into one step.
      if (tag === "select") {
        dropdownOpenedNotYetSelected.add(el);
        return;
      }

      // Buttons / action links: dedupe rapid repeats on the same element.
      const sig = clickSignature(el);
      const now = Date.now();
      if (sig === lastClickSignature.key && now - lastClickSignature.time < CLICK_DEDUPE_WINDOW_MS) {
        return;
      }
      lastClickSignature.key = sig;
      lastClickSignature.time = now;

      if (tag === "button" || el.type === "submit" || tag === "a") {
        emitActionEvent(el, "click");
      }
    },
    true
  );

  document.addEventListener(
    "change",
    (e) => {
      const el = e.target;
      sendDebug("raw-change", el);
      if (!el || !isImportant(el)) return;
      const tag = el.tagName.toLowerCase();

      if (tag === "select") {
        dropdownOpenedNotYetSelected.delete(el);
        const selectedOption = el.options[el.selectedIndex];
        const value = selectedOption ? selectedOption.textContent.trim() : el.value;
        emitFieldEvent(el, "select", value);
        return;
      }

      if (tag === "input" || tag === "textarea") {
        // Blur fires "change" synchronously; cancel any still-pending
        // debounce timer so we never emit the same value twice.
        if (pendingTypeTimers.has(el)) {
          clearTimeout(pendingTypeTimers.get(el));
          pendingTypeTimers.delete(el);
        }
        emitFieldEvent(el, "type", el.value);
      }
    },
    true
  );

  document.addEventListener(
    "input",
    (e) => {
      const el = e.target;
      sendDebug("raw-input", el);
      if (!el || !isImportant(el)) return;
      const tag = el.tagName.toLowerCase();
      if (tag !== "input" && tag !== "textarea") return;

      // Debounce: only emit once typing has settled, with the final value.
      if (pendingTypeTimers.has(el)) {
        clearTimeout(pendingTypeTimers.get(el));
      }
      const timer = setTimeout(() => {
        pendingTypeTimers.delete(el);
        if (el.value) emitFieldEvent(el, "type", el.value);
      }, TYPE_DEBOUNCE_MS);
      pendingTypeTimers.set(el, timer);
    },
    true
  );

  document.addEventListener(
    "submit",
    (e) => {
      const form = e.target;
      if (!form || !inScope()) return;
      // Only emit if we didn't just emit a Save/Submit button click
      // (avoids the duplicate "Submitted form" + "Clicked Save" pair
      // seen in raw recordings).
      const now = Date.now();
      if (now - lastClickSignature.time < CLICK_DEDUPE_WINDOW_MS) return;
      safeSend({
        kind: "action",
        action_type: "submit",
        description: "Submit form",
        value: null,
        best_selector: 'page.locator("form")',
        best_selector_type: "css",
        fallback_selector: 'page.locator("form")',
        wait_condition: "form valid",
        timestamp: now,
      });
    },
    true
  );

  // ---------- Polling safety net ----------
  // Some third-party dropdown/autocomplete widgets set the underlying
  // <select>/<input>'s value via JS without dispatching a native
  // input/change event that our listeners above would see (this is
  // common with jQuery-plugin-based comboboxes). This poller is the
  // guarantee that a value change is captured either way: it reads
  // .value directly off the DOM, independent of whatever event dance
  // the widget does or doesn't do internally.
  //
  // It requires a value to be stable across two consecutive polls
  // before emitting (same debounce idea as typing), so it won't spam
  // a step per keystroke, and it shares lastEmittedValue with the
  // event-based path above so the two can never double-emit the same
  // value for the same element.
  const POLL_INTERVAL_MS = 400;
  const prevPollValue = new WeakMap();

  function currentFieldValue(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "select") {
      const opt = el.options[el.selectedIndex];
      return opt ? opt.textContent.trim() : el.value;
    }
    return el.value;
  }

  function pollImportantFields() {
    const form = document.querySelector("form");
    if (!form) return;
    const fields = form.querySelectorAll("input, select, textarea");
    fields.forEach((el) => {
      if (!isImportant(el)) return;
      const value = currentFieldValue(el);

      if (!prevPollValue.has(el)) {
        // First time we've seen this element: its current value (e.g. a
        // default "Select Product" placeholder option, or an empty text
        // field) is a starting state, not something the user chose.
        // Record it as the baseline without emitting a step.
        prevPollValue.set(el, value);
        if (!lastEmittedValue.has(el)) lastEmittedValue.set(el, value);
        return;
      }

      if (!value) {
        prevPollValue.set(el, value);
        return;
      }

      const prev = prevPollValue.get(el);
      prevPollValue.set(el, value);

      if (prev === value && lastEmittedValue.get(el) !== value) {
        const tag = el.tagName.toLowerCase();
        emitFieldEvent(el, tag === "select" ? "select" : "type", value);
      }
    });
  }

  setInterval(pollImportantFields, POLL_INTERVAL_MS);

  // ---------- Success / error detection ----------
  // Small, targeted observer -- only watches for a short allow-list of
  // toast/alert node classes being added. Nothing about attributes,
  // styles, or transforms is ever inspected.

  const successObserver = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType !== 1) continue;
        for (const sel of SUCCESS_SELECTORS) {
          if (node.matches && node.matches(sel)) {
            reportOutcome(node, sel);
            return;
          }
          const found = node.querySelector && node.querySelector(sel);
          if (found) {
            reportOutcome(found, sel);
            return;
          }
        }
      }
    }
  });

  function reportOutcome(node, matchedSelector) {
    if (!inScope()) return;
    const isError = /error|danger/i.test(matchedSelector);
    safeSend({
      kind: "outcome",
      action_type: isError ? "error" : "success",
      description: (node.textContent || "").trim().slice(0, 200) ||
        (isError ? "Error toast shown" : "Success toast shown"),
      value: null,
      best_selector: null,
      best_selector_type: null,
      fallback_selector: null,
      wait_condition: null,
      timestamp: Date.now(),
    });
  }

  successObserver.observe(document.documentElement, { childList: true, subtree: true });

  // Always sent, even outside debug mode -- this is the single most
  // useful diagnostic line there is: if this never shows up in the
  // Python terminal, the script isn't running in the page at all
  // (wrong frame, blocked injection, etc.), which is a completely
  // different problem than "importance filtering is wrong".
  safeSend({
    kind: "debug",
    action_type: "installed",
    description: `Recorder script installed on ${location.href} (debug=${DEBUG})`,
    value: null,
    timestamp: Date.now(),
  });
})();