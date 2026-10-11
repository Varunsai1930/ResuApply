// Light or dark mode: the system setting, unless the user chose one with the header toggle.
// The choice is kept in this browser only (localStorage) and set on <html> as data-theme.
// Loaded in <head> without defer, so a chosen mode applies before the page is drawn.
(function () {
  "use strict";
  var KEY = "resuapply-theme";
  var root = document.documentElement;
  var system = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function stored() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }  // blocked storage: follow the system
  }
  function store(value) {
    try { window.localStorage.setItem(KEY, value); } catch (e) { /* the choice lasts for this page only */ }
  }
  function isDark() {
    if (root.dataset.theme === "dark" || root.dataset.theme === "light") return root.dataset.theme === "dark";
    return Boolean(system && system.matches);
  }

  var choice = stored();
  if (choice === "dark" || choice === "light") root.dataset.theme = choice;

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.querySelector("[data-theme-toggle]");
    if (!button) return;
    function sync() {
      var dark = isDark();
      button.setAttribute("aria-pressed", dark ? "true" : "false");
      button.title = dark ? "Switch to light mode" : "Switch to dark mode";
    }
    button.addEventListener("click", function () {
      var next = isDark() ? "light" : "dark";
      root.dataset.theme = next;
      store(next);
      sync();
    });
    if (system && system.addEventListener) system.addEventListener("change", sync);
    sync();
    button.hidden = false;
  });
})();
