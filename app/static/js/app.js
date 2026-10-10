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
  // The tasks panel's own refetch is not "that last action": it reports inside the panel.
  document.body.addEventListener("htmx:responseError", function (event) {
    var xhr = event.detail && event.detail.xhr;
    if (xhr && xhr.status === 403) {
      showProblem("restarted", true);
    } else if (isTasksPanelFetch(event)) {
      showTasksError();
    } else {
      showProblem("failed", false);
    }
  });

  // No response at all: the app was stopped or the connection dropped.
  document.body.addEventListener("htmx:sendError", function (event) {
    if (isTasksPanelFetch(event)) showTasksError(); else showProblem("offline", false);
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

  // The top-bar task indicator (M2.20): #tasks-toggle is a disclosure button for the non-modal
  // #tasks-popover. Opening moves no focus. The panel closes on Esc (focus goes to the button
  // only when it was on it or in the panel), on a pointer press outside the two, and when
  // keyboard focus moves from them to something else. Only app pages have them.
  //
  // The popover is the one element that fetches the panel: on the body's task events and on
  // "tasks:refresh" from here (see its hx-trigger). Closed, it fetches nothing.
  var tasksToggle = document.getElementById("tasks-toggle");
  var tasksPopover = document.getElementById("tasks-popover");
  var tasksSlot = tasksToggle ? tasksToggle.parentElement : null;
  var TASKS_POLL_MS = 2000;
  var tasksTimer = null;
  var tasksPaused = false; // a request from the panel failed: no timed refetch until one works

  function isTasksPanelFetch(event) {
    return !!tasksPopover && !!event.detail && event.detail.elt === tasksPopover;
  }

  // The message is in the template (spec 3.10); it is said once each time it appears.
  function showTasksError() {
    var line = document.getElementById("tasks-error");
    if (!line || !line.hidden) return;
    line.hidden = false;
    announce(line.textContent.trim());
  }

  function hideTasksError() {
    var line = document.getElementById("tasks-error");
    if (line) line.hidden = true;
  }

  function refreshTasksPanel() {
    tasksPopover.dispatchEvent(new CustomEvent("tasks:refresh"));
  }

  // Progress is saved without an event, so the open panel asks again every two seconds while
  // it shows a task that is queued or running (those have a progress bar). An idle or closed
  // panel, a hidden tab and a panel whose last request failed ask nothing.
  function pollTasksPanel() {
    if (tasksPaused || document.visibilityState === "hidden") return;
    if (tasksPopover.querySelector("progress")) refreshTasksPanel();
  }

  // A panel with no control in it is a Tab stop itself, so its list can be scrolled by
  // keyboard; with one, Tab goes from the button straight to the first control.
  function syncTasksTabStop() {
    if (tasksPopover.querySelector("button, a[href]")) {
      tasksPopover.removeAttribute("tabindex");
    } else {
      tasksPopover.setAttribute("tabindex", "0");
    }
  }

  function setTasksOpen(open) {
    if (open === !tasksPopover.hidden) return;
    tasksPopover.hidden = !open;
    tasksToggle.setAttribute("aria-expanded", open ? "true" : "false");
    window.clearInterval(tasksTimer);
    tasksTimer = null;
    if (!open) return;
    tasksPaused = false;
    hideTasksError();
    syncTasksTabStop();
    refreshTasksPanel();
    tasksTimer = window.setInterval(pollTasksPanel, TASKS_POLL_MS);
  }

  if (tasksToggle && tasksPopover && tasksSlot) {
    tasksToggle.addEventListener("click", function () {
      setTasksOpen(tasksToggle.getAttribute("aria-expanded") !== "true");
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" || event.defaultPrevented || tasksPopover.hidden) return;
      var inside = tasksSlot.contains(document.activeElement);
      event.preventDefault();
      setTasksOpen(false);
      if (inside) tasksToggle.focus();
    });
    // Focus that moves to another element closes the panel. Focus that just goes away (its
    // element was replaced, or the window lost focus) names no other element and doesn't.
    tasksSlot.addEventListener("focusout", function (event) {
      var next = event.relatedTarget;
      if (next instanceof Element && !tasksSlot.contains(next)) setTasksOpen(false);
    });
    // Closed, the panel asks for nothing: only the count follows the task events.
    document.body.addEventListener("htmx:beforeRequest", function (event) {
      if (isTasksPanelFetch(event) && tasksPopover.hidden) event.preventDefault();
    });
    function pauseTasksPolling(event) {
      var from = event.detail && event.detail.elt;
      if (from instanceof Element && tasksPopover.contains(from)) tasksPaused = true;
    }
    document.body.addEventListener("htmx:responseError", pauseTasksPolling);
    document.body.addEventListener("htmx:sendError", pauseTasksPolling);

    // A refetched panel that says what the page already shows is not swapped in: replacing
    // a button loses a click or key press that is under way, and for a moment the new one
    // isn't wired up. What is compared is every id and the text; only the progress values
    // may differ, and those are copied into the bars already on the page.
    function tasksSignature(panel) {
      var ids = [];
      panel.querySelectorAll("[id]").forEach(function (element) { ids.push(element.id); });
      var copy = panel.cloneNode(true);
      copy.querySelectorAll("progress").forEach(function (bar) { bar.remove(); });
      return ids.join(" ") + "|" + copy.textContent.replace(/\s+/g, " ").trim();
    }
    document.body.addEventListener("htmx:beforeSwap", function (event) {
      var detail = event.detail || {};
      var shown = detail.target;
      if (!shown || shown.id !== "tasks-panel" || !detail.shouldSwap || detail.isError) return;
      tasksPaused = false;
      hideTasksError();
      if (typeof detail.serverResponse !== "string" || !window.DOMParser) return;
      var parsed = new DOMParser().parseFromString(detail.serverResponse, "text/html");
      var fresh = parsed.getElementById("tasks-panel");
      if (!fresh || tasksSignature(fresh) !== tasksSignature(shown)) return;
      fresh.querySelectorAll("progress[id]").forEach(function (bar) {
        var same = document.getElementById(bar.id);
        if (!same || !shown.contains(same)) return;
        same.setAttribute("value", bar.getAttribute("value") || "0");
        same.textContent = bar.textContent;
      });
      detail.shouldSwap = false;
    });

    // A refresh replaces the panel's content, and an answer to Cancel or Retry replaces a
    // task. Keyboard focus that was inside the panel is never left on <body>: it goes to the
    // new element with the same id, else to what the answer marks with data-autofocus, else
    // to the button. A removed element fires no event, so the last element focused in the
    // panel is remembered until focus goes elsewhere or the user presses outside the panel.
    var tasksFocusId = null; // "" for an element without an id
    document.addEventListener("focusin", function (event) {
      var inside = event.target instanceof Element && tasksPopover.contains(event.target);
      tasksFocusId = inside ? event.target.id : null;
    });
    document.addEventListener("pointerdown", function (event) {
      var target = event.target;
      if (!(target instanceof Element) || !tasksPopover.contains(target)) tasksFocusId = null;
      if (!(target instanceof Node) || !tasksSlot.contains(target)) setTasksOpen(false);
    });
    function restoreTasksFocus() {
      if (tasksFocusId === null || tasksPopover.hidden) return;
      var active = document.activeElement;
      if (active && active !== document.body && document.body.contains(active)) return; // kept
      var next = tasksFocusId ? document.getElementById(tasksFocusId) : null;
      if (!next || !tasksPopover.contains(next)) next = tasksPopover.querySelector("[data-autofocus]");
      if (next) {
        if (next.tabIndex < 0 && !next.hasAttribute("tabindex")) next.setAttribute("tabindex", "-1");
        next.focus();
      }
      if (!next || document.activeElement !== next) tasksToggle.focus();
    }
    // htmx wires up swapped-in content only when the swap settles, 20 ms later; until then a
    // new Cancel or Retry would ignore a click or key press, and focus would wait on <body>.
    // In the panel both happen at once, and again at the settle in case that changed things.
    document.body.addEventListener("htmx:afterSwap", function (event) {
      if (!(event.target instanceof Element) || !tasksPopover.contains(event.target)) return;
      if (window.htmx) window.htmx.process(event.target);
      syncTasksTabStop();
      restoreTasksFocus();
    });
    document.body.addEventListener("htmx:afterSettle", restoreTasksFocus);
  }

  // Finished tasks are announced once (M2.20). Every answer to the indicator's request fires
  // "tasks:finished" with the recently finished tasks: { tasks: [{ key, message }] }, the key
  // being the task's id and attempt and the message a fixed sentence from the server. The
  // keys already dealt with are kept in localStorage (keys only, never a message; the newest
  // 50), so a reload or another tab says nothing again; without storage they are kept for
  // this page. The first answer a page gets is what happened before it was opened: its keys
  // are remembered, and only a task that finished in the last 10 seconds is said (it may have
  // ended while the browser was going from one page to the next). "age" is the server's
  // count of whole seconds since the task finished.
  var ANNOUNCED_KEY = "jobpipeline-tasks-announced";
  var ANNOUNCED_MAX = 50;
  var ANNOUNCE_RECENT_S = 10;
  var announcedHere = [];
  var announcedBaseline = false;

  function storedAnnounced() {
    try {
      var stored = JSON.parse(window.localStorage.getItem(ANNOUNCED_KEY) || "[]");
      if (!Array.isArray(stored)) return [];
      return stored.filter(function (key) { return typeof key === "string"; });
    } catch (error) {
      return []; // no storage, or something else wrote there
    }
  }

  function rememberAnnounced(known, keys) {
    announcedHere = known.concat(keys).filter(function (key, index, all) {
      return all.indexOf(key) === index;
    }).slice(-ANNOUNCED_MAX);
    try {
      window.localStorage.setItem(ANNOUNCED_KEY, JSON.stringify(announcedHere));
    } catch (error) { /* kept for this page only */ }
  }

  document.body.addEventListener("tasks:finished", function (event) {
    var tasks = event.detail && event.detail.tasks;
    if (!Array.isArray(tasks)) return;
    var known = storedAnnounced().concat(announcedHere);
    var fresh = tasks.filter(function (task) {
      return task && typeof task.key === "string" && typeof task.message === "string" &&
        known.indexOf(task.key) < 0;
    }).reverse(); // the server lists the newest first; say them in the order they ended
    var first = !announcedBaseline;
    announcedBaseline = true;
    if (!fresh.length) return;
    rememberAnnounced(known, fresh.map(function (task) { return task.key; }));
    var said = fresh.filter(function (task) {
      return !first || (typeof task.age === "number" && task.age <= ANNOUNCE_RECENT_S);
    });
    if (said.length) announce(said.map(function (task) { return task.message; }).join(" "));
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
