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

  // Live updates (ADR-076 item 1, ADR-077): the server streams events at the URL in
  // <body data-events-url> (only pages that opt in have one; the setup guide doesn't).
  // EventSource can't send headers, so the stream is read with fetch and the launch token goes
  // in X-JP-Token like every other request, never in the URL. Each "resync" event becomes a
  // "tasks:resync" event on the body and each "task-updated" a "tasks:updated" event, both
  // without detail: changes can arrive out of order, so listeners refetch what they show.
  //
  // - A stream that ends or can't be opened is tried again after 1, 2, 4, 8, 16 and then every
  //   30 seconds; a successful connect starts that over.
  // - A 403 means the app restarted: the reader stops and says so. Any other answer that isn't
  //   an event stream stops it quietly.
  // - A hidden tab holds no stream; it connects at once when it is shown again.
  // - One stream per browser: the tab that holds a Web Lock (the leader) reads the stream and
  //   passes each event on over a BroadcastChannel; the other tabs (followers) wait for the
  //   lock. Browsers without either API read one stream per visible tab.
  var eventsUrl = document.body.getAttribute("data-events-url");
  var MAX_PENDING = 64 * 1024; // the longest unfinished message the reader will keep
  var RETRY_MS = [1000, 2000, 4000, 8000, 16000, 30000];
  var SHARED_NAME = "jobpipeline-task-events"; // the lock and the channel
  var EVENT_NAMES = { "resync": "tasks:resync", "task-updated": "tasks:updated" };

  var stopped = false;    // for good, until the page is reloaded
  var current = null;     // the attempt or open stream: { controller, reader }
  var retryTimer = null;
  var failures = 0;       // attempts since the last successful connect
  var channel = null;     // set when tabs share one stream
  var lockState = "none"; // "asking", "waiting" (a follower) or "held" (the leader)
  var releaseLock = null;

  function launchToken() {
    try {
      var value = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-JP-Token"];
      return typeof value === "string" ? value : "";
    } catch (error) {
      return "";
    }
  }

  function canRead() {
    return !stopped && document.visibilityState !== "hidden";
  }

  function dispatch(name) {
    document.body.dispatchEvent(new CustomEvent(name, { bubbles: true }));
  }

  // One message is the lines between blank lines: ": comment" lines and fields such as
  // "event: resync" and "data: {}". Only the event name matters here; the data is not read.
  function handleMessage(message) {
    var name = "";
    message.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) name = line.slice(6).replace(/^ /, "");
    });
    if (!Object.prototype.hasOwnProperty.call(EVENT_NAMES, name)) return;
    dispatch(EVENT_NAMES[name]);
    if (channel) channel.postMessage(EVENT_NAMES[name]); // not delivered to this tab itself
  }

  // Handles the complete messages in `text`, whose lines all end in "\n", and returns the rest.
  function handleMessages(text) {
    var messages = text.split("\n\n");
    var rest = messages.pop();
    messages.forEach(handleMessage);
    return rest;
  }

  // Closes the stream or the attempt, if any, and forgets a planned retry.
  function closeStream() {
    var attempt = current;
    current = null;
    window.clearTimeout(retryTimer);
    retryTimer = null;
    if (!attempt) return;
    if (attempt.controller) attempt.controller.abort();
    if (attempt.reader) attempt.reader.cancel().catch(function () { /* already closed */ });
  }

  // The leader gives the lock up, so that another tab can read the stream.
  function giveUpLock() {
    var release = releaseLock;
    if (!release) return;
    releaseLock = null;
    lockState = "none";
    release();
  }

  // Stops for good. Another tab that takes the lock asks the server itself.
  function stopEvents(problem) {
    stopped = true;
    closeStream();
    giveUpLock();
    if (problem) showProblem(problem, true);
  }

  // The stream ended or could not be opened: try again later.
  function lost(attempt) {
    if (current !== attempt) return; // closed on purpose
    current = null;
    var delay = RETRY_MS[Math.min(failures, RETRY_MS.length - 1)];
    failures += 1;
    retryTimer = window.setTimeout(connect, delay);
  }

  // Lines end in "\n", "\r\n" or a bare "\r"; each chunk's endings are turned into "\n" at once.
  // A chunk that ends in "\r" may hold the first half of a "\r\n", so a "\n" that starts the next
  // chunk is part of that line ending and is dropped. An unfinished message longer than MAX_PENDING
  // is not what our server sends: the reader stops for good and stays quiet.
  function readEvents(attempt) {
    var decoder = new TextDecoder();
    var pending = "";
    var afterCr = false;
    function next() {
      attempt.reader.read().then(function (chunk) {
        if (current !== attempt) return;
        if (chunk.done) { lost(attempt); return; }
        var text = decoder.decode(chunk.value, { stream: true });
        if (text) { // an empty result is the first bytes of a multi-byte character
          if (afterCr && text.charAt(0) === "\n") text = text.slice(1);
          afterCr = text.slice(-1) === "\r";
        }
        pending = handleMessages(pending + text.replace(/\r\n?/g, "\n"));
        if (pending.length > MAX_PENDING) { stopEvents(null); return; }
        next(); // not returned, so a stream that lasts all day builds no chain of promises
      }).catch(function () { lost(attempt); });
    }
    next();
  }

  // Opens the stream unless one is open or being opened. The token is read for every attempt.
  function connect() {
    if (current || !canRead()) return;
    window.clearTimeout(retryTimer);
    retryTimer = null;
    var token = launchToken();
    if (!token) { stopEvents(null); return; }
    var attempt = { controller: null, reader: null };
    if (window.AbortController) attempt.controller = new AbortController();
    var options = {
      headers: { "X-JP-Token": token, "Accept": "text/event-stream" },
      mode: "same-origin",
      cache: "no-store",
      redirect: "manual", // a redirect is never followed (it shows up as !response.ok)
      credentials: "omit"
    };
    if (attempt.controller) options.signal = attempt.controller.signal;
    current = attempt;
    fetch(eventsUrl, options).then(function (response) {
      if (current !== attempt) { // closed while the answer was on its way
        if (response.body) response.body.cancel().catch(function () { /* already closed */ });
        return;
      }
      var type = (response.headers.get("content-type") || "").toLowerCase();
      if (!response.ok || type.indexOf("text/event-stream") !== 0 || !response.body) {
        current = null; // the answer is left alone: not read, and not aborted either
        // A 403 means this page holds an old launch token: the app restarted.
        stopEvents(response.status === 403 ? "restarted" : null);
        return;
      }
      failures = 0;
      attempt.reader = response.body.getReader();
      readEvents(attempt);
    }).catch(function () { lost(attempt); }); // no answer: the app is down or the network is
  }

  // This tab has the lock: it reads the stream until it is hidden, stops or is closed.
  function lead() {
    if (!canRead()) { // hidden or stopped while it waited: the next tab's turn
      lockState = "none";
      return undefined;
    }
    lockState = "held";
    connect();
    if (stopped) { // it could not even ask (no token): the next tab's turn
      lockState = "none";
      return undefined;
    }
    return new Promise(function (resolve) { releaseLock = resolve; });
  }

  // Another tab reads the stream. This one refetches once, because it saw none of the events
  // so far, then gets them over the channel and waits in line for the lock.
  function follow() {
    lockState = "none";
    if (!canRead()) return;
    dispatch("tasks:resync");
    lockState = "waiting";
    navigator.locks.request(SHARED_NAME, lead).catch(readAlone);
  }

  // Web Locks refused to work: this tab reads its own stream.
  function readAlone() {
    lockState = "none";
    releaseLock = null;
    if (channel) channel.close();
    channel = null;
    connect();
  }

  // Called when the page loads and whenever the tab is shown.
  function startEvents() {
    if (!canRead()) return;
    failures = 0;
    if (!channel) { connect(); return; }
    if (lockState === "waiting") dispatch("tasks:resync"); // a follower again: it missed events
    if (lockState !== "none") return;
    lockState = "asking";
    navigator.locks.request(SHARED_NAME, { ifAvailable: true }, function (lock) {
      return lock ? lead() : follow();
    }).catch(readAlone);
  }

  function pauseEvents() {
    closeStream();
    giveUpLock(); // a hidden leader would keep the events from the tab the user looks at
  }

  if (eventsUrl && window.fetch && window.TextDecoder) {
    if (navigator.locks && navigator.locks.request && window.BroadcastChannel) {
      channel = new BroadcastChannel(SHARED_NAME);
      channel.addEventListener("message", function (event) {
        // The leader has dispatched it already; a hidden tab refetches when it is shown; a
        // stopped tab (it may show the restart message) is done until it is reloaded.
        if (lockState === "held" || !canRead()) return;
        if (event.data === "tasks:resync" || event.data === "tasks:updated") dispatch(event.data);
      });
    }
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") pauseEvents(); else startEvents();
    });
    startEvents();
  }
})();
