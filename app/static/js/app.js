// Shell behaviour shared by every page (htmx-ui skill). No build step; plain browser JS.
// User-facing messages live in the templates (spec 3.10); this script only places them.
(function () {
  "use strict";

  var status = document.getElementById("app-status");

  function announce(message) {
    var region = document.getElementById("announcer");
    if (!region) return;
    // Clear first so a repeated message is announced again.
    region.textContent = "";
    window.setTimeout(function () { region.textContent = message; }, 50);
  }

  // Show a problem visibly and announce it; offer Reload only when reloading fixes it.
  function showProblem(kind, offerReload) {
    if (!status) return;
    var message = status.getAttribute("data-msg-" + kind) || "";
    document.getElementById("app-status-message").textContent = message;
    document.getElementById("app-status-reload").hidden = !offerReload;
    status.hidden = false;
    announce(message);
  }

  document.getElementById("app-status-reload").addEventListener("click", function () {
    window.location.reload();
  });

  // Routes can ask for an announcement with the header HX-Trigger: {"announce": "..."}.
  document.body.addEventListener("announce", function (event) {
    if (event.detail && typeof event.detail.value === "string") announce(event.detail.value);
  });

  // A 403 means this page holds an old launch token, usually because the app restarted.
  document.body.addEventListener("htmx:responseError", function (event) {
    var xhr = event.detail && event.detail.xhr;
    if (xhr && xhr.status === 403) {
      showProblem("restarted", true);
    } else {
      showProblem("failed", false);
    }
  });

  // No response at all: the app was stopped or the connection dropped.
  document.body.addEventListener("htmx:sendError", function () {
    showProblem("offline", false);
  });

  // Fragments mark the element that should receive focus after a swap with data-autofocus.
  // Marks in the initially loaded page are not swaps; clear them so they can't steal focus later.
  document.querySelectorAll("[data-autofocus]").forEach(function (element) {
    element.removeAttribute("data-autofocus");
  });
  document.body.addEventListener("htmx:afterSettle", function () {
    var target = document.querySelector("[data-autofocus]");
    if (!target) return;
    target.removeAttribute("data-autofocus");
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    target.focus();
  });
})();
