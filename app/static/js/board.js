// The Board screen (M5.4; htmx-ui skill): the card status menu and drag-and-drop. Loaded on
// /board only, after SortableJS. No build step; plain browser JS. The words a person reads live
// in the templates and the server's answers; this script only moves focus and places a few
// short messages for the screen reader.
//
// Both ways of moving a card send the same request: the menu form posts through htmx, and a drop
// posts the same three fields (version, status, source) to the same /applications/<id>/status.
// The server's answer replaces #board-content, so the card appears in its new list with its
// new version, focus on its title, and "Moved to <status>." is announced.
(function () {
  "use strict";

  var phone = window.matchMedia("(max-width: 767.98px)");
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  var pending = {}; // drops whose answer is awaited, by request path: each keeps its own place
  var pressInMenu = false; // a pointer press inside a menu is on its way to a click

  // The live region is app.js's; it listens for this event (the same one a route's HX-Trigger
  // header makes).
  function say(message) {
    document.body.dispatchEvent(new CustomEvent("announce", { detail: { value: message } }));
  }

  // --- the "Move to..." menu: Enter opens it on its first item, the arrows choose, Enter moves
  // (each item is a submit button of the card's move form), Esc closes it and gives focus back.

  function menuOf(button) {
    return document.getElementById(button.getAttribute("aria-controls") || "");
  }

  function itemsOf(menu) {
    return Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"]'));
  }

  function closeMenu(button, refocus) {
    var menu = menuOf(button);
    if (menu) menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (refocus) button.focus();
  }

  function closeMenus(except) {
    var open = document.querySelectorAll('.board-menu-button[aria-expanded="true"]');
    Array.prototype.forEach.call(open, function (button) {
      if (button !== except) closeMenu(button, false);
    });
  }

  function openMenu(button) {
    var menu = menuOf(button);
    if (!menu) return;
    closeMenus(button);
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    var items = itemsOf(menu);
    if (items.length) items[0].focus();
  }

  function menuButtonOf(target) {
    return target instanceof Element ? target.closest(".board-menu-button") : null;
  }

  document.addEventListener("click", function (event) {
    var button = menuButtonOf(event.target);
    if (button) {
      if (button.getAttribute("aria-expanded") === "true") closeMenu(button, false);
      else openMenu(button);
      return;
    }
    // A press outside every open menu closes them.
    if (!(event.target instanceof Element) || !event.target.closest(".board-menu")) closeMenus(null);
  });

  document.addEventListener("keydown", function (event) {
    var target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    var button = menuButtonOf(target);
    if (button && event.key === "ArrowDown") {
      event.preventDefault();
      openMenu(button);
      return;
    }
    var menu = target.closest('[role="menu"]');
    if (!menu || menu.hidden) return;
    var opener = document.querySelector('[aria-controls="' + menu.id + '"]');
    var items = itemsOf(menu);
    var at = items.indexOf(target);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      var step = event.key === "ArrowDown" ? 1 : -1;
      items[(at + step + items.length) % items.length].focus();
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      items[event.key === "Home" ? 0 : items.length - 1].focus();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (opener) closeMenu(opener, true);
    } else if (event.key === "Tab" && opener) {
      closeMenu(opener, false); // focus carries on from the button as usual
    }
  });

  // A pointer press inside a menu may take focus away before the click lands (Safari does not
  // focus a button that is pressed); the menu must stay open until that click is done.
  document.addEventListener("pointerdown", function (event) {
    pressInMenu = event.target instanceof Element && event.target.closest(".board-menu") !== null;
  }, true);
  ["click", "pointerup", "pointercancel"].forEach(function (name) {
    document.addEventListener(name, function () {
      window.setTimeout(function () { pressInMenu = false; }, 0);
    }, true);
  });

  // Focus leaving the menu and its button (a click elsewhere, Tab) closes it.
  document.addEventListener("focusout", function (event) {
    var wrapper = event.target instanceof Element ? event.target.closest(".board-menu") : null;
    if (!wrapper || pressInMenu) return;
    var next = event.relatedTarget;
    if (next instanceof Element && wrapper.contains(next)) return;
    var button = wrapper.querySelector(".board-menu-button");
    if (button && button.getAttribute("aria-expanded") === "true") closeMenu(button, false);
  });

  // --- drag and drop (SortableJS). A drop on another list posts the move; a drop on the same
  // list changes nothing. Dragging is off at phone width (the menu is the way) and animation is
  // off with reduced motion. Closed has no list: its cards move with their menu.

  function dragOptions() {
    return { disabled: phone.matches, animation: reducedMotion.matches ? 0 : 150 };
  }

  function setup() {
    if (typeof window.Sortable === "undefined") return;
    var options = dragOptions();
    Array.prototype.forEach.call(document.querySelectorAll("[data-sortable]"), function (list) {
      if (window.Sortable.get(list)) return;
      window.Sortable.create(list, {
        group: "board",
        sort: false,
        draggable: ".board-card",
        filter: ".board-menu",
        preventOnFilter: false,
        delay: 150,
        delayOnTouchOnly: true,
        animation: options.animation,
        disabled: options.disabled,
        ghostClass: "is-ghost",
        chosenClass: "is-chosen",
        onEnd: dropped
      });
    });
  }

  function syncOptions() {
    var options = dragOptions();
    Array.prototype.forEach.call(document.querySelectorAll("[data-sortable]"), function (list) {
      var sortable = window.Sortable && window.Sortable.get(list);
      if (!sortable) return;
      sortable.option("disabled", options.disabled);
      sortable.option("animation", options.animation);
    });
  }

  function putBack(drop, message) {
    // A card the page no longer holds was replaced by the server's answer: nothing to undo.
    if (!document.body.contains(drop.card)) return;
    drop.from.insertBefore(drop.card, drop.from.children[drop.index] || null);
    say(message);
  }

  function dropped(event) {
    var card = event.item;
    if (event.to === event.from) return;
    var drop = { card: card, from: event.from, index: event.oldIndex };
    var id = card.getAttribute("data-application-id");
    var version = card.getAttribute("data-version");
    var status = event.to.getAttribute("data-status");
    if (!id || !version || !status || !window.htmx) {
      putBack(drop, "The card could not be moved. It is back where it was.");
      return;
    }
    drop.path = "/applications/" + encodeURIComponent(id) + "/status";
    (pending[drop.path] = pending[drop.path] || []).push(drop);
    window.htmx.ajax("POST", drop.path, {
      target: "#board-content",
      swap: "outerHTML settle:0ms",
      values: { version: version, status: status, source: "board" }
    });
  }

  // The answer: 200 swaps the board (the card is in its new list). Anything else puts the card
  // back where it was and says so; a stale version also shows the reload prompt, which says why
  // (so the words here are short). Each drop keeps its own place, so two answers in a row never
  // mix them up.
  document.body.addEventListener("htmx:afterRequest", function (event) {
    var detail = event.detail || {};
    var path = detail.requestConfig ? detail.requestConfig.path : "";
    var drops = pending[path];
    if (!drops || !drops.length) return;
    var drop = drops.shift();
    if (!drops.length) pending[path] = undefined;
    var status = detail.xhr ? detail.xhr.status : 0;
    if (status >= 200 && status < 300) return;
    putBack(
      drop,
      status === 409
        ? "This item changed elsewhere. The card is back where it was."
        : "The move was not saved. The card is back where it was."
    );
  });

  // Lists the server's answer replaces give up their Sortable instance (its listeners and
  // state), so instances never pile up with each move.
  document.body.addEventListener("htmx:beforeCleanupElement", function (event) {
    var element = event.detail ? event.detail.elt : event.target;
    if (!(element instanceof Element) || !element.hasAttribute("data-sortable")) return;
    var sortable = window.Sortable && window.Sortable.get(element);
    if (sortable) sortable.destroy();
  });

  document.body.addEventListener("htmx:afterSettle", setup);
  phone.addEventListener("change", syncOptions);
  reducedMotion.addEventListener("change", syncOptions);
  setup();
})();
