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
        const list = item.parentElement;
        const neighbor = item.nextElementSibling || item.previousElementSibling;
        const field = neighbor && (neighbor.querySelector('input:not([type="hidden"]), textarea, select')
          || neighbor.querySelector('button'));
        const addButton = list && Array.from(document.querySelectorAll('[data-add]'))
          .find(button => button.dataset.target === list.id);
        markDirty(remove);
        item.remove();
        const focus = field || addButton;
        if (focus) focus.focus();
      }
    }
  });

  // Unsaved-changes guard.
  const dirtyForms = new Set();
  let pendingSubmission = null;
  let aiSubmission = null;
  function markDirty(el) {
    const form = el.closest("form[data-guard]");
    if (form) dirtyForms.add(form);
  }
  function restoreAIControls() {
    if (!aiSubmission) return;
    delete aiSubmission.event.target.dataset.sent;
    aiSubmission.buttons.forEach(({ button, disabled }) => { button.disabled = disabled; });
    const label = aiSubmission.label;
    if (label) {
      label.button.innerHTML = label.html;
      if (label.busy === null) label.button.removeAttribute("aria-busy");
      else label.button.setAttribute("aria-busy", label.busy);
    }
    aiSubmission = null;
  }
  document.addEventListener("input", (e) => markDirty(e.target));
  document.addEventListener("change", (e) => markDirty(e.target));
  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return;
    if (e.target.matches("form[data-ai]")) {
      if (aiSubmission || e.target.dataset.sent) {
        e.preventDefault();
        return;
      }
      // Native beforeunload offers no cancellation event. Ask before locking AI controls,
      // then exempt this confirmed navigation from a second unsaved-changes prompt.
      const otherEdits = Array.from(dirtyForms).some((form) => form !== e.target);
      if (otherEdits && !window.confirm("You have unsaved changes. Leave this page and discard them to run AI?")) {
        e.preventDefault();
        pendingSubmission = null;
        return;
      }
      pendingSubmission = { event: e, confirmed: otherEdits };
      const buttons = Array.from(document.querySelectorAll("form[data-ai] button"),
        (button) => ({ button, disabled: button.disabled }));
      const button = e.target.querySelector("button");
      const label = button && button.dataset.busyLabel ? {
        button, html: button.innerHTML, busy: button.getAttribute("aria-busy"),
      } : null;
      aiSubmission = { event: e, buttons, label };
      e.target.dataset.sent = "1";
      buttons.forEach(({ button }) => { button.disabled = true; });
      if (label) {
        button.textContent = button.dataset.busyLabel;
        button.setAttribute("aria-busy", "true");
      }
      // Browser-dispatched events can run microtasks between listeners. Wait for the
      // full submit dispatch before checking whether another listener canceled it.
      setTimeout(function () {
        if (e.defaultPrevented && aiSubmission && aiSubmission.event === e) {
          restoreAIControls();
          if (pendingSubmission && pendingSubmission.event === e) pendingSubmission = null;
        }
      }, 0);
    } else {
      pendingSubmission = { event: e, confirmed: false };
    }
  });
  // Pages restored from the back/forward cache should not keep buttons stuck in the busy state.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    restoreAIControls();
    pendingSubmission = null;
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
    const submission = pendingSubmission && !pendingSubmission.event.defaultPrevented ? pendingSubmission : null;
    const submitted = submission && submission.event.target;
    pendingSubmission = null;
    // Exempt only the form being submitted. Keep it dirty if another form's warning cancels
    // navigation: the attempted save did not happen, and a later leave still needs a warning.
    if (!(submission && submission.confirmed) && Array.from(dirtyForms).some((form) => form !== submitted)) {
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
      // As answers.measure does on the server: UTF-16 units (like maxlength), and words split on
      // whitespace and em and en dashes.
      const size = words ? box.value.split(/[\s\u2013\u2014]+/).filter(Boolean).length : box.value.length;
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
    // Keep the current step visible in the narrow-screen step rail. This moves only
    // the rail, instantly, so page navigation and keyboard actions stay immediate.
    const steps = document.querySelector(".steps");
    const current = steps && steps.querySelector('[aria-current="step"]');
    if (current && steps.scrollWidth > steps.clientWidth) {
      steps.scrollLeft += current.getBoundingClientRect().left - steps.getBoundingClientRect().left
        - (steps.clientWidth - current.offsetWidth) / 2;
    }
    const summary = document.querySelector("[data-focus]");
    if (summary) summary.focus();
  });
})();
