// Restore the sidebar before drawing the page. A collapsed rail keeps navigation available.
(function () {
  "use strict";
  var KEY = "resuapply-sidebar";
  var root = document.documentElement;
  try {
    if (window.localStorage.getItem(KEY) === "collapsed") root.dataset.sidebar = "collapsed";
  } catch (e) { /* Storage may be blocked; the sidebar starts open. */ }

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.querySelector("[data-sidebar-toggle]");
    if (!button) return;
    function sync() {
      var expanded = root.dataset.sidebar !== "collapsed";
      var label = expanded ? "Collapse sidebar" : "Open sidebar";
      button.setAttribute("aria-expanded", expanded ? "true" : "false");
      button.setAttribute("aria-label", label);
      button.title = label;
    }
    button.addEventListener("click", function () {
      var next = root.dataset.sidebar === "collapsed" ? "expanded" : "collapsed";
      root.dataset.sidebar = next;
      try { window.localStorage.setItem(KEY, next); } catch (e) { /* Keep the choice for this page. */ }
      sync();
    });
    sync();
    button.hidden = false;
  });
})();
