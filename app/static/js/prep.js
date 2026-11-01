/* A passive prep refresh preserves a safe heading's focus without moving it from elsewhere. */
(function () {
  "use strict";
  var restore = "";
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var d = event.detail || {};
    var requester = d.requestConfig && d.requestConfig.elt;
    var active = document.activeElement;
    restore = "";
    if (d.shouldSwap && d.target && d.target.id === "prep" && requester === d.target &&
        active && d.target.contains(active) && active.hasAttribute("data-refresh-safe-focus")) {
      restore = active.id;
    }
  });
  document.body.addEventListener("htmx:afterSettle", function () {
    if (!restore) return;
    var next = document.getElementById(restore);
    restore = "";
    if (next && (document.activeElement === document.body || document.activeElement === next)) next.focus();
  });
}());
