// A native modal drawer keeps the queue behind it and lets budget questions open above it.
// Reads never mark a job seen. Closing refreshes the queue only after a change in the drawer.
(function () {
  "use strict";
  var opener = null;
  var changed = false;
  var removedCardAnalysis = null;
  var analysisNext = null;
  var restoreInboxFocus = false;
  var reopenAfterRefresh = false;
  var drafts = Object.create(null); // memory only; no personal text in browser storage

  function drawer() { return document.querySelector("dialog.job-drawer[open]"); }

  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var from = event.detail.elt;
    if (from && from.id === "job-detail-header" && from.contains(document.activeElement)) {
      event.preventDefault();
      return;
    }
    var path = event.detail.pathInfo && event.detail.pathInfo.requestPath;
    if (/^\/inbox\/jobs\/[^/]+\/detail$/.test(path || "")) {
      if (restoreInboxFocus) {
        event.preventDefault(); reopenAfterRefresh = true; return;
      }
      opener = document.activeElement;
      changed = false;
    }
  });

  document.body.addEventListener("htmx:afterSettle", function (event) {
    var target = event.detail && event.detail.target;
    if (restoreInboxFocus && !drawer() && target && target.id === "inbox") {
      var title = document.getElementById("inbox-card-heading") || document.getElementById("inbox-queue-heading");
      if (title) title.focus();
      restoreInboxFocus = false;
      if (reopenAfterRefresh && title) { reopenAfterRefresh = false; title.click(); }
    }
    var panel = document.querySelector("dialog.job-drawer");
    if (panel && !panel.open) {
      var restored = restoreDraft(panel);
      panel.showModal();
      var words = "Job details opened.";
      if (restored) words += " " + panel.querySelector("#job-edit-draft").textContent;
      document.body.dispatchEvent(new CustomEvent("announce", {detail: {value: words}}));
    }
    var remove = document.querySelector("#detail-remove-dialog[data-detail-reopen]");
    if (remove && !remove.open) {
      remove.removeAttribute("data-detail-reopen");
      remove.showModal();
      var invalid = remove.querySelector("[aria-invalid='true']");
      if (invalid) invalid.focus();
    }
  });

  // The card can offer the same summary controls before a first summary exists. Its ids
  // must not coexist with the drawer's controls, or htmx would target the hidden card.
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail;
    var target = detail.target;
    var isRead = detail.requestConfig && detail.requestConfig.verb === "get";
    if (target && target.id === "inbox" && isRead && target.querySelector("dialog[open]")) {
      detail.shouldSwap = false;
      return;
    }
    if (target && target.id === "job-detail-header" && isRead &&
        (target.contains(document.activeElement) || target.querySelector("details[open], dialog[open]"))) {
      detail.shouldSwap = false;
      return;
    }
    if (!detail.target || detail.target.id !== "inbox-details" || !detail.shouldSwap ||
        !detail.xhr || detail.xhr.status !== 200) return;
    var panel = document.querySelector("#inbox-card [data-job-analysis]");
    if (panel) { analysisNext = panel.nextSibling; panel.remove(); removedCardAnalysis = panel; }
  });

  document.body.addEventListener("job:changed", function (event) {
    if (drawer()) changed = true;
    var data = event.detail || {};
    if (data.edited) delete drafts[data.id];
    var detail = document.querySelector("[data-detail-job]");
    if (!detail || detail.getAttribute("data-detail-job") !== data.id) {
      if (data.edited && !drawer() && document.getElementById("inbox")) {
        restoreInboxFocus = true;
        document.body.dispatchEvent(new CustomEvent("inbox:changed", {bubbles: true}));
      }
      return;
    }
    var field = detail.querySelector("#job-edit input[name='version']");
    if (field && data.previous_version && field.value === String(data.previous_version)) {
      field.value = String(data.version);
    }
    if (data.edited) {
      document.body.dispatchEvent(new CustomEvent("job:edited"));
    }
  });
  document.body.addEventListener("tasks:updated", function () { if (drawer()) changed = true; });

  function editForm(panel) { return panel ? panel.querySelector("#job-edit form") : null; }
  function dirty(form) {
    return form && (form.hasAttribute("data-unsaved") || Array.prototype.some.call(form.elements, function (field) {
      return field.name && field.type !== "hidden" && "defaultValue" in field && field.value !== field.defaultValue;
    }));
  }
  function keepDraft(panel) {
    var form = editForm(panel), detail = panel.querySelector("[data-detail-job]");
    if (!form || !detail) return;
    if (!dirty(form)) { delete drafts[detail.getAttribute("data-detail-job")]; return; }
    var values = Object.create(null);
    Array.prototype.forEach.call(form.elements, function (field) {
      if (field.name) values[field.name] = field.value;
    });
    drafts[detail.getAttribute("data-detail-job")] = values;
  }
  function restoreDraft(panel) {
    var detail = panel.querySelector("[data-detail-job]");
    var saved = detail && drafts[detail.getAttribute("data-detail-job")];
    var form = editForm(panel);
    if (!saved || !form) return false;
    Array.prototype.forEach.call(form.elements, function (field) {
      if (Object.prototype.hasOwnProperty.call(saved, field.name)) field.value = saved[field.name];
    });
    panel.querySelector("#job-edit-draft").hidden = false;
    return true;
  }
  window.addEventListener("beforeunload", function (event) {
    if (!dirty(document.querySelector("#job-edit form")) && !Object.keys(drafts).length) return;
    event.preventDefault(); event.returnValue = "";
  });

  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (target && target.closest("[data-drawer-close]") && drawer()) drawer().close();
    var remove = document.getElementById("detail-remove-dialog");
    if (target && target.closest("[data-detail-remove]") && remove && !remove.open) remove.showModal();
    if (target && target.closest("[data-detail-cancel]") && remove) remove.close();
    if (target && target.closest("[data-detail-other]")) {
      document.getElementById("detail-other").hidden = false;
      target.closest("[data-detail-other]").setAttribute("aria-expanded", "true");
      document.getElementById("detail-text").focus();
    }
  });

  document.addEventListener("keydown", function (event) {
    var panel = drawer();
    var remove = document.querySelector("#detail-remove-dialog[open]");
    if (remove) panel = remove;
    if (event.key !== "Tab" || !panel || document.querySelector("dialog[data-modal][open]")) return;
    var controls = Array.prototype.filter.call(panel.querySelectorAll(
      "a[href], button:not([disabled]), input:not([disabled]):not([type='hidden']), " +
      "select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
    ), function (element) { return element.getClientRects().length > 0; });
    var first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });

  document.addEventListener("close", function (event) {
    var panel = event.target;
    if (panel instanceof Element && panel.id === "detail-remove-dialog") {
      var button = document.getElementById("detail-remove");
      if (button) button.focus();
      return;
    }
    if (!(panel instanceof Element) || !panel.matches("dialog.job-drawer")) return;
    keepDraft(panel);
    panel.remove();
    if (opener && opener.isConnected) opener.focus();
    var card = document.getElementById("inbox-card");
    if (removedCardAnalysis && !changed && card) {
      card.insertBefore(removedCardAnalysis, analysisNext && analysisNext.parentNode === card ? analysisNext : null);
      if (window.htmx) window.htmx.process(removedCardAnalysis);
    }
    removedCardAnalysis = null; analysisNext = null;
    if (changed) {
      restoreInboxFocus = true;
      document.body.dispatchEvent(new CustomEvent("inbox:changed", {bubbles: true}));
    }
  }, true);
}());
