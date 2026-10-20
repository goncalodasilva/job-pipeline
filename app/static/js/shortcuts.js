// The job inbox screen (M4.7, htmx-ui skill): single-key shortcuts and the small client parts
// of the screen. No build step; plain browser JS. User-facing words live in the templates
// (spec 3.10); this script only places them.
//
// - Shortcuts: J next, K previous, A mark to apply, R remove, Enter details, Z undo, ? help and
//   Esc to close. They are ignored while the user types, with a modifier key, or while a dialog
//   is open, and the versioned Settings switch turns them all off (data-shortcuts on the page);
//   only "?" still opens the help, which then says how to turn them on. Each key presses the
//   button the screen shows, so the keyboard and the mouse share one path to the server.
// - The queue stays as it was when the page loaded while the user moves through it, so J then
//   K always come back. The card that J would show is fetched ahead of time with a GET, which
//   marks nothing; a card is marked seen by the POST it sends itself once it is displayed.
//   That answer changes the job's version, so an action pressed before it arrives is held
//   (data-seen-pending) and sent once it is here, with the new version.
// - Remove opens a modal question; "Other" asks for an optional reason.
// - Undo lines disappear after the seconds they name (data-undo-seconds), but not while the
//   user is on them (focus or pointer).
(function () {
  "use strict";

  var page = document.getElementById("inbox-page");
  if (!page) return;

  var CACHE_MS = 30000;
  var SEEN_WAIT_MS = 5000;
  var prefetched = Object.create(null); // card address -> { html, at }
  var requested = Object.create(null); // card addresses on their way
  var lastInbox = null;
  var undoTimer = null;
  var heldAction = null; // { form, submitter }: pressed before the card's seen answer
  var seenTimer = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function shortcutsOn() {
    return page.getAttribute("data-shortcuts") === "true";
  }

  function announce(text) {
    document.body.dispatchEvent(new CustomEvent("announce", { detail: { value: text } }));
  }

  function card() {
    return byId("inbox-card");
  }

  function queueLinks() {
    return Array.prototype.slice.call(document.querySelectorAll("#inbox-queue a[data-job-id]"));
  }

  // The queue link next to the shown card ("next" or "previous"), or null.
  function neighbour(direction) {
    var shown = card();
    if (!shown) return null;
    var links = queueLinks();
    var id = shown.getAttribute("data-job-id");
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute("data-job-id") === id) {
        return links[direction === "next" ? i + 1 : i - 1] || null;
      }
    }
    return null;
  }

  function typing(element) {
    if (!(element instanceof Element)) return false;
    return element.closest("input, textarea, select, [contenteditable]:not([contenteditable='false'])") !== null;
  }

  // Focus is on something that does its own thing with Enter.
  function handlesEnter(element) {
    if (!(element instanceof Element)) return false;
    return element.closest("a[href], button, summary, [tabindex]:not([tabindex='-1'])") !== null;
  }

  // --- moving between cards ------------------------------------------------------------------

  function fetchHeaders() {
    var headers = { "HX-Request": "true" };
    try {
      var held = JSON.parse(document.body.getAttribute("hx-headers") || "{}");
      Object.keys(held).forEach(function (name) { headers[name] = held[name]; });
    } catch (error) { /* no token: the request is refused and nothing is kept */ }
    return headers;
  }

  // The address of a queue link's card, only when it is a card of this site: the token is
  // sent with the request, so nothing else may be asked for.
  function cardUrl(link) {
    var url = link ? link.getAttribute("hx-get") : null;
    if (!url || url.indexOf("/inbox/jobs/") !== 0) return null;
    try {
      var parsed = new URL(url, window.location.href);
      return parsed.origin === window.location.origin ? url : null;
    } catch (error) {
      return null;
    }
  }

  function prefetchNext() {
    var url = cardUrl(neighbour("next"));
    if (!url || prefetched[url] || requested[url] || typeof window.fetch !== "function") return;
    requested[url] = true;
    window.fetch(url, { headers: fetchHeaders(), credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.text() : ""; })
      .then(function (html) {
        delete requested[url];
        if (html) prefetched[url] = { html: html, at: Date.now() };
      })
      .catch(function () { delete requested[url]; });
  }

  // Shows the card of a queue link: from the prefetched copy when it is fresh and not
  // waiting for a task, else by following the link (htmx fetches the card).
  function open(link) {
    var url = cardUrl(link);
    var kept = url ? prefetched[url] : null;
    if (url) delete prefetched[url];
    if (kept && Date.now() - kept.at < CACHE_MS && kept.html.indexOf("data-processing") < 0 &&
        window.htmx && typeof window.htmx.swap === "function") {
      window.htmx.swap("#inbox-card", kept.html, { swapStyle: "outerHTML" });
      window.history.replaceState(null, "", link.getAttribute("href"));
    } else {
      link.click();
    }
  }

  function go(direction) {
    var link = neighbour(direction);
    if (link) open(link);
  }

  // --- the remove question --------------------------------------------------------------------

  function closeRemove() {
    var dialog = byId("remove-dialog");
    if (dialog && dialog.open) dialog.close();
  }

  function otherButton() {
    return document.querySelector("#remove-dialog [data-other-reason]");
  }

  function revealOther(focus) {
    var field = byId("remove-other");
    if (!field) return;
    field.hidden = false;
    var text = byId("remove-text");
    if (text) text.disabled = false;
    var other = otherButton();
    if (other) other.setAttribute("aria-expanded", "true");
    if (text && focus) text.focus();
  }

  function clearAddedReason(form) {
    form.querySelectorAll("[data-added-reason]").forEach(function (input) { input.remove(); });
  }

  function resetOther() {
    var field = byId("remove-other");
    if (!field) return;
    clearAddedReason(field.form || document);
    field.hidden = true;
    var text = byId("remove-text");
    if (text) text.disabled = false;
    var other = otherButton();
    if (other) other.setAttribute("aria-expanded", "false");
  }

  // --- the undo line --------------------------------------------------------------------------

  function undoLine() {
    var undo = byId("inbox-undo");
    return undo && !undo.hidden && undo.hasAttribute("data-undo-started") ? undo : null;
  }

  function undoHeld(undo) {
    return undo.matches(":hover") || undo.contains(document.activeElement);
  }

  function armUndo(undo) {
    var seconds = parseInt(undo.getAttribute("data-undo-seconds") || "", 10);
    window.clearTimeout(undoTimer);
    undoTimer = window.setTimeout(function () {
      if (!undo.isConnected) return;
      var heading = byId("inbox-card-heading");
      if (undo.contains(document.activeElement) && heading) heading.focus();
      undo.hidden = true;
      undo.textContent = "";
    }, seconds * 1000);
  }

  function startUndo() {
    var undo = byId("inbox-undo");
    if (!undo || undo.hidden || undo.hasAttribute("data-undo-started")) return;
    if (!(parseInt(undo.getAttribute("data-undo-seconds") || "", 10) > 0)) return;
    undo.setAttribute("data-undo-started", "");
    armUndo(undo);
  }

  // The time does not run while the user is on the line, and starts again in full after.
  function undoPointerOrFocus(event, entering) {
    var undo = undoLine();
    if (!undo || !(event.target instanceof Node) || !undo.contains(event.target)) return;
    if (entering) {
      window.clearTimeout(undoTimer);
    } else {
      window.setTimeout(function () {
        var line = undoLine();
        if (line && !undoHeld(line)) armUndo(line);
      }, 0);
    }
  }

  document.addEventListener("focusin", function (event) { undoPointerOrFocus(event, true); });
  document.addEventListener("focusout", function (event) { undoPointerOrFocus(event, false); });
  document.addEventListener("mouseover", function (event) { undoPointerOrFocus(event, true); });
  document.addEventListener("mouseout", function (event) { undoPointerOrFocus(event, false); });

  // --- the card after every swap --------------------------------------------------------------

  function syncQueue(shown) {
    var id = shown.getAttribute("data-job-id");
    queueLinks().forEach(function (link) {
      if (link.getAttribute("data-job-id") === id) link.setAttribute("aria-current", "true");
      else link.removeAttribute("aria-current");
    });
  }

  // The card's seen answer is here (or will not come): actions pressed meanwhile go now.
  function releaseSeen() {
    window.clearTimeout(seenTimer);
    var shown = card();
    if (shown) shown.removeAttribute("data-seen-pending");
    var action = heldAction;
    heldAction = null;
    if (!action || !action.form.isConnected) return;
    // A click, not requestSubmit: htmx reads which button was used from the click.
    if (action.submitter && action.submitter.isConnected) action.submitter.click();
    else action.form.requestSubmit();
  }

  function afterSwap() {
    var inbox = byId("inbox");
    if (inbox !== lastInbox) { // a new queue: addresses and versions of the old one are void
      prefetched = Object.create(null);
      lastInbox = inbox;
    }
    startUndo();
    var shown = card();
    if (!shown) return;
    syncQueue(shown);
    if (shown.hasAttribute("data-seen-pending")) {
      window.clearTimeout(seenTimer);
      seenTimer = window.setTimeout(releaseSeen, SEEN_WAIT_MS);
    }
    var said = shown.getAttribute("data-announce");
    if (said) {
      shown.removeAttribute("data-announce");
      announce(said);
    }
    var notice = byId("inbox-notice");
    if (notice && notice.firstElementChild) { // a refused move: the question behind is moot
      closeRemove();
      var prompt = notice.firstElementChild;
      if (typeof prompt.focus === "function") prompt.focus();
    }
    var again = document.querySelector("#remove-dialog[data-reopen]");
    if (again && typeof again.showModal === "function" && !again.open) {
      again.removeAttribute("data-reopen");
      again.showModal();
      if (byId("remove-text")) revealOther(false);
      var invalid = again.querySelector("[aria-invalid='true']");
      if (invalid) invalid.focus();
    }
    prefetchNext();
  }

  document.body.addEventListener("htmx:afterSettle", afterSwap);
  afterSwap();

  // The user is working in the card: a menu is open, a field has focus or has been typed in.
  function cardInUse(shown) {
    if (shown.querySelector("details[open]")) return true;
    var active = document.activeElement;
    if (active && shown.contains(active) && active.closest("input, select, textarea, summary")) {
      return true;
    }
    return Array.prototype.some.call(
      shown.querySelectorAll("input[type='date'], input[type='text']"),
      function (input) { return input.value !== input.defaultValue; }
    );
  }

  // A card that asks for itself again (Processing) or the whole screen (a job was added) must
  // not take a dialog, an open menu or typed words away from the user; the next event asks again.
  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var detail = event.detail || {};
    var from = detail.elt;
    if (!(from instanceof Element) || from !== detail.target || !page.contains(from)) return;
    if (from.querySelector("dialog[open]") || (from.id === "inbox-card" && cardInUse(from))) {
      event.preventDefault();
    }
  });

  // The card was marked seen: its forms carry the job's new version from now on, and what was
  // pressed meanwhile is sent. Any answer counts, so a refused one cannot hold the card for ever.
  document.body.addEventListener("htmx:afterRequest", function (event) {
    var detail = event.detail || {};
    var path = detail.pathInfo && detail.pathInfo.requestPath;
    var found = /^\/inbox\/jobs\/([^/]+)\/seen$/.exec(path || "");
    if (!found) return;
    var shown = card();
    var answer = null;
    if (detail.successful && detail.xhr) {
      try { answer = JSON.parse(detail.xhr.responseText); } catch (error) { answer = null; }
    }
    if (answer && shown && shown.getAttribute("data-job-id") === found[1]) {
      shown.querySelectorAll("input[name='version']").forEach(function (input) {
        input.value = String(answer.version);
      });
      shown.setAttribute("data-state", String(answer.state));
    }
    if (answer) {
      queueLinks().forEach(function (link) {
        if (link.getAttribute("data-job-id") !== found[1]) return;
        var badge = link.querySelector(".queue-new");
        if (badge) badge.remove();
      });
    }
    if (shown && shown.getAttribute("data-job-id") === found[1]) releaseSeen();
  });

  // An action of the card waits for its seen answer; the remove question's "Other" removes with
  // its words, any chip or the plain button without them (so Enter in the box can never mean a
  // chip, and a chip never carries words). Capture phase: this runs before htmx reads the form.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var shown = card();
    if (!(form instanceof HTMLFormElement) || !shown || !shown.contains(form) ||
        form.id === "inbox-seen") return;
    if (shown.hasAttribute("data-seen-pending")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      heldAction = { form: form, submitter: event.submitter || null };
      return;
    }
    if (!form.closest("#remove-dialog")) return;
    var submitter = event.submitter;
    var text = byId("remove-text");
    var typed = text && !byId("remove-other").hidden && text.value.trim() !== "";
    var plain = submitter && submitter.hasAttribute("data-remove-plain");
    clearAddedReason(form);
    if (typed && plain) { // words were typed: the plain button means "Other" with them
      var reason = document.createElement("input");
      reason.type = "hidden";
      reason.name = "reason";
      reason.value = "other";
      reason.setAttribute("data-added-reason", "");
      form.appendChild(reason);
      text.disabled = false;
      return;
    }
    if (text) text.disabled = !(submitter && submitter.hasAttribute("data-other-reason"));
  }, true);

  // --- clicks ---------------------------------------------------------------------------------

  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    if (target.closest("#inbox-add-toggle")) {
      setAdd(!byId("inbox-add").classList.contains("is-open"), true);
    } else if (target.closest("[data-action='remove']")) {
      var dialog = byId("remove-dialog");
      if (dialog && !dialog.open && typeof dialog.showModal === "function") dialog.showModal();
    } else if (target.closest("[data-other-reason]")) {
      var field = byId("remove-other");
      if (field && field.hidden) { // the first press only opens the box
        event.preventDefault();
        revealOther(true);
      }
    } else if (target.closest("[data-close-dialog]")) {
      closeRemove();
    } else if (target.closest("[data-nav]")) {
      go(target.closest("[data-nav]").getAttribute("data-nav") === "next" ? "next" : "previous");
    }
  });

  // "close" doesn't bubble, so it is caught on its way down.
  document.addEventListener("close", function (event) {
    var dialog = event.target;
    if (!(dialog instanceof Element) || dialog.id !== "remove-dialog") return;
    resetOther();
    // Focus goes back to what opened the question; a dialog that was opened after a swap
    // has no earlier focus to go back to, and must not leave it on its own hidden field.
    var opener = byId("inbox-remove");
    var active = document.activeElement;
    if (opener && (!active || active === document.body || dialog.contains(active))) opener.focus();
  }, true);

  // --- "+ Add job" ----------------------------------------------------------------------------

  function setAdd(open, focus) {
    var panel = byId("inbox-add");
    var toggle = byId("inbox-add-toggle");
    if (!panel || !toggle) return;
    panel.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open && focus) {
      var heading = byId("job-intake-heading");
      if (heading) heading.focus();
    }
  }

  // --- keys -----------------------------------------------------------------------------------

  function help() {
    return byId("inbox-help");
  }

  function toggleHelp() {
    var details = help();
    if (!details) return;
    details.open = !details.open;
    if (details.open) details.querySelector("summary").focus();
  }

  function escape(event) {
    if (document.querySelector("dialog[open]")) return; // the dialog closes itself
    var panel = byId("inbox-add");
    var toggle = byId("inbox-add-toggle");
    var details = help();
    if (panel && panel.classList.contains("is-open")) {
      var inside = panel.contains(document.activeElement);
      setAdd(false, false);
      if (inside && toggle) toggle.focus();
      event.preventDefault();
    } else if (details && details.open) {
      var held = details.contains(document.activeElement);
      details.open = false;
      if (held) details.querySelector("summary").focus();
      event.preventDefault();
    }
  }

  function press(selector) {
    var button = document.querySelector(selector);
    if (button) button.click();
  }

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented) return;
    if (event.key === "Escape") { escape(event); return; }
    // Enter in the reason box removes with "Other" and what was typed.
    if (event.key === "Enter" && event.target instanceof Element && event.target.id === "remove-text") {
      var other = otherButton();
      event.preventDefault();
      if (other) other.click(); // a click, so htmx sends this button's reason
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (typing(event.target) || document.querySelector("dialog[open]")) return;
    var key = event.key;
    if (key === "?") { // the help stays reachable with shortcuts off: it says how to turn them on
      toggleHelp();
      event.preventDefault();
      return;
    }
    if (!shortcutsOn()) return;
    var handled = true;
    if (key === "j" || key === "J") go("next");
    else if (key === "k" || key === "K") go("previous");
    else if ((key === "a" || key === "A") && !event.repeat) press("#inbox-card [data-action='apply']");
    else if ((key === "r" || key === "R") && !event.repeat) press("#inbox-card [data-action='remove']");
    else if ((key === "z" || key === "Z") && !event.repeat) press("#inbox-undo:not([hidden]) button");
    else if (key === "Enter" && !handlesEnter(event.target)) press("#inbox-card-heading");
    else handled = false;
    if (handled) event.preventDefault();
  });
}());
