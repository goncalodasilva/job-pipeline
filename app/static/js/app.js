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

  var reload = document.getElementById("app-status-reload");
  if (reload) {
    reload.addEventListener("click", function () {
      window.location.reload();
    });
  }

  // Narrow layouts: the top-bar toggle shows and hides the sidebar (CSS hides it by default).
  var toggle = document.querySelector(".sidebar-toggle");
  var sidebar = document.getElementById("sidebar");

  function setSidebarOpen(open, focusTarget) {
    sidebar.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (focusTarget) focusTarget.focus();
  }

  if (toggle && sidebar) {
    toggle.addEventListener("click", function () {
      var open = toggle.getAttribute("aria-expanded") !== "true";
      setSidebarOpen(open, open ? sidebar.querySelector("a") : null);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (toggle.getAttribute("aria-expanded") !== "true") return;
      if (window.getComputedStyle(toggle).display === "none") return; // wide layout
      // Only when the user is in the menu; Escape elsewhere belongs to other features.
      var active = document.activeElement;
      if (active !== toggle && !sidebar.contains(active)) return;
      event.preventDefault();
      setSidebarOpen(false, toggle);
    });
  }

  // Esc inside an in-page question (such as "Remove the saved token?") answers it like its
  // cancel button ("Keep it"), which the server handles as usual.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    var question = event.target instanceof Element ? event.target.closest(".remove-confirm") : null;
    var cancel = question ? question.querySelector("[data-cancel]") : null;
    if (!cancel || cancel.disabled) return;
    event.preventDefault();
    cancel.click();
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
    // Only elements that can't take focus get tabindex="-1"; controls keep their Tab order.
    if (target.tabIndex < 0 && !target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    target.focus();
  });

  // Live updates (ADR-076 item 1): the server streams events at the URL in <body data-events-url>
  // (app pages only; the setup guide has none). EventSource can't send headers, so the stream is
  // read with fetch and the launch token goes in X-JP-Token like every other request, never in
  // the URL. Each "resync" event becomes a "tasks:resync" event on the body, which the parts of
  // the page that show tasks listen for. Anything else is ignored, and a refused or wrong
  // response ends the reader quietly: reconnecting and telling the user come later (M2.18).
  var eventsUrl = document.body.getAttribute("data-events-url");
  var MAX_PENDING = 64 * 1024; // the longest unfinished message the reader will keep

  function launchToken() {
    try {
      var value = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-JP-Token"];
      return typeof value === "string" ? value : "";
    } catch (error) {
      return "";
    }
  }

  // One message is the lines between blank lines: ": comment" lines and fields such as
  // "event: resync" and "data: {}". Only the event name matters here.
  function handleMessage(message) {
    var name = "";
    message.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) name = line.slice(6).replace(/^ /, "");
    });
    if (name === "resync") {
      document.body.dispatchEvent(new CustomEvent("tasks:resync", { bubbles: true }));
    }
  }

  // Handles the complete messages in `text`, whose lines all end in "\n", and returns the rest.
  function handleMessages(text) {
    var messages = text.split("\n\n");
    var rest = messages.pop();
    messages.forEach(handleMessage);
    return rest;
  }

  // Lines end in "\n", "\r\n" or a bare "\r"; each chunk's endings are turned into "\n" at once.
  // A chunk that ends in "\r" may hold the first half of a "\r\n", so a "\n" that starts the next
  // chunk is part of that line ending and is dropped. An unfinished message longer than MAX_PENDING
  // is not what our server sends: the stream is cancelled and the reader stays quiet.
  function readEvents(reader) {
    var decoder = new TextDecoder();
    var pending = "";
    var afterCr = false;
    function next() {
      reader.read().then(function (chunk) {
        if (chunk.done) return;
        var text = decoder.decode(chunk.value, { stream: true });
        if (text) { // an empty result is the first bytes of a multi-byte character
          if (afterCr && text.charAt(0) === "\n") text = text.slice(1);
          afterCr = text.slice(-1) === "\r";
        }
        pending = handleMessages(pending + text.replace(/\r\n?/g, "\n"));
        if (pending.length > MAX_PENDING) {
          reader.cancel().catch(function () { /* already closed */ });
          return;
        }
        next(); // not returned, so a stream that lasts all day builds no chain of promises
      }).catch(function () { /* the stream was cut; reconnecting comes with M2.18 */ });
    }
    next();
  }

  function startEvents(url) {
    var token = launchToken();
    if (!token) return;
    fetch(url, {
      headers: { "X-JP-Token": token, "Accept": "text/event-stream" },
      mode: "same-origin",
      cache: "no-store",
      redirect: "manual", // a redirect is never followed (it shows up as !response.ok)
      credentials: "omit"
    }).then(function (response) {
      var type = (response.headers.get("content-type") || "").toLowerCase();
      if (!response.ok || type.indexOf("text/event-stream") !== 0 || !response.body) return;
      readEvents(response.body.getReader());
    }).catch(function () { /* refused or offline: stay quiet */ });
  }

  if (eventsUrl && window.fetch && window.TextDecoder) startEvents(eventsUrl);
})();
