(function () {
  "use strict";
  var pressInMenu = false;
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

}());
