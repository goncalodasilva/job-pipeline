/* A reminder refresh must not discard an unfinished choice or an active action. */
(function () {
  "use strict";
  if (!document.getElementById("today-content")) return;
  function busy() {
    var root = document.getElementById("today-content");
    if (!root) return false;
    if (root.contains(document.activeElement) && document.activeElement.closest("form")) return true;
    return Array.from(root.querySelectorAll("select")).some(function (select) {
      return Array.from(select.options).some(function (o) { return o.selected !== o.defaultSelected; });
    });
  }
  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var d = event.detail;
    if (d.elt.id === "today-content" && busy()) event.preventDefault();
  });
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var d = event.detail;
    if (d.target.id === "today-content" && d.requestConfig &&
        d.requestConfig.verb.toLowerCase() === "get" && busy()) d.shouldSwap = false;
  });
}());
