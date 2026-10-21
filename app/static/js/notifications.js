/* Non-modal notifications. Only a displayed list is marked seen, by a protected POST. */
(function () {
  "use strict";
  var toggle = document.getElementById("notifications-toggle");
  var popover = document.getElementById("notifications-popover");
  var slot = document.getElementById("notifications-slot");
  if (!toggle || !popover || !slot) return;
  var pending = new Set();
  var focusId = null;
  function error() { document.getElementById("notifications-error").hidden = false; }
  function open(value) {
    popover.hidden = !value;
    toggle.setAttribute("aria-expanded", String(value));
    if (!value) return;
    document.getElementById("notifications-error").hidden = true;
    var tasks = document.getElementById("tasks-toggle");
    if (tasks && tasks.getAttribute("aria-expanded") === "true") tasks.click();
    window.htmx.trigger(popover, "notifications:refresh");
  }
  toggle.addEventListener("click", function () { open(popover.hidden); });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || popover.hidden || event.defaultPrevented) return;
    event.preventDefault();
    var inside = slot.contains(document.activeElement);
    open(false);
    if (inside) toggle.focus();
  });
  document.addEventListener("pointerdown", function (event) {
    if (!slot.contains(event.target)) open(false);
  });
  slot.addEventListener("focusout", function (event) {
    if (event.relatedTarget && !slot.contains(event.relatedTarget)) open(false);
  });
  document.addEventListener("focusin", function () {
    focusId = popover.contains(document.activeElement) ? (document.activeElement.id || "") : null;
  });
  function panelGet(event) {
    return event.detail && event.detail.elt === popover;
  }
  function editedChoice() {
    return Array.from(popover.querySelectorAll("select")).some(function (select) {
      return Array.from(select.options).some(function (o) { return o.selected !== o.defaultSelected; });
    });
  }
  document.body.addEventListener("htmx:beforeRequest", function (event) {
    if (panelGet(event) && popover.hidden) event.preventDefault();
  });
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var d = event.detail;
    if (!d || d.target.id !== "notifications-panel" || !d.requestConfig ||
        d.requestConfig.verb.toLowerCase() !== "get") return;
    if (popover.hidden || editedChoice() || popover.contains(document.activeElement)) d.shouldSwap = false;
  });
  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (event.target.id !== "notifications-count") return;
    var count = document.getElementById("notifications-count");
    var due = new Set(JSON.parse(count.dataset.dueApplications));
    document.querySelectorAll("[data-due-marker]").forEach(function (marker) {
      marker.hidden = !due.has(marker.dataset.dueMarker);
    });
  });
  document.body.addEventListener("htmx:afterSettle", function (event) {
    if (!popover.contains(event.target) || popover.hidden) return;
    if (focusId !== null && !popover.contains(document.activeElement)) {
      var next = focusId && document.getElementById(focusId);
      (next || document.getElementById("notifications-heading")).focus();
    }
    var keys = Array.from(popover.querySelectorAll('[data-unseen="true"]'))
      .map(function (row) { return row.dataset.reminderKey; })
      .filter(function (key) { return !pending.has(key); });
    if (!keys.length) return;
    keys.forEach(function (key) { pending.add(key); });
    var headers = JSON.parse(document.body.getAttribute("hx-headers"));
    headers["Content-Type"] = "application/x-www-form-urlencoded";
    fetch("/notifications/seen", { method: "POST", headers: headers,
      body: new URLSearchParams({ keys: JSON.stringify(keys) }), credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("seen");
        // Seen changes the count only. Keep the displayed action forms in place.
        window.htmx.trigger(toggle, "notifications:seen");
      }).catch(error).finally(function () {
        keys.forEach(function (key) { pending.delete(key); });
      });
  });
  ["htmx:sendError", "htmx:responseError"].forEach(function (name) {
    document.body.addEventListener(name, function (event) { if (panelGet(event)) error(); });
  });
  document.addEventListener("click", function (event) {
    var link = event.target.closest("[data-open-tasks]");
    if (!link) return;
    event.preventDefault();
    open(false);
    var tasks = document.getElementById("tasks-toggle");
    tasks.focus();
    if (tasks.getAttribute("aria-expanded") !== "true") tasks.click();
  });
}());
