/* The Prep tab. A passive refresh preserves a safe heading's focus without moving it from
   elsewhere; the practice box keeps what is typed in it across a background refresh and shows
   how many characters are left. The adapt section (#adapt) swaps alone, so the practice box is
   never touched by it; an Edit left open in one paragraph is put back after an action on
   another one (the new version has new paragraph ids, so a paragraph is found by its text). */
(function () {
  "use strict";
  var restore = "";
  var draft = null;
  var edits = [];
  var REFRESH = /\/practice\/[^/?]+\?refresh=1/;

  function format(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  // The counter under the practice box; the polite note speaks only when the person comes
  // near the limit (fewer than 200 left) or reaches it, once for each, not on every key.
  function count(box, speak) {
    var shown = document.getElementById("practice-count");
    var note = document.getElementById("practice-count-note");
    var max = parseInt(box.getAttribute("maxlength") || "4000", 10);
    var left = max - box.value.length;
    if (shown) shown.textContent = format(box.value.length) + " of " + format(max) + " characters";
    if (!note) return;
    var band = left <= 0 ? "limit" : left < 200 ? "near" : "";
    if (note.getAttribute("data-band") === band) return;
    note.setAttribute("data-band", band);
    if (!speak && !band) return;
    note.textContent = band === "limit" ? "Limit reached: " + format(max) + " characters." :
      band === "near" ? "Fewer than 200 characters left." : "";
  }

  document.body.addEventListener("input", function (event) {
    var box = event.target;
    if (box && box.id === "practice-answer") count(box, true);
  });

  // The Edit forms the person has open in the adapt section, except the one being answered.
  function openEdits(section, requester) {
    var answered = requester && requester.closest ? requester.closest("li[data-unit]") : null;
    var found = [];
    section.querySelectorAll("li[data-unit]").forEach(function (unit) {
      var details = unit.querySelector("details[data-unit-edit]");
      var box = details ? details.querySelector("textarea") : null;
      var text = unit.querySelector("[data-unit-text]");
      if (unit === answered || !details || !details.open || !box || !text) return;
      found.push({ saved: text.textContent, value: box.value });
    });
    return found;
  }

  function putBackEdits() {
    var used = [];
    var units = Array.prototype.slice.call(document.querySelectorAll("#adapt li[data-unit]"));
    edits.forEach(function (edit) {
      var unit = units.filter(function (item) {
        var text = item.querySelector("[data-unit-text]");
        return used.indexOf(item) < 0 && text && text.textContent === edit.saved;
      })[0];
      var details = unit ? unit.querySelector("details[data-unit-edit]") : null;
      var box = details ? details.querySelector("textarea") : null;
      if (!unit || !details || !box || box.hasAttribute("data-autofocus")) return;
      used.push(unit);
      details.open = true;
      box.value = edit.value;
    });
    edits = [];
  }

  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var d = event.detail || {};
    var requester = d.requestConfig && d.requestConfig.elt;
    var active = document.activeElement;
    restore = "";
    draft = null;
    edits = d.shouldSwap && d.target && d.target.id === "adapt" ? openEdits(d.target, requester) : [];
    if (d.shouldSwap && d.target && (d.target.id === "prep" || d.target.id === "adapt") &&
        requester === d.target &&
        active && d.target.contains(active) && active.hasAttribute("data-refresh-safe-focus")) {
      restore = active.id;
    }
    // A refresh (the poll, a task or usage event, "Refresh practice") must not wipe an answer
    // the person is typing: the box is put back as it was once the new region has settled.
    var path = (d.pathInfo && d.pathInfo.requestPath) || "";
    var box = d.target && d.target.id === "prep" ? d.target.querySelector("#practice-answer") : null;
    if (d.shouldSwap && box && REFRESH.test(path) && box.value !== box.defaultValue) {
      draft = box.value;
    }
  });

  document.body.addEventListener("htmx:afterSettle", function () {
    if (edits.length) putBackEdits();
    var box = document.getElementById("practice-answer");
    if (draft !== null && box && box.value !== draft) {
      box.value = draft;
      count(box, false);
    }
    draft = null;
    if (!restore) return;
    var next = document.getElementById(restore);
    restore = "";
    if (next && (document.activeElement === document.body || document.activeElement === next)) next.focus();
  });
}());
