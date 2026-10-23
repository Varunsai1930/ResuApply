"use strict";
document.addEventListener("click", function (event) {
  const exportControl = event.target.closest('[data-saved-export][aria-disabled="true"]');
  if (exportControl) { event.preventDefault(); return; }
  if (event.target.closest("[data-print-resume]")) window.print();
});

(function () {
  document.querySelectorAll('[data-resume-preview]').forEach((panel) => {
    const scope = panel.closest('#resume') || document;
    const form = scope.querySelector('[data-live-layout]');
    const html = panel.querySelector('[data-resume-html]');
    const documentPreview = html && html.querySelector('.resume-document');
    const pages = panel.querySelector('[data-resume-pages]');
    const status = panel.querySelector('[data-preview-status]');
    const retry = panel.querySelector('[data-preview-retry]');
    const checks = panel.querySelector('[data-preview-checks]');
    const saved = panel.dataset.savedLayout || '';
    let selected = saved;
    let pending = null;

    async function preview(layout) {
      if (pending) pending.abort();
      const request = new AbortController();
      pending = request;
      panel.hidden = false;
      panel.setAttribute('aria-busy', 'true');
      panel.classList.remove('has-page-preview');
      pages.hidden = true;
      retry.hidden = true;
      if (checks) checks.hidden = true;
      status.textContent = 'Preparing page preview…';
      const timeout = window.setTimeout(() => request.abort(), 45_000);
      try {
        const url = new URL(panel.dataset.previewUrl, window.location.href);
        if (layout) url.searchParams.set('layout', layout);
        url.searchParams.set('background', 'true');
        let response;
        let result;
        do {
          response = await fetch(url, { signal: request.signal, cache: 'no-store', headers: { Accept: 'application/json' } });
          result = await response.json();
          if (response.status === 202) await new Promise(resolve => window.setTimeout(resolve, 350));
        } while (response.status === 202 && !request.signal.aborted);
        if (request.signal.aborted) throw new DOMException('Preview interrupted', 'AbortError');
        if (!response.ok) throw new Error(result.detail || 'The page preview could not be prepared.');
        if (pending !== request) return;
        const fragment = document.createDocumentFragment();
        const images = [];
        result.pages.forEach((src, index) => {
          const figure = document.createElement('figure');
          figure.className = 'resume-preview-page';
          const image = document.createElement('img');
          image.alt = `Resume preview, page ${index + 1} of ${result.page_count}`;
          image.width = 1190;
          image.height = 1684;
          image.loading = index === 0 ? 'eager' : 'lazy';
          image.src = src;
          image.addEventListener('error', () => {
            if (pending !== request) return;
            panel.classList.remove('has-page-preview');
            pages.hidden = true;
            status.textContent = 'A preview page could not be loaded. Retry, or download Word.';
            retry.hidden = false;
          });
          const caption = document.createElement('figcaption');
          caption.textContent = `Page ${index + 1} of ${result.page_count}`;
          figure.append(image, caption);
          fragment.append(figure);
          images.push(image);
        });
        if (images[0]) await images[0].decode();
        if (pending !== request) return;
        pages.replaceChildren(fragment);
        pages.hidden = false;
        panel.classList.add('has-page-preview');
        if (checks && result.readability) {
          checks.hidden = false;
          checks.textContent = result.readability.warnings.length
            ? result.readability.warnings.join(' ')
            : 'Searchable text found; no text outside page boundaries detected. Review spacing and reading order in the preview.';
        }
        status.textContent = `${result.page_count} ${result.page_count === 1 ? 'page' : 'pages'} · A4 · 18 mm side margins`;
      } catch (error) {
        if (pending !== request) return;
        panel.classList.remove('has-page-preview');
        pages.hidden = true;
        status.textContent = error.name === 'AbortError'
          ? 'The preview took too long. Retry, or download Word.'
          : error.message || 'The page preview could not be prepared. You can still download Word.';
        retry.hidden = false;
      } finally {
        window.clearTimeout(timeout);
        if (pending === request) panel.removeAttribute('aria-busy');
      }
    }

    function select(layout) {
      selected = layout;
      // Change the HTML immediately while the exact PDF pages are prepared.
      if (documentPreview) {
        documentPreview.classList.remove('resume-layout-classic', 'resume-layout-modern', 'resume-layout-compact');
        documentPreview.classList.add('resume-layout-' + layout);
      }
      const unsaved = layout !== saved;
      if (form) {
        const notice = form.querySelector('[data-layout-status]');
        notice.hidden = !unsaved;
        notice.textContent = `Previewing ${layout[0].toUpperCase() + layout.slice(1)}. Apply layout to save this choice and enable downloads.`;
        form.querySelector('button[type="submit"]').disabled = !unsaved;
        scope.querySelectorAll('[data-saved-export]').forEach((control) => {
          if (unsaved) {
            control.setAttribute('aria-disabled', 'true');
            control.setAttribute('tabindex', '-1');
          } else {
            control.removeAttribute('aria-disabled');
            control.removeAttribute('tabindex');
          }
          if (control.tagName === 'BUTTON') control.disabled = unsaved;
        });
      }
      preview(layout);
    }

    retry.addEventListener('click', () => preview(selected));
    if (form) {
      form.addEventListener('change', (event) => {
        if (event.target.matches('input[name="layout"]')) select(event.target.value);
      });
      form.querySelector('button[type="submit"]').disabled = true;
    }
    if (form && panel.hasAttribute('data-preview-auto')) {
      // Gallery links are a preview only; saving still requires Apply layout.
      const requested = new URL(window.location.href).searchParams.get('preview_layout');
      const choice = Array.from(form.querySelectorAll('input[name="layout"]'))
        .find((input) => input.value === requested) || form.querySelector('input[name="layout"]:checked');
      if (choice) { choice.checked = true; select(choice.value); }
    } else if (panel.hasAttribute('data-preview-auto')) preview(saved);
  });
})();
