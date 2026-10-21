// Application drawer and memory-only drafts. No application text goes in browser storage.
(function () {
  "use strict";
  var opener = null, drafts = Object.create(null), changed = false, refreshFocus = "";
  var stops = "a[href], button:not([disabled]), input:not([type='hidden']), select, textarea, [tabindex='0']";
  function panel() { return document.querySelector("#application-drawer[open]"); }
  function fields(form) {
    var values = Object.create(null);
    Array.prototype.forEach.call(form.elements, function (field) {
      if (field.name) values[field.name] = field.type === "checkbox" ? field.checked : field.value;
    });
    return values;
  }
  function dirty(form) {
    return (form.applicationPending && JSON.stringify(fields(form)) !== JSON.stringify(form.applicationPending)) || form.hasAttribute("data-unsaved") || Array.prototype.some.call(form.elements, function (field) {
      if (!field.name || field.type === "hidden") return false;
      if (field.type === "checkbox") return field.checked !== field.defaultChecked;
      if (field.tagName === "SELECT") return Array.prototype.some.call(field.options, function (option) { return option.selected !== option.defaultSelected; });
      return field.value !== field.defaultValue;
    });
  }
  function keep(root, except) {
    if (!root) return;
    var aid = root.getAttribute("data-application");
    if (!aid) return;
    var saved = drafts[aid] || Object.create(null);
    root.querySelectorAll("form[data-detail-editor]").forEach(function (form) {
      if (form.id === except || !dirty(form)) delete saved[form.id];
      else saved[form.id] = { values: fields(form), open: !!form.closest("details[open]") };
    });
    if (Object.keys(saved).length) drafts[aid] = saved;
    else delete drafts[aid];
  }
  function restore(root, only) {
    if (!root) return false;
    var saved = drafts[root.getAttribute("data-application")];
    if (!saved) return false;
    Object.keys(saved).forEach(function (id) {
      if (only && id !== only) return;
      var form = document.getElementById(id), draft = saved[id];
      if (!form || !root.contains(form)) return;
      Array.prototype.forEach.call(form.elements, function (field) {
        if (!Object.prototype.hasOwnProperty.call(draft.values, field.name)) return;
        if (field.type === "checkbox") field.checked = draft.values[field.name];
        else field.value = draft.values[field.name];
      });
      if (draft.open && form.closest("details")) form.closest("details").open = true;
    });
    return true;
  }
  function timedFields(form) {
    var checkbox = form.querySelector("[name='all_day']");
    if (!checkbox) return;
    form.querySelectorAll("[name='time'], [name='duration_minutes']").forEach(function (field) {
      var wasDisabled = field.disabled;
      field.disabled = checkbox.checked;
      if (wasDisabled && !checkbox.checked && field.name === "duration_minutes" && !field.validity.valid) field.value = "60";
    });
  }
  document.addEventListener("change", function (event) {
    if (event.target.name === "all_day" && event.target.form) timedFields(event.target.form);
  });
  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var path = event.detail.pathInfo && event.detail.pathInfo.requestPath;
    if (/^\/applications\/[^/]+\/detail$/.test(path || "")) {
      opener = document.activeElement; changed = false;
    }
    var form = event.detail.elt;
    if (form && form.matches("form[data-detail-editor]")) {
      form.applicationPending = fields(form);
      event.detail.xhr.applicationSave = { id: form.closest("[data-application]").getAttribute("data-application"), form: form.id, values: fields(form), element: form };
    }
  });
  document.body.addEventListener("htmx:configRequest", function (event) {
    if (event.detail.elt.id !== "board") return;
    // The Closed toggle swaps only the content. Refresh from its current page address.
    var here = new URL(window.location.href);
    if (here.pathname === "/board") event.detail.path = here.pathname + here.search;
  });
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail.target;
    var save = event.detail.xhr && event.detail.xhr.applicationSave;
    if (target && target.matches("form[data-detail-editor]") && event.detail.shouldSwap && save &&
        JSON.stringify(fields(save.element)) !== JSON.stringify(save.values)) {
      var saved = drafts[save.id] || Object.create(null);
      saved[save.form] = {values: fields(save.element), open: !!target.closest("details[open]")};
      drafts[save.id] = saved;
      save.restoreRefused = true;
    }
    if (target && target.id === "application-detail" && event.detail.shouldSwap) {
      var unchanged = save && JSON.stringify(fields(save.element)) === JSON.stringify(save.values);
      keep(target, event.detail.xhr.status === 200 && unchanged ? save.form : "");
    }
  });
  document.body.addEventListener("htmx:afterRequest", function (event) {
    var save = event.detail.xhr && event.detail.xhr.applicationSave;
    if (!save) return;
    delete save.element.applicationPending;
    if (event.detail.xhr.status !== 200) return;
    var saved = drafts[save.id];
    if (saved && saved[save.form] && JSON.stringify(saved[save.form].values) === JSON.stringify(save.values)) delete saved[save.form];
    if (saved && !Object.keys(saved).length) delete drafts[save.id];
  });
  document.body.addEventListener("htmx:afterSettle", function (event) {
    var root = document.getElementById("application-detail");
    var target = event.detail.target;
    if (refreshFocus && target && target.id === "board") {
      var returned = document.querySelector(refreshFocus) || document.getElementById("board-closed-toggle") || document.getElementById("board-heading");
      refreshFocus = "";
      if (returned) returned.focus();
    }
    if (target && (target.id === "application-detail" || target.id === "application-details")) restore(root);
    var save = event.detail.xhr && event.detail.xhr.applicationSave;
    if (target && target.matches("form[data-detail-editor]") && save && save.restoreRefused) {
      restore(root, save.form);
      var note = target.querySelector("[data-editor-draft]");
      if (note) note.hidden = false;
      document.body.dispatchEvent(new CustomEvent("announce", {detail: {value: "Your newer typing has been kept. Review the message before saving again."}}));
    }
    if (root) root.querySelectorAll("form.application-event-form").forEach(timedFields);
    var drawer = document.getElementById("application-drawer");
    if (drawer && !drawer.open) {
      var restored = restore(root);
      drawer.showModal();
      var notice = drawer.querySelector("[data-application-draft]");
      if (notice) notice.hidden = !restored;
      document.body.dispatchEvent(new CustomEvent("announce", {detail: {value: restored ? "Application details opened. Unsaved values restored." : "Application details opened."}}));
    }
    var invalid = target && target.querySelector("[aria-invalid='true']");
    if (invalid) invalid.focus();
  });
  document.body.addEventListener("application:saved", function () { if (panel()) changed = true; });
  document.body.addEventListener("board:changed", function () {
    if (panel()) changed = true;
    else if (document.getElementById("board")) document.body.dispatchEvent(new CustomEvent("board:refresh", {bubbles: true}));
  });
  document.addEventListener("click", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (target && target.closest("[data-application-close]") && panel()) panel().close();
  });
  document.addEventListener("keydown", function (event) {
    var drawer = panel();
    if (event.key !== "Tab" || !drawer || document.querySelector("dialog[data-modal][open]")) return;
    var controls = Array.prototype.filter.call(drawer.querySelectorAll(stops), function (el) { return el.getClientRects().length > 0; });
    var first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  // Chromium can cancel both nested dialogs on one native Escape. Close only the question.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || !panel()) return;
    var question = panel().querySelector("dialog[data-modal][open]");
    if (!question) return;
    event.preventDefault();
    event.stopPropagation();
    if (!question.querySelector(".htmx-request")) question.close();
  }, true);
  document.addEventListener("close", function (event) {
    var drawer = event.target;
    if (!(drawer instanceof Element) || drawer.id !== "application-drawer") return;
    keep(drawer.querySelector("[data-application]"), "");
    drawer.remove();
    if (opener && opener.isConnected) opener.focus();
    if (changed) {
      var aid = drawer.querySelector("[data-application]").getAttribute("data-application");
      refreshFocus = 'a[hx-get="/applications/' + aid + '/detail"]';
      document.body.dispatchEvent(new CustomEvent("board:refresh", {bubbles: true}));
    }
  }, true);
  window.addEventListener("beforeunload", function (event) {
    var root = document.getElementById("application-detail");
    if (root) keep(root, "");
    if (Object.keys(drafts).length) { event.preventDefault(); event.returnValue = ""; }
  });
}());
