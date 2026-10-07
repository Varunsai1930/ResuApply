// Minimal behaviour for the server-rendered forms:
// - add/remove rows in repeating sections (rows are cloned from <template> elements),
// - warn before leaving a form with unsaved edits,
// - move focus to the error summary after a failed submit.
(function () {
  "use strict";
  let counter = 0;
  const token = () => "n" + Date.now().toString(36) + (counter++).toString(36);

  document.addEventListener("click", function (event) {
    const add = event.target.closest("[data-add]");
    if (add) {
      const template = document.getElementById(add.dataset.add);
      const target = document.getElementById(add.dataset.target);
      if (!template || !target) return;
      const html = template.innerHTML
        .replaceAll("__i__", token())
        .replaceAll("__p__", add.dataset.prefix || "");
      target.insertAdjacentHTML("beforeend", html);
      const added = target.lastElementChild;
      const first = added && added.querySelector("input:not([type=hidden]), textarea, select");
      if (first) first.focus();
      markDirty(add);
      return;
    }
    const remove = event.target.closest("[data-remove]");
    if (remove) {
      const item = remove.closest("[data-item]");
      if (item) {
        markDirty(remove);
        item.remove();
      }
    }
  });

  // Unsaved-changes guard.
  const dirtyForms = new Set();
  function markDirty(el) {
    const form = el.closest("form[data-guard]");
    if (form) dirtyForms.add(form);
  }
  document.addEventListener("input", (e) => markDirty(e.target));
  document.addEventListener("submit", (e) => dirtyForms.delete(e.target));
  window.addEventListener("beforeunload", function (event) {
    if (dirtyForms.size) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    const summary = document.querySelector("[data-focus]");
    if (summary) summary.focus();
  });
})();
