/* Typing lives only in page memory. Restored edits keep the original version/action,
   so a new server version refuses them instead of silently rebasing them. */
(function () {
  "use strict";
  var drafts = new Map();
  var requests = new WeakMap();
  var pending = new WeakMap();
  function aid() {
    var root = document.getElementById("tailoring");
    return root ? root.getAttribute("data-application-id") : "";
  }
  function key(box) { return aid() + ":" + box.getAttribute("data-draft-key"); }
  function remember(box) {
    if (!(box instanceof HTMLTextAreaElement) || !box.hasAttribute("data-draft-key")) return;
    var baseline = pending.has(box) ? pending.get(box) : box.hasAttribute("data-draft-baseline") ? box.getAttribute("data-draft-baseline") : box.defaultValue;
    var id = box.getAttribute("data-memory-key") || key(box);
    if (box.value === baseline) {
      drafts.delete(id);
      box.removeAttribute("data-holds-refresh");
      return;
    }
    var form = box.form;
    var version = form.querySelector('[name="version"]');
    var section = form.querySelector('[name="section"]');
    var role = form.querySelector('[name="role"]');
    drafts.set(id, {aid: aid(), key: box.getAttribute("data-draft-key"), identity: box.getAttribute("data-draft-identity"), text: box.value, baseline: baseline, version: version ? version.value : null, action: form.getAttribute("action"), section: section ? section.value : null, role: role ? role.value : null});
    box.setAttribute("data-holds-refresh", "");
  }
  document.body.addEventListener("input", function (e) { remember(e.target); });
  document.body.addEventListener("change", function (e) {
    if (e.target.form) e.target.form.querySelectorAll("textarea[data-draft-key]").forEach(remember);
  });
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    var form = e.detail.elt;
    if (!(form instanceof HTMLFormElement)) return;
    var captured = [];
    form.querySelectorAll("textarea[data-draft-key]").forEach(function (box) {
      remember(box);
      captured.push({id: box.getAttribute("data-memory-key") || key(box), box: box, text: box.value});
      pending.set(box, box.value);
    });
    if (captured.length) requests.set(e.detail.xhr, captured);
  });
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var captured = requests.get(e.detail.xhr);
    if (!captured) return;
    var saved = e.detail.xhr.status === 200 && e.detail.xhr.responseText.indexOf('id="tailoring-dialog-3"') === -1;
    captured.forEach(function (entry) {
      var laterTyping = entry.box.value !== entry.text;
      if (!saved && !laterTyping) pending.delete(entry.box);
      if (saved && entry.box.value === entry.text) drafts.delete(entry.id);
      else remember(entry.box);
      // Swapping blurs the old textarea and can emit a final change event. Its saved
      // baseline is now the submitted value, even while the old node is being removed.
      if (saved || laterTyping) entry.box.setAttribute("data-draft-baseline", entry.text);
      pending.delete(entry.box);
    });
    requests.delete(e.detail.xhr);
  });
  document.body.addEventListener("htmx:afterRequest", function (e) {
    var captured = requests.get(e.detail.xhr);
    if (!captured) return;
    captured.forEach(function (entry) { pending.delete(entry.box); remember(entry.box); });
    requests.delete(e.detail.xhr);
  });
  document.body.addEventListener("htmx:configRequest", function (e) {
    if (e.detail.elt.id !== "tailoring") return;
    var key = e.detail.elt.getAttribute("data-cv-task-key");
    if (key) e.detail.headers["X-JP-CV-Seen"] = key;
  });
  function restore() {
    var root = document.getElementById("cv-step");
    if (!root || root.getAttribute("data-readonly") === "true") return;
    var boxes = Array.from(root.querySelectorAll("textarea[data-draft-key]"));
    var restored = false;
    drafts.forEach(function (draft, id) {
      if (draft.aid !== aid()) return;
      var box = boxes.find(function (field) { return field.getAttribute("data-draft-key") === draft.key; });
      if (!box && draft.identity) box = boxes.find(function (field) { return field.getAttribute("data-draft-identity") === draft.identity; });
      if (!box) {
        if (!root.querySelector('[data-recovered-key="' + CSS.escape(id) + '"]')) {
          var label = document.createElement("label");
          label.textContent = "Your unsaved text from an earlier line (copy it before reloading)";
          var recovery = document.createElement("textarea");
          recovery.readOnly = true;
          recovery.value = draft.text;
          recovery.setAttribute("data-recovered-key", id);
          label.appendChild(recovery);
          root.appendChild(label);
        }
        restored = true;
        return;
      }
      box.value = draft.text;
      box.setAttribute("data-draft-baseline", draft.baseline);
      box.setAttribute("data-memory-key", id);
      box.setAttribute("data-holds-refresh", "");
      var form = box.form;
      if (draft.version) {
        form.querySelector('[name="version"]').value = draft.version;
        form.setAttribute("action", draft.action);
        form.setAttribute("hx-post", draft.action);
        if (window.htmx) window.htmx.process(form);
      }
      if (draft.section !== null) form.querySelector('[name="section"]').value = draft.section;
      if (draft.role !== null) form.querySelector('[name="role"]').value = draft.role;
      var details = box.closest("details");
      if (details) details.open = true;
      restored = true;
    });
    if (restored) {
      var note = document.getElementById("cv-draft-note");
      if (note.hidden) {
        note.hidden = false;
        document.body.dispatchEvent(new CustomEvent("announce", {detail: {value: note.textContent}}));
      }
    }
  }
  document.body.addEventListener("htmx:afterSettle", restore);
}());
