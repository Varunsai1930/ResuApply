// Minimal behaviour for the server-rendered forms:
// - add/remove rows in repeating sections (rows are cloned from <template> elements),
// - warn before leaving a form with unsaved edits,
// - move focus to the error summary after a failed submit,
// - show only the fields for the chosen requirement criterion,
// - disable AI buttons while a request runs, so it can't be sent twice,
// - let "Use as starting point" fill an answer box from a saved answer (the user still saves it),
// - count characters or words against an employer's length limit,
// - show the "save the approved package" option only while Applied is chosen.
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
  document.addEventListener("submit", function (e) {
    dirtyForms.delete(e.target);
    if (e.target.matches("form[data-ai]")) {
      if (e.target.dataset.sent) {
        e.preventDefault();
        return;
      }
      e.target.dataset.sent = "1";
      document.querySelectorAll("form[data-ai] button").forEach(function (button) {
        button.disabled = true;
      });
      const button = e.target.querySelector("button");
      if (button && button.dataset.busyLabel) {
        button.textContent = button.dataset.busyLabel;
        button.setAttribute("aria-busy", "true");
      }
    }
  });
  // Pages restored from the back/forward cache should not keep buttons stuck in the busy state.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    document.querySelectorAll("form[data-ai]").forEach(function (form) {
      delete form.dataset.sent;
      form.querySelectorAll("button").forEach(function (b) { b.disabled = false; b.removeAttribute("aria-busy"); });
    });
  });

  // Requirement criteria: show the fields for the selected type only.
  function syncCriterion(select) {
    const row = select.closest("[data-item]");
    if (!row) return;
    row.querySelectorAll("[data-criterion]").forEach(function (group) {
      group.hidden = group.dataset.criterion !== select.value;
    });
  }
  document.addEventListener("change", function (e) {
    if (e.target.matches("select[data-criterion-type]")) syncCriterion(e.target);
  });
  new MutationObserver(function () {
    document.querySelectorAll("select[data-criterion-type]").forEach(syncCriterion);
  }).observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("beforeunload", function (event) {
    if (dirtyForms.size) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // Answer bank: copy a saved answer into the answer box. Nothing is saved until the user submits the form.
  document.querySelectorAll("[data-use-answer]").forEach(function (button) { button.hidden = false; });
  document.addEventListener("click", function (event) {
    const use = event.target.closest("[data-use-answer]");
    if (!use) return;
    const form = use.closest("form");
    const box = form && form.querySelector("textarea[name=text]");
    const suggestion = use.closest("[data-bank-id]");
    if (!box || !suggestion) return;
    const text = suggestion.querySelector(".bank-text").textContent;
    box.value = text;
    box.dataset.fromBank = text;
    form.elements.origin.value = "bank";
    form.elements.bank_id.value = suggestion.dataset.bankId;
    box.dispatchEvent(new Event("input", { bubbles: true }));
    box.focus();
  });
  // Editing a copied answer makes it the user's own again.
  document.addEventListener("input", function (event) {
    const box = event.target;
    if (!box.matches || !box.matches("textarea[name=text]") || box.dataset.fromBank === undefined) return;
    if (box.value !== box.dataset.fromBank) {
      box.form.elements.origin.value = "user";
      box.form.elements.bank_id.value = "";
      delete box.dataset.fromBank;
    }
  });

  // Length counters for answers with an employer limit.
  function updateCounter(box) {
    document.querySelectorAll("[data-counter-for='" + box.id + "']").forEach(function (counter) {
      const limit = Number(counter.dataset.limit);
      const words = counter.dataset.unit === "words";
      const size = words ? (box.value.trim() ? box.value.trim().split(/\s+/).length : 0) : Array.from(box.value).length;
      counter.textContent = size + " of " + limit.toLocaleString() + " " + (words ? "words" : "characters");
      counter.classList.toggle("over-limit", size > limit);
    });
  }
  document.addEventListener("input", function (event) {
    if (event.target.id) updateCounter(event.target);
  });

  // Offer "save the approved package" only while the chosen status is Applied.
  function syncStatusOptions() {
    document.querySelectorAll("[data-when-status]").forEach(function (field) {
      const select = field.closest("form").querySelector("select[name=status]");
      if (select) field.hidden = select.value !== field.dataset.whenStatus;
    });
  }
  document.addEventListener("change", function (event) {
    if (event.target.matches("select[name=status]")) syncStatusOptions();
  });

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("select[data-criterion-type]").forEach(syncCriterion);
    syncStatusOptions();
    const summary = document.querySelector("[data-focus]");
    if (summary) summary.focus();
  });
})();
