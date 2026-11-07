// The command palette (M9.5 part A; ADR-098 item 7). Plain JavaScript, no build step.
//
// Ctrl+K (Cmd+K on macOS) or the top bar's [data-palette-open] button opens the modal dialog
// #palette from any page, also while typing in a field and with the single-key shortcuts
// switched off (this script does not read that switch). The search field is a combobox: focus
// stays in it, ArrowUp/ArrowDown move the active option (aria-selected="true", named by
// aria-activedescendant) and Enter opens it (an Enter typed before the answer arrives opens its
// first option). Esc and the Close button close the dialog, and focus goes back to what had it;
// a chosen option closes it and the page moves on. It opens over the list of shortcuts or Quick
// add (they close), never over a question. Tab and Shift+Tab stay inside.
//
// The answers are the protected fragments /palette/search?q= and /palette/recent?path=. The
// launch token is read from <body hx-headers> and sent as a header on each request; it is never
// in a URL or in the dialog. Typed text is never stored. Only the paths of the last 8 item
// pages (a job, company, application or fact) are kept in localStorage, newest first; the
// server resolves their titles, so no title or text is stored in the browser.
(function () {
  "use strict";

  var KEY = "jobpipeline.palette.recent";
  var MAX_RECENT = 8;
  var DEBOUNCE_MS = 150;
  var ITEM_PAGE = /^\/(?:(?:jobs|companies|applications)\/[A-Za-z0-9_-]{1,64}|facts\/[A-Za-z0-9_-]{1,64}\/edit)$/;

  // --- recent pages (localStorage can be blocked or full: every access is guarded) ----------

  function readRecent() {
    try {
      var stored = JSON.parse(window.localStorage.getItem(KEY) || "[]");
      if (!Array.isArray(stored)) return [];
      return stored.filter(function (path) { return typeof path === "string" && ITEM_PAGE.test(path); }).slice(0, MAX_RECENT);
    } catch (_) {
      return [];
    }
  }

  function remember(path) {
    if (!ITEM_PAGE.test(path)) return;
    var paths = readRecent().filter(function (known) { return known !== path; });
    paths.unshift(path);
    try {
      window.localStorage.setItem(KEY, JSON.stringify(paths.slice(0, MAX_RECENT)));
    } catch (_) {
      // Storage is unavailable: the palette just has no recent list.
    }
  }

  remember(window.location.pathname);

  // --- the dialog ----------------------------------------------------------------------------

  var dialog = document.getElementById("palette");
  var input = document.getElementById("palette-input");
  var results = document.getElementById("palette-results");
  if (!dialog || !input || !results || typeof dialog.showModal !== "function") return;

  var placeholder = results.innerHTML; // "Loading…", from the template
  var announcer = document.getElementById("palette-status");
  var opener = null;
  var timer = null;
  var controller = null;
  var serial = 0;
  var active = -1;
  var inFlight = false;
  var pendingEnter = false; // Enter pressed before the answer to what was typed arrived
  var leaving = false; // an option was chosen: the page moves on, focus does not go back

  function options() {
    return Array.prototype.slice.call(results.querySelectorAll('[role="option"]'));
  }

  function setActive(index) {
    var list = options();
    active = index >= 0 && index < list.length ? index : -1;
    list.forEach(function (option, i) {
      option.setAttribute("aria-selected", i === active ? "true" : "false");
    });
    if (active >= 0) {
      input.setAttribute("aria-activedescendant", list[active].id);
      list[active].scrollIntoView({ block: "nearest" });
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function tokenHeaders() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}");
    } catch (_) {
      return {};
    }
  }

  // A fresh answer: number its options, make the first one active and let the count be heard
  // (a live region announces a change of its text, so its words are put back a moment later).
  // The count is spoken by one live region that stays in the dialog (#palette-status): a region
  // swapped in with the answer is not reliably announced.
  function say(words) {
    if (!announcer) return;
    announcer.textContent = "";
    window.setTimeout(function () { announcer.textContent = words; }, 60);
  }

  function show(markup) {
    results.innerHTML = markup;
    options().forEach(function (option, i) { option.id = "palette-option-" + (i + 1); });
    setActive(options().length ? 0 : -1);
    input.setAttribute("aria-expanded", options().length ? "true" : "false");
    var status = results.querySelector("[data-palette-status]");
    if (status) say(status.textContent);
    if (pendingEnter) {
      pendingEnter = false;
      if (options().length) options()[0].click();
    }
  }

  // The failure line is built here from the template's words, as an alert, only when needed.
  function fail() {
    var line = document.createElement("p");
    line.className = "field-error";
    line.setAttribute("role", "alert");
    line.textContent = results.getAttribute("data-msg-failed") || "";
    results.replaceChildren(line);
    setActive(-1);
    input.setAttribute("aria-expanded", "false");
    pendingEnter = false;
  }

  function load(url) {
    if (controller) controller.abort();
    controller = new AbortController();
    var mine = ++serial;
    inFlight = true;
    fetch(url, { headers: tokenHeaders(), credentials: "same-origin", cache: "no-store", signal: controller.signal })
      .then(function (response) {
        if (!response.ok) throw new Error("palette");
        return response.text();
      })
      .then(function (markup) {
        if (mine === serial) inFlight = false;
        if (mine === serial && dialog.open) show(markup);
      })
      .catch(function (error) {
        if (error && error.name === "AbortError") return;
        if (mine === serial) {
          inFlight = false;
          fail();
        }
      });
  }

  function loadRecent() {
    var query = readRecent().map(function (path) { return "path=" + encodeURIComponent(path); }).join("&");
    load("/palette/recent" + (query ? "?" + query : ""));
  }

  function search() {
    timer = null;
    if (!input.value.trim()) loadRecent();
    else load("/palette/search?q=" + encodeURIComponent(input.value));
  }

  function open(from) {
    if (dialog.open) {
      input.focus();
      return;
    }
    // A question that must be answered first (a modal or the Remove dialog) keeps the screen;
    // the list of shortcuts and Quick add are not questions, so the palette takes their place.
    var other = document.querySelector("dialog[open]");
    if (other && !other.hasAttribute("data-global-dialog")) return;
    if (other) other.close();
    opener = from instanceof HTMLElement ? from : (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    input.value = "";
    dialog.showModal();
    input.focus();
    loadRecent();
  }

  var mac = /Mac|iPhone|iPad/i.test((navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "");
  // The key that works here: Cmd+K on a Mac, Ctrl+K elsewhere.
  document.querySelectorAll("[data-palette-open]").forEach(function (button) {
    button.setAttribute("aria-keyshortcuts", mac ? "Meta+K" : "Control+K");
  });

  // Capture phase, so a field or a screen's own key handler can't swallow it.
  document.addEventListener("keydown", function (event) {
    var modifier = mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
    if (!modifier || event.altKey || event.shiftKey || event.key.toLowerCase() !== "k") return;
    event.preventDefault();
    open(null);
  }, true);

  document.addEventListener("click", function (event) {
    var button = event.target instanceof Element ? event.target.closest("[data-palette-open]") : null;
    if (button) open(button);
  });

  dialog.addEventListener("click", function (event) {
    if (event.target instanceof Element && event.target.closest("[data-palette-close]")) {
      dialog.close();
    } else if (event.target === dialog) {
      var box = dialog.getBoundingClientRect();
      var outside = event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
      if (outside) dialog.close();
    }
  });

  // A chosen option (Enter or a click) leaves the dialog: it closes first, and focus does not
  // go back to the opener, because the page moves on (or, for the Inbox's own add panel, the
  // inbox script moves focus there).
  dialog.addEventListener("click", function (event) {
    var option = event.target instanceof Element ? event.target.closest('[role="option"]') : null;
    if (!option || !dialog.open) return;
    leaving = true;
    dialog.close();
  });

  input.addEventListener("input", function () {
    pendingEnter = false;
    setActive(-1);
    if (timer !== null) window.clearTimeout(timer);
    timer = window.setTimeout(search, DEBOUNCE_MS);
  });

  input.addEventListener("keydown", function (event) {
    var count = options().length;
    if (event.key === "Escape") {
      // A search field would use Esc to clear itself first; here it closes the dialog.
      event.preventDefault();
      dialog.close();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!count) return;
      var step = event.key === "ArrowDown" ? 1 : -1;
      setActive(active < 0 ? (step > 0 ? 0 : count - 1) : (active + step + count) % count);
    } else if (event.key === "Enter" && !event.isComposing && active >= 0) {
      event.preventDefault();
      options()[active].click();
    } else if (event.key === "Enter" && !event.isComposing && (timer !== null || inFlight)) {
      event.preventDefault();
      pendingEnter = true; // opened by show() once the answer to what was typed is here
    }
  });

  // Tab and Shift+Tab wrap inside the dialog (the options are reached by the arrow keys; the
  // results region is a stop of its own, so a long answer can be scrolled by keyboard).
  dialog.addEventListener("keydown", function (event) {
    if (event.key !== "Tab") return;
    var stops = dialog.querySelectorAll('input, [tabindex="0"], button:not([disabled]), a[href]:not([tabindex="-1"])');
    if (!stops.length) return;
    var first = stops[0];
    var last = stops[stops.length - 1];
    var current = document.activeElement;
    var inside = dialog.contains(current);
    if (event.shiftKey && (!inside || current === first)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (!inside || current === last)) {
      event.preventDefault();
      first.focus();
    }
  });

  dialog.addEventListener("close", function () {
    if (controller) controller.abort();
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
    serial += 1;
    inFlight = false;
    pendingEnter = false;
    results.innerHTML = placeholder;
    input.setAttribute("aria-expanded", "false");
    if (announcer) announcer.textContent = "";
    var back = opener;
    opener = null;
    // "close" comes a moment after close(): focus that has gone somewhere meanwhile (the Inbox's
    // add panel) stays there; only lost focus (on <body>, or in the closed dialog) comes back.
    var current = document.activeElement;
    var lost = !current || current === document.body || dialog.contains(current);
    if (leaving || !lost) {
      leaving = false;
      return;
    }
    if (back === document.body || !back || !back.isConnected || back.closest("dialog:not([open])")) {
      back = document.querySelector("[data-palette-open]");
    }
    if (back) back.focus();
  });

  // A page restored from the browser's history cache must not come back with the dialog open.
  window.addEventListener("pagehide", function () {
    if (dialog.open) dialog.close();
  });
}());
