// Shell behaviour shared by every page (htmx-ui skill). No build step; plain browser JS.
// User-facing messages live in the templates (spec 3.10); this script only places them.
(function () {
  "use strict";

  var status = document.getElementById("app-status");

  function announce(message, shown) {
    var region = document.getElementById("announcer");
    if (!region) return;
    // Clear first so a repeated message is announced again.
    region.textContent = "";
    window.setTimeout(function () {
      region.textContent = message;
      if (shown) shown();
    }, 50);
  }

  // A summary owns its exact attempt's completion words. Keep them in the persistent
  // announcer, once, and give the initiating save/start words a moment to be heard.
  var SUMMARY_ANNOUNCED = "jobpipeline-summary-announced";
  var summarySeen = storedKeys(SUMMARY_ANNOUNCED);
  var summaryActionAt = 0;
  function summaryFinished(initial) {
    document.querySelectorAll("[data-summary-finished-key]").forEach(function (section) {
      var key = section.getAttribute("data-summary-finished-key");
      var message = section.getAttribute("data-summary-finished-message");
      if (!key || !message || summarySeen.indexOf(key) >= 0) return;
      summarySeen = storeKeys(SUMMARY_ANNOUNCED, summarySeen.concat([key]).slice(-50));
      if (initial) return;
      var id = section.id;
      function sayWhenReady() {
        var current = document.getElementById(id);
        if (!current || current.getAttribute("data-summary-finished-key") !== key) return;
        var delay = 1000 - (window.performance.now() - summaryActionAt);
        if (delay > 0) window.setTimeout(sayWhenReady, delay);
        else announce(message);
      }
      sayWhenReady();
    });
  }
  summaryFinished(true);
  document.body.addEventListener("htmx:afterSwap", function () { summaryFinished(false); });

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

  // A part of a form shown only while one choice of a radio group is made
  // (data-shown-when="<name>=<value>": the merge screen's own wording, M3.6). The server
  // renders it hidden unless that choice is checked; choosing a radio of the group shows or
  // hides it at once. Focus stays on the radio. Without scripts CSS keeps it in sight.
  document.addEventListener("change", function (event) {
    var radio = event.target;
    if (!(radio instanceof HTMLInputElement) || radio.type !== "radio" || !radio.form) return;
    var parts = radio.form.querySelectorAll("[data-shown-when]");
    for (var i = 0; i < parts.length; i++) {
      var rule = parts[i].getAttribute("data-shown-when") || "";
      var at = rule.indexOf("=");
      if (at < 0 || rule.slice(0, at) !== radio.name) continue;
      parts[i].hidden = !(radio.checked && radio.value === rule.slice(at + 1));
    }
  });

  // Routes can ask for an announcement with the header HX-Trigger: {"announce": "..."}.
  document.body.addEventListener("announce", function (event) {
    if (!event.detail || typeof event.detail.value !== "string") return;
    var summaryOwner = event.target instanceof Element &&
      event.target.closest("#job-intake, [id^='job-summary-']");
    if (summaryOwner) summaryActionAt = window.performance.now() + 50;
    announce(event.detail.value, summaryOwner ? function () {
      summaryActionAt = window.performance.now();
    } : null);
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

  // A link to a section of the page it is on (a[data-section-link], such as "Open Usage
  // budgets" on Settings) scrolls there but leaves keyboard focus behind. Focus goes to the
  // section's heading (the element its aria-labelledby names), else to the section.
  function sectionHeading(link) {
    if (!link.hash || link.pathname !== window.location.pathname) return null;
    var section = document.getElementById(link.hash.slice(1));
    if (!section) return null;
    var labelledBy = section.getAttribute("aria-labelledby");
    return (labelledBy && document.getElementById(labelledBy)) || section;
  }

  function focusQuietly(element) {
    if (element.tabIndex < 0 && !element.hasAttribute("tabindex")) element.setAttribute("tabindex", "-1");
    element.focus();
  }

  // Arriving on a page at a section marked data-hash-focus (an "Add event" link from the Board
  // or a job) puts keyboard focus on that section's heading, as the in-page link does.
  function focusHashSection() {
    var section = window.location.hash ? document.getElementById(window.location.hash.slice(1)) : null;
    if (!section || !section.hasAttribute("data-hash-focus")) return;
    var labelledBy = section.getAttribute("aria-labelledby");
    focusQuietly((labelledBy && document.getElementById(labelledBy)) || section);
  }
  // After the load: the browser's own jump to the fragment would otherwise take focus back.
  function focusHashSectionSoon() { window.setTimeout(focusHashSection, 0); }
  if (document.readyState === "complete") focusHashSectionSoon();
  else window.addEventListener("load", focusHashSectionSoon);

  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    var link = target ? target.closest("a[data-section-link]") : null;
    if (!link || link.closest("dialog[data-modal]")) return; // a dialog's link: when it closes
    var heading = sectionHeading(link);
    // After the browser has gone to the section, which drops focus.
    if (heading) window.setTimeout(function () { focusQuietly(heading); }, 0);
  });

  // Modal questions (M2.24: the budget warning). A fragment holds <dialog data-modal>; once
  // its swap settles it is opened with showModal(), which moves focus to its [autofocus]
  // control ("Not now"), makes the rest of the page inert and closes on Esc. Tab and
  // Shift+Tab wrap inside it. "Not now" ([data-modal-close]) and Esc only close it: nothing
  // is sent. A closed dialog is removed, and focus goes to the control its data-return-focus
  // selects (the action that asked); when a link in it was followed, to the heading of the
  // section the link names if that is on this page. A confirmed start replaces the dialog
  // with the answer, and until that answer is here Esc and "Not now" do nothing; an answer
  // that can't be shown closes it, so the problem is visible.
  var MODAL_STOPS = "a[href], button:not([disabled]), select:not([disabled]), " +
    "textarea:not([disabled]), input:not([disabled]):not([type='hidden']), " +
    "[tabindex]:not([tabindex='-1'])";
  var modalLeavingTo = null; // where focus goes after a link in the dialog was followed

  function openModal() {
    return document.querySelector("dialog[data-modal][open]");
  }

  // htmx marks the form whose request is on its way.
  function modalBusy(dialog) {
    return dialog.querySelector(".htmx-request") !== null;
  }

  document.body.addEventListener("htmx:afterSettle", function () {
    if (openModal()) return;
    var dialog = document.querySelector("dialog[data-modal]");
    if (dialog && typeof dialog.showModal === "function") dialog.showModal();
  });

  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    var dialog = target ? target.closest("dialog[data-modal]") : null;
    if (!dialog || !dialog.open) return;
    var link = target.closest("a[href]");
    if (link) {
      dialog.setAttribute("data-leaving", "");
      modalLeavingTo = sectionHeading(link);
      dialog.close();
    } else if (target.closest("[data-modal-close]") && !modalBusy(dialog)) {
      dialog.close();
    }
  });

  // Esc while the confirmed start is on its way: the dialog stays until the answer is here.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var dialog = openModal();
    if (dialog && modalBusy(dialog)) event.preventDefault();
  }, true);
  // "cancel" doesn't bubble either.
  document.addEventListener("cancel", function (event) {
    var dialog = event.target;
    if (!(dialog instanceof Element) || !dialog.matches("dialog[data-modal]")) return;
    if (modalBusy(dialog)) event.preventDefault();
  }, true);

  // "close" doesn't bubble, so it is caught on its way down.
  document.addEventListener("close", function (event) {
    var dialog = event.target;
    if (!(dialog instanceof Element) || !dialog.matches("dialog[data-modal]")) return;
    var leaving = dialog.hasAttribute("data-leaving");
    var selector = dialog.getAttribute("data-return-focus");
    var next = leaving ? modalLeavingTo : (selector ? document.querySelector(selector) : null);
    modalLeavingTo = null;
    dialog.remove();
    if (next) focusQuietly(next);
  }, true);

  // A part of the page that fetches itself again (Testing tools, when a budget changes) is
  // not replaced while it holds an open modal: the question on screen would vanish.
  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var detail = event.detail || {};
    var from = detail.elt;
    if (!(from instanceof Element) || from !== detail.target) return;
    if (from.querySelector("dialog[data-modal][open]")) event.preventDefault();
  });

  // A part of the page where the user edits in place ([data-keep-edits]: the Fact bank's
  // drafts, M3.5) and that fetches itself again when its content changed elsewhere is never
  // replaced while the user is at work in it: an inline form is open (<details open>), focus
  // is inside, or something in it would be lost for good ([data-holds-refresh]: the Undo of a
  // discarded draft, which the refreshed list no longer has). Typing is never lost to a
  // refresh. The refresh is dropped, both before
  // it is asked for and, when work began while the answer was on its way, before it is
  // swapped in; the part's own note ([data-stale-note], its words are in the template) is
  // shown and said once instead. Answers to the forms inside target their own block, so
  // they are never held back.
  function keepsEdits(region, requester) {
    if (!(region instanceof Element) || region !== requester) return false;
    if (!region.hasAttribute("data-keep-edits")) return false;
    var active = document.activeElement;
    return region.querySelector("details[open], [data-holds-refresh]") !== null ||
      (active instanceof Element && active !== region && region.contains(active));
  }
  function showStaleNote(region) {
    var note = region.querySelector("[data-stale-note]");
    if (!note || !note.hidden) return;
    note.hidden = false;
    announce(note.textContent.replace(/\s+/g, " ").trim());
  }
  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var detail = event.detail || {};
    if (!keepsEdits(detail.target, detail.elt)) return;
    event.preventDefault();
    showStaleNote(detail.target);
  });
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail || {};
    var requester = detail.requestConfig ? detail.requestConfig.elt : null;
    if (!detail.shouldSwap || !keepsEdits(detail.target, requester)) return;
    detail.shouldSwap = false;
    showStaleNote(detail.target);
  });

  // A form that confirms several drafts together ([data-skips-open-edits]: the Fact bank's
  // "Confirm all from this import") never confirms one the user is rewording. Each of its
  // fields that would confirm a draft names that draft's block (data-confirms); when the
  // block's inline form is open with wording that differs from the saved one, the field is
  // left out of the request, so the server leaves that draft as it is.
  function editedInPlace(block) {
    if (!block) return false;
    var fields = block.querySelectorAll("details[open] textarea");
    for (var i = 0; i < fields.length; i += 1) {
      var saved = fields[i].getAttribute("data-saved");
      if (fields[i].value !== (saved === null ? fields[i].defaultValue : saved)) return true;
    }
    return false;
  }
  document.body.addEventListener("htmx:configRequest", function (event) {
    var detail = event.detail || {};
    var form = detail.elt;
    if (!(form instanceof Element) || !form.hasAttribute("data-skips-open-edits")) return;
    form.querySelectorAll("input[data-confirms]").forEach(function (input) {
      if (!editedInPlace(document.getElementById(input.getAttribute("data-confirms")))) return;
      if (detail.formData && typeof detail.formData.delete === "function") {
        detail.formData.delete(input.name);
      }
      if (detail.parameters) delete detail.parameters[input.name];
    });
  });

  // A refused upload answers 400 or 413, which htmx treats as an error and doesn't show. A
  // part of the page that shows its own refusals ([data-shows-refusals]: the CV import
  // section) gets such an answer swapped in when it is HTML with something in it, so the
  // reason appears with the form; an empty or plain-text refusal (the server's backstop)
  // stays an error and the section stays as it is.
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail || {};
    var xhr = detail.xhr;
    var target = detail.target;
    if (!xhr || (xhr.status !== 400 && xhr.status !== 413)) return;
    if (!(target instanceof Element) || !target.hasAttribute("data-shows-refusals")) return;
    var type = (xhr.getResponseHeader("content-type") || "").toLowerCase();
    if (type.indexOf("text/html") !== 0 || !(xhr.responseText || "").trim()) return;
    detail.shouldSwap = true;
    detail.isError = false;
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Tab") return;
    var dialog = openModal();
    if (!dialog) return;
    var stops = dialog.querySelectorAll(MODAL_STOPS);
    var first = stops[0];
    var last = stops[stops.length - 1];
    var active = document.activeElement;
    var outside = !dialog.contains(active);
    if (!first) {
      event.preventDefault();
    } else if (event.shiftKey ? (outside || active === first) : (outside || active === last)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    }
  });

  function closeModalAfterFailure(event) {
    var from = event.detail && event.detail.elt;
    var dialog = openModal();
    if (dialog && from instanceof Element && dialog.contains(from)) dialog.close();
  }
  document.body.addEventListener("htmx:responseError", closeModalAfterFailure);
  document.body.addEventListener("htmx:sendError", closeModalAfterFailure);

  // A control with aria-disabled="true" (an action a budget blocks) can take focus, so its
  // reason is read, but does nothing: no click (Enter and Space on a button, and Enter in a
  // field of its form, arrive as clicks on it) and no submit of the form it would submit.
  // Pressing it says the reason again (the text its aria-describedby names), so the press
  // isn't met with silence. The server refuses the request as well.
  function blockedControl(element) {
    return element instanceof Element ? element.closest("[aria-disabled='true']") : null;
  }
  function sayWhyBlocked(control) {
    var ids = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    var reason = ids.map(function (id) {
      var element = document.getElementById(id);
      return element ? element.textContent.replace(/\s+/g, " ").trim() : "";
    }).filter(Boolean).join(" ");
    if (reason) announce(reason);
  }
  function refuseBlocked(event, control) {
    if (!control) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    sayWhyBlocked(control);
  }
  document.addEventListener("click", function (event) {
    refuseBlocked(event, blockedControl(event.target));
  }, true);
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var submitter = event.submitter ||
      form.querySelector("button[type='submit'], button:not([type]), input[type='submit']");
    refuseBlocked(event, blockedControl(submitter));
  }, true);

  // Refine with AI (M3.9). "Edit it myself" ([data-focus-field], a button) takes focus to
  // the editor's wording field and leaves the conversation as it is.
  document.addEventListener("click", function (event) {
    var control = event.target instanceof Element ? event.target.closest("[data-focus-field]") : null;
    if (!control) return;
    var field = document.getElementById(control.getAttribute("data-focus-field") || "");
    if (!field) return;
    event.preventDefault();
    field.focus();
  });

  // When the panel is replaced (its turn ended) while focus was in it, on a control the new
  // panel no longer has (Cancel), focus goes to its status line, which says what happened.
  // Focus elsewhere on the page is never taken. A refetch that changes nothing answers 204
  // and swaps nothing, so focus stays where it was. htmx fires beforeSwap for that 204 too
  // (shouldSwap false) with no settle after it, so the mark is set only for a swap of the
  // panel that happens (a 204 clears it), and only the panel's own settle reads and clears
  // it: another part's swap (an editor save, the tasks indicator), even one that settles
  // between the panel's swap and its settle, neither moves focus here nor drops the mark.
  var refineHadFocus = false;
  function isRefinePanel(element) {
    return element instanceof Element && element.id === "fact-refine";
  }
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail || {};
    if (!isRefinePanel(detail.target)) return;
    var active = document.activeElement;
    refineHadFocus = detail.shouldSwap === true && active instanceof Element &&
      detail.target.contains(active);
  });
  document.body.addEventListener("htmx:afterSettle", function (event) {
    var detail = event.detail || {};
    if (!isRefinePanel(detail.elt) && !isRefinePanel(detail.target)) return;
    var hadFocus = refineHadFocus;
    refineHadFocus = false;
    if (!hadFocus) return;
    var panel = document.getElementById("fact-refine");
    if (!panel || panel.contains(document.activeElement)) return;
    var line = document.getElementById("fact-refine-status") ||
      document.getElementById("fact-refine-heading");
    if (!line) return;
    if (!line.hasAttribute("tabindex")) line.setAttribute("tabindex", "-1");
    line.focus();
  });

  // Job-analysis refreshes can replace a focused start button, modal, or heading. Keep
  // focus by id, falling back to that panel's heading; never take it from elsewhere.
  // Keep the intent across an immediate refetch that arrives before the start swap settles.
  var jobFocus = Object.create(null);
  function isJobPanel(element) {
    return element instanceof Element && element.hasAttribute("data-job-analysis");
  }
  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var detail = event.detail || {};
    var panel = detail.target;
    if (!isJobPanel(panel) || !detail.requestConfig || detail.requestConfig.verb !== "post") return;
    // Disabling a submitting button blurs it before beforeSwap, especially on phone layouts.
    var heading = panel.querySelector("h2[id]");
    if (heading) jobFocus[panel.id] = heading.id;
  });
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail || {};
    var panel = detail.target;
    if (!isJobPanel(panel) || detail.shouldSwap !== true) return;
    var active = document.activeElement;
    if (active instanceof Element && panel.contains(active)) {
      var heading = panel.querySelector("h2[id]");
      jobFocus[panel.id] = active.id || (heading ? heading.id : "");
    } else if (active && active !== document.body) delete jobFocus[panel.id];
  });
  document.body.addEventListener("htmx:afterSettle", function (event) {
    var detail = event.detail || {};
    var old = isJobPanel(detail.target) ? detail.target : detail.elt;
    if (!isJobPanel(old)) return;
    var panel = document.getElementById(old.id);
    if (!panel) return;
    var target = null;
    Object.keys(jobFocus).forEach(function (id) {
      var region = document.getElementById(id);
      if (!region || (panel !== region && !panel.contains(region))) return;
      target = document.getElementById(jobFocus[id]) || region.querySelector("h2[id]");
      delete jobFocus[id];
    });
    if (target && panel.contains(target) && document.activeElement === document.body) target.focus();
  });

  // Its Accept (form[data-refine-accept]) is blocked (aria-disabled, its reason shown) while
  // a claim that isn't in the fact bank is neither ticked nor taken out of the wording; a
  // form for a suggestion made before the fact changed (data-refine-stale) stays blocked as
  // the server rendered it. A claim counts as taken out as the server's details_still_said
  // reads one: its case-folded, space-collapsed text is no longer a substring of the
  // wording's, and it could be (data-edits-out="true"; a detail the model reported that the
  // suggestion didn't write as reported can't), and the wording isn't the one the server
  // last refused with it (data-refused-for). The substring test is the looser one, so the
  // client opens Accept only where that text is gone; numbers the server reads by value
  // (written as words, say) can still be refused, and the refusal lists them with
  // data-refused-for, which keeps Accept blocked until the wording changes again.
  function folded(text) {
    return text.toLowerCase().replace(/ß/g, "ss").replace(/ς/g, "σ")
      .replace(/\s+/g, " ").trim();
  }
  function syncRefineAccept(form) {
    if (form.hasAttribute("data-refine-stale")) return;
    var button = form.querySelector("[data-refine-accept-button]");
    var reason = document.getElementById("fact-refine-accept-reason");
    if (!button || !reason) return;
    var parts = [];
    form.querySelectorAll("textarea[data-refine-wording]").forEach(function (field) {
      if (field.value !== "") parts.push(field.value);
    });
    var wording = folded(parts.join("\n"));
    var blocked = false;
    form.querySelectorAll("input[data-refine-claim]").forEach(function (box) {
      if (box.checked) return;
      var refusedFor = box.getAttribute("data-refused-for");
      if (box.getAttribute("data-edits-out") !== "true" ||
          wording.indexOf(folded(box.value)) >= 0 ||
          (refusedFor !== null && folded(refusedFor) === wording)) {
        blocked = true;
      }
    });
    reason.hidden = !blocked;
    if (blocked) {
      button.setAttribute("aria-disabled", "true");
      button.setAttribute("aria-describedby", reason.id);
    } else {
      button.removeAttribute("aria-disabled");
      button.removeAttribute("aria-describedby");
    }
  }
  function syncRefineForms() {
    document.querySelectorAll("form[data-refine-accept]").forEach(syncRefineAccept);
  }
  function refineFormOf(event) {
    var target = event.target;
    return target instanceof Element ? target.closest("form[data-refine-accept]") : null;
  }
  ["input", "change"].forEach(function (name) {
    document.addEventListener(name, function (event) {
      var form = refineFormOf(event);
      if (form) syncRefineAccept(form);
    });
  });
  syncRefineForms();
  document.body.addEventListener("htmx:afterSettle", syncRefineForms);

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
  var TASKS_IDLE_POLLS = 30; // every 30th tick, a minute, an idle panel asks too
  var tasksTimer = null;
  var tasksTicks = 0;
  var tasksPaused = false; // a request from the panel failed: no timed refetch until one works

  // Lists of task keys ("<id>:<attempt>") kept in localStorage: keys only, never a message.
  // Without storage, or when something else wrote there, the list is empty.
  function storedKeys(name) {
    try {
      var stored = JSON.parse(window.localStorage.getItem(name) || "[]");
      if (!Array.isArray(stored)) return [];
      return stored.filter(function (key) { return typeof key === "string"; });
    } catch (error) {
      return [];
    }
  }

  // `keys`, each once; stored when storage works, and returned.
  function storeKeys(name, keys) {
    var kept = keys.filter(function (key, index, all) { return all.indexOf(key) === index; });
    try {
      window.localStorage.setItem(name, JSON.stringify(kept));
    } catch (error) { /* kept for this page only */ }
    return kept;
  }

  // "1 needs attention" (M2.26): the closed button counts the failed or interrupted tasks
  // that haven't been looked at. The server lists the recent ones as keys in the count's
  // data-attention, and on each such task's row in the panel. Looking at a task is having
  // its row in the open panel: the keys of the rows shown are remembered when the panel
  // opens and after each time its content is replaced, so a panel that couldn't load its
  // tasks marks nothing. The keys looked at are kept in localStorage (keys only; for this
  // page only without storage), so the words stay away after a reload and go away in other
  // tabs. Only keys the server still lists (in the count or in the panel) are kept: the
  // list is exact and never longer than what the server lists. The sentences are in the
  // template (spec 3.10).
  var SEEN_KEY = "jobpipeline-tasks-seen";
  var seenHere = [];
  var tasksAttention = document.getElementById("tasks-attention");
  var tasksAttentionGap = document.getElementById("tasks-attention-gap"); // ", " before it

  function attentionKeys(element) {
    var value = element ? element.getAttribute("data-attention") : null;
    return value ? value.split(/\s+/).filter(Boolean) : [];
  }

  function listedKeys() {
    return attentionKeys(document.getElementById("tasks-count"));
  }

  function shownKeys() {
    var keys = [];
    tasksPopover.querySelectorAll("[data-attention]").forEach(function (item) {
      keys = keys.concat(attentionKeys(item));
    });
    return keys;
  }

  function seenKeys() {
    return storedKeys(SEEN_KEY).concat(seenHere);
  }

  function lookAtTasks() {
    var shown = shownKeys();
    var known = seenKeys();
    var listed = listedKeys().concat(shown);
    var kept = known.concat(shown).filter(function (key, index, all) {
      return listed.indexOf(key) >= 0 && all.indexOf(key) === index;
    });
    // Also when nothing new is shown: a key the server stopped listing goes, whichever of
    // the count and the panel was refreshed first. Nothing is written when nothing changes.
    var same = kept.every(function (key) { return known.indexOf(key) >= 0; }) &&
      known.every(function (key) { return kept.indexOf(key) >= 0; });
    if (same) return;
    seenHere = storeKeys(SEEN_KEY, kept);
  }

  function showAttention() {
    if (!tasksAttention || !tasksToggle || !tasksPopover) return;
    if (!tasksPopover.hidden) lookAtTasks();
    var seen = seenKeys();
    var count = listedKeys().filter(function (key) { return seen.indexOf(key) < 0; }).length;
    var words = "";
    if (count === 1) words = tasksAttention.getAttribute("data-one") || "";
    if (count > 1) {
      words = (tasksAttention.getAttribute("data-many") || "").replace("{n}", String(count));
    }
    if (tasksAttention.textContent !== words) tasksAttention.textContent = words;
    tasksAttention.hidden = !words;
    if (tasksAttentionGap) tasksAttentionGap.hidden = !words;
    tasksToggle.classList.toggle("has-attention", !!words); // narrow screens: CSS shows it alone
  }

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
  // it shows a task that is queued or running (those have a progress bar). An idle open
  // panel asks once a minute, so "5 minutes ago" moves on. A closed panel, a hidden tab and
  // a panel whose last request failed ask nothing.
  function pollTasksPanel() {
    if (tasksPaused || document.visibilityState === "hidden") return;
    tasksTicks += 1;
    var minute = tasksTicks % TASKS_IDLE_POLLS === 0;
    if (minute || tasksPopover.querySelector("progress")) refreshTasksPanel();
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
    showAttention(); // the rows already in the panel are looked at; a refresh brings the rest
    if (!open) return;
    tasksPaused = false;
    tasksTicks = 0;
    hideTasksError();
    syncTasksTabStop();
    refreshTasksPanel();
    tasksTimer = window.setInterval(pollTasksPanel, TASKS_POLL_MS);
  }

  if (tasksToggle && tasksPopover && tasksSlot) {
    showAttention();
    // A new count from the server, and keys looked at in another tab.
    document.body.addEventListener("htmx:afterSwap", function (event) {
      if (event.target instanceof Element && event.target.id === "tasks-count") showAttention();
    });
    window.addEventListener("storage", function (event) {
      if (event.key === SEEN_KEY) showAttention();
    });
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
    // and the relative times ("5 minutes ago") may differ, and those are copied into the
    // elements already on the page. A task's data-attention key (its attempt: a retry that
    // failed the same way is another task to look at) and a time's exact moment count too.
    function tasksSignature(panel) {
      var ids = [];
      panel.querySelectorAll("[id]").forEach(function (element) {
        ids.push([
          element.id,
          element.getAttribute("data-attention") || "",
          element.getAttribute("datetime") || ""
        ].join("@"));
      });
      var copy = panel.cloneNode(true);
      copy.querySelectorAll("progress, time").forEach(function (part) { part.remove(); });
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
      fresh.querySelectorAll("time[id]").forEach(function (time) {
        var same = document.getElementById(time.id);
        if (same && shown.contains(same)) same.textContent = time.textContent;
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
      showAttention(); // the rows the open panel now shows have been looked at
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
  // A page that shows a failure's reason itself says so in its own words: an element with
  // data-announce-instead-of (the server's sentence) and data-announce-instead (what to say
  // in its place; both written by the server) swaps the one for the other.
  var ANNOUNCED_KEY = "jobpipeline-tasks-announced";
  var ANNOUNCED_MAX = 50;
  var ANNOUNCE_RECENT_S = 10;
  var announcedHere = [];
  var announcedBaseline = false;
  var pendingOwnedRequests = new Set();
  var deferredFinished = [];
  var deferredFinishedReceived = false;

  // A fast turn can finish before its initiating POST has supplied the panel's task key.
  // Wait for that response (or its failure) before deciding which component owns the words.
  document.body.addEventListener("htmx:beforeSend", function (event) {
    var detail = event.detail || {};
    var from = detail.elt;
    if (from instanceof Element && from.closest("#fact-refine, #job-intake, [id^='job-summary-']") &&
        detail.requestConfig && detail.requestConfig.verb === "post") {
      var xhr = detail.xhr;
      pendingOwnedRequests.add(xhr);
      // A save can detach the originating form. Failure events then cannot bubble to the
      // body. loadend belongs to the request itself and follows htmx's onload/swap handler.
      xhr.addEventListener("loadend", function () {
        if (!pendingOwnedRequests.delete(xhr) || pendingOwnedRequests.size) return;
        var tasks = deferredFinished;
        var received = deferredFinishedReceived;
        deferredFinished = [];
        deferredFinishedReceived = false;
        // No reply and a real empty reply have different baseline semantics.
        if (received) finishedTasks(tasks);
      }, { once: true });
    }
  });

  function storedAnnounced() {
    return storedKeys(ANNOUNCED_KEY);
  }

  function rememberAnnounced(known, keys) {
    announcedHere = storeKeys(ANNOUNCED_KEY, known.concat(keys).filter(function (key, index, all) {
      return all.indexOf(key) === index;
    }).slice(-ANNOUNCED_MAX));
  }

  function pageWords(message) {
    var own = document.querySelectorAll("[data-announce-instead-of][data-announce-instead]");
    for (var i = 0; i < own.length; i += 1) {
      if (own[i].getAttribute("data-announce-instead-of") === message) {
        return own[i].getAttribute("data-announce-instead") || message;
      }
    }
    return message;
  }

  function finishedTasks(tasks) {
    if (!Array.isArray(tasks)) return;
    if (pendingOwnedRequests.size) {
      deferredFinishedReceived = true;
      tasks.forEach(function (task) {
        if (task && typeof task.key === "string" &&
            !deferredFinished.some(function (held) { return held.key === task.key; })) {
          deferredFinished.push(task);
        }
      });
      deferredFinished = deferredFinished.slice(-ANNOUNCED_MAX);
      return;
    }
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
      // A panel that follows this exact turn announces its result itself. Mark its key
      // above even when the indicator's answer wins the race with the panel's answer.
      var owned = Array.from(document.querySelectorAll("[data-task-announcement-key]"))
        .some(function (panel) {
          return panel.getAttribute("data-task-announcement-key") === task.key;
        });
      return !owned && (!first || (typeof task.age === "number" && task.age <= ANNOUNCE_RECENT_S));
    });
    if (said.length) announce(said.map(function (task) { return pageWords(task.message); }).join(" "));
  }

  document.body.addEventListener("tasks:finished", function (event) {
    finishedTasks(event.detail && event.detail.tasks);
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

// A calendar download sends the launch token as a header, never in its URL.
(function () {
  "use strict";
  function reveal() { document.querySelectorAll("[data-calendar-download]").forEach(function (button) { button.hidden = false; }); }
  function say(words) { document.body.dispatchEvent(new CustomEvent("announce", {detail: {value: words}})); }
  function feedback(button, words) {
    var status = button.nextElementSibling;
    if (status && status.hasAttribute("data-calendar-status")) status.textContent = words;
    say(words);
  }
  reveal();
  document.body.addEventListener("htmx:afterSettle", reveal);
  document.addEventListener("click", function (event) {
    var button = event.target instanceof Element ? event.target.closest("[data-calendar-download]") : null;
    if (!button || button.disabled) return;
    var path = button.getAttribute("data-calendar-download");
    if (!/^\/applications\/[A-Za-z0-9_-]+\/events\/[A-Za-z0-9_-]+\/ics$/.test(path)) return;
    var headers;
    try { headers = JSON.parse(document.body.getAttribute("hx-headers")); }
    catch (_) { feedback(button, "Calendar file could not be downloaded. Reload the page and try again."); return; }
    button.disabled = true;
    feedback(button, "Preparing calendar file…");
    fetch(path, {headers: headers, credentials: "same-origin", cache: "no-store", redirect: "error"}).then(function (response) {
      if (!response.ok || !(response.headers.get("Content-Type") || "").startsWith("text/calendar")) throw new Error("download");
      return response.blob();
    }).then(function (blob) {
      var url = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = url; link.download = "jobpipeline-event.ics";
      document.body.appendChild(link); link.click(); link.remove();
      window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      feedback(button, "Calendar file downloaded.");
    }).catch(function () { feedback(button, "Calendar file could not be downloaded. Try again."); }).finally(function () { button.disabled = false; });
  });
}());
