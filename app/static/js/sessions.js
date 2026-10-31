// Session answers stay in page memory only; task refreshes never erase newer typing.
(function () {
  "use strict";
  var drafts = new Map(), pending = new Set(), automatic = new WeakSet(), focus = new WeakMap();
  function box() { return document.querySelector("textarea[data-session-answer]"); }
  function protectedPanel(root) {
    var field = root.querySelector("textarea[data-session-answer]");
    return pending.size > 0 || root.hasAttribute("data-session-conflict") ||
      root.hasAttribute("data-session-retained") ||
      (root.getAttribute("data-session-phase") !== "waiting" && field && !field.readOnly && field.value !== field.defaultValue) ||
      root.querySelector("dialog[open], details[open]") !== null;
  }
  function remember(field) {
    if (!field) return;
    var key = field.getAttribute("data-session-answer"), saved = drafts.get(key);
    var versions = saved ? saved.versions : Array.from(field.form.querySelectorAll("input[type=hidden]"))
      .map(function (input) { return {name: input.name, value: input.value}; });
    drafts.set(key, {value: field.value, submitted: saved ? saved.submitted : null, versions: versions,
      start: field.selectionStart, end: field.selectionEnd});
  }
  document.addEventListener("input", function (e) {
    if (e.target.matches("textarea[data-session-answer]")) remember(e.target);
  });
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    var d = e.detail, root = document.getElementById("session-panel");
    if (!root || d.target !== root) return;
    if (d.requestConfig.verb === "get") {
      automatic.add(d.xhr);
      if (protectedPanel(root)) e.preventDefault();
      return;
    }
    if (d.requestConfig.verb !== "post") return;
    pending.add(d.xhr);
    d.xhr.addEventListener("loadend", function () { pending.delete(d.xhr); }, {once: true});
    var form = d.elt;
    if (!form || form.id !== "session-answer-form") return;
    var field = box();
    remember(field);
    if (field) drafts.get(field.getAttribute("data-session-answer")).submitted = field.value;
  });
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    if (!e.detail.target || e.detail.target.id !== "session-panel" || !e.detail.shouldSwap) return;
    if (automatic.has(e.detail.xhr) && protectedPanel(e.detail.target)) {
      e.detail.shouldSwap = false;
      return;
    }
    var active = document.activeElement;
    focus.set(e.detail.xhr, {within: e.detail.target.contains(active), id: active && active.id,
      automatic: automatic.has(e.detail.xhr)});
    remember(box());
  });
  document.body.addEventListener("htmx:afterSettle", function (e) {
    var root = document.getElementById("session-panel");
    if (!root || !(e.detail.target && e.detail.target.id === "session-panel")) return;
    var previous = focus.get(e.detail.xhr);
    focus.delete(e.detail.xhr);
    var field = box(), restored = false;
    if (field) {
      var saved = drafts.get(field.getAttribute("data-session-answer"));
      if (saved) {
        if (saved.value === field.value && saved.value === saved.submitted) drafts.delete(field.getAttribute("data-session-answer"));
        else if (saved.value !== field.value) {
          field.value = saved.value;
          field.setSelectionRange(saved.start, saved.end);
          var note = root.querySelector("[data-session-typing-note]");
          if (note) note.hidden = false;
          restored = true;
          // A refreshed result may advance versions while this text still belongs to the
          // old editor. Keep that snapshot: a later retry must conflict, never overwrite.
          saved.versions.forEach(function (version) {
            var input = field.form.elements.namedItem(version.name);
            if (input) input.value = version.value;
          });
          if (!field.readOnly && root.getAttribute("data-session-phase") !== "waiting")
            root.setAttribute("data-session-retained", "");
        }
      }
    }
    if (previous && previous.within && (previous.automatic || (restored && previous.id === "session-answer"))) {
      var target = restored && previous.id === "session-answer" ? field : document.getElementById(previous.id || "");
      if (!target || !root.contains(target)) target = document.getElementById("session-status");
      if (target) target.focus();
    }
  });
  document.addEventListener("change", function (e) {
    if (e.target.id !== "session-length") return;
    var n = e.target.value === "5" ? 5 : 3, line = document.getElementById("session-request-notice");
    if (line) line.textContent = "A " + n + "-question session uses up to " + (n * 2) + " requests.";
  });
  document.addEventListener("click", function (e) {
    if (!e.target.closest("[data-session-open-tasks]")) return;
    var toggle = document.getElementById("tasks-toggle");
    if (toggle && toggle.getAttribute("aria-expanded") !== "true") toggle.click();
    if (toggle) toggle.focus();
  });
}());
