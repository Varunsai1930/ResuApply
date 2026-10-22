/* Keep the profile outline in step with the content, without changing scroll history. */
(function () {
  "use strict";
  const nav = document.querySelector('.profile-jump');
  if (!nav) return;
  const entries = Array.from(nav.querySelectorAll('a[href^="#"]'), (link) => ({
    link, section: document.getElementById(link.hash.slice(1)),
  })).filter((entry) => entry.section);
  if (!entries.length) return;

  const header = document.querySelector('.site-header');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let active = null;
  let queued = false;

  function highlight(entry) {
    if (entry === active) return;
    active = entry;
    entries.forEach(({ link }) => {
      if (link === entry.link) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    // The mobile outline scrolls horizontally. Reveal its active item without
    // scrollIntoView(), which would also pull the document back to the outline.
    const item = entry.link.getBoundingClientRect();
    const panel = nav.getBoundingClientRect();
    if (nav.scrollWidth > nav.clientWidth && (item.left < panel.left || item.right > panel.right)) {
      nav.scrollTo({
        left: nav.scrollLeft + item.left - panel.left - (nav.clientWidth - item.width) / 2,
        behavior: reducedMotion.matches ? 'auto' : 'smooth',
      });
    }
  }

  function update() {
    queued = false;
    const readingLine = (header ? header.getBoundingClientRect().bottom : 0) + 32;
    let current = entries[0];
    entries.forEach((entry) => {
      if (entry.section.getBoundingClientRect().top <= readingLine) current = entry;
    });
    // The final section may be too short to reach the reading line.
    if (window.scrollY > 0 && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) {
      current = entries[entries.length - 1];
    }
    highlight(current);
  }

  function scheduleUpdate() {
    if (queued) return;
    queued = true;
    window.requestAnimationFrame(update);
  }

  entries.forEach(({ link, section }) => {
    link.addEventListener('click', (event) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (window.location.hash !== link.hash) window.history.pushState(null, '', link.hash);
      const keyboard = event.detail === 0;
      if (keyboard) {
        section.setAttribute('tabindex', '-1');
        section.focus({ preventScroll: true });
      }
      section.scrollIntoView({ block: 'start', behavior: reducedMotion.matches || keyboard ? 'auto' : 'smooth' });
      scheduleUpdate();
    });
  });

  window.addEventListener('scroll', scheduleUpdate, { passive: true });
  window.addEventListener('resize', scheduleUpdate);
  window.addEventListener('hashchange', scheduleUpdate);
  window.addEventListener('popstate', () => {
    // Back can arrive while a smooth click scroll is still running. Restore the
    // anchor after the browser's history restoration, cancelling that old scroll.
    window.requestAnimationFrame(() => {
      const restored = entries.find(({ link }) => link.hash === window.location.hash);
      if (restored) restored.section.scrollIntoView({ block: 'start', behavior: 'instant' });
      scheduleUpdate();
    });
  });
  window.addEventListener('pageshow', scheduleUpdate);
  window.addEventListener('load', scheduleUpdate);
  // Expanding source references can move the following sections without scrolling.
  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(scheduleUpdate);
    observer.observe(document.querySelector('.profile-document'));
  }
  update();
})();
