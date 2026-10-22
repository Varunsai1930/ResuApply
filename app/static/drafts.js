/* Local recovery for explicitly opted-in editors. Stored data is never trusted HTML. */
(function () {
  "use strict";
  const prefix = 'resuapply:draft:v1:';
  const lifetime = 7 * 24 * 60 * 60 * 1000;
  const maxBytes = 1024 * 1024;
  const safeKey = /^[a-zA-Z0-9]{1,80}$/;

  function fields(form) {
    return Array.from(form.querySelectorAll('input, textarea, select')).filter((field) =>
      field.name && !field.disabled && !['file', 'password', 'submit', 'button'].includes(field.type) &&
      (field.type !== 'hidden' || /^(education|experience|projects|certifications)-[a-zA-Z0-9]+(?:-bullets-[a-zA-Z0-9]+)?-id$/.test(field.name)));
  }
  function values(form) {
    return fields(form).map((field) => [field.name, field.value, !!field.checked]);
  }
  function groups(form) {
    return Array.from(form.querySelectorAll('[data-add][data-target]')).map((button) => {
      const target = document.getElementById(button.dataset.target);
      if (!target || !form.contains(target)) return null;
      const start = target.id.endsWith('-list') ? target.id.slice(0, -5) : button.dataset.prefix + '-bullets';
      const keys = Array.from(target.children).filter((child) => child.matches('[data-item]')).map((child) => {
        const field = child.querySelector('[name]');
        return field && field.name.startsWith(start + '-') ? field.name.slice(start.length + 1).split('-')[0] : '';
      });
      return [target.id, keys];
    }).filter(Boolean);
  }
  function restoreGroups(form, saved) {
    // Build parent rows before their nested bullet rows, always from current trusted templates.
    const ordered = saved.slice().sort((a, b) => Number(b[0].endsWith('-list')) - Number(a[0].endsWith('-list')));
    ordered.forEach(([id, keys]) => {
      const button = Array.from(form.querySelectorAll('[data-add][data-target]')).find((item) => item.dataset.target === id);
      const target = document.getElementById(id);
      const template = button && document.getElementById(button.dataset.add);
      if (!button || !target || !template || !form.contains(target)) return;
      target.replaceChildren();
      keys.forEach((key) => {
        const markup = template.innerHTML.replaceAll('__i__', key).replaceAll('__p__', button.dataset.prefix || '');
        target.insertAdjacentHTML('beforeend', markup);
      });
    });
  }
  function valid(record) {
    return record && record.version === 1 && Number.isFinite(record.saved) &&
      record.saved <= Date.now() && Date.now() - record.saved < lifetime &&
      typeof record.baseline === 'string' && Array.isArray(record.values) && record.values.length <= 4000 &&
      record.values.every((item) => Array.isArray(item) && item.length === 3 && typeof item[0] === 'string' &&
        typeof item[1] === 'string' && typeof item[2] === 'boolean') &&
      Array.isArray(record.groups) && record.groups.length <= 1000 && record.groups.every((group) =>
        Array.isArray(group) && group.length === 2 && typeof group[0] === 'string' && Array.isArray(group[1]) &&
        group[1].length <= 500 && new Set(group[1]).size === group[1].length &&
        group[1].every((key) => typeof key === 'string' && safeKey.test(key)));
  }

  document.querySelectorAll('form[data-draft]').forEach((form) => {
    const key = prefix + form.dataset.draft;
    const initial = JSON.stringify(values(form));
    // Revision guards are compared, but their values are never restored or stored separately.
    const revision = form.dataset.draftRevision || '';
    const baseline = JSON.stringify([revision, form.dataset.draftBaseline || initial]);
    const savedForm = form.dataset.draftSaved !== 'false';
    let pending = null;
    let timer;
    let changed = false;
    let available = true;
    const bar = document.createElement('div');
    bar.className = 'draft-recovery';
    const status = document.createElement('p');
    status.setAttribute('role', 'status');
    status.textContent = 'Edits are backed up in this browser for 7 days. Review and save when ready.';
    const restore = document.createElement('button');
    restore.type = 'button'; restore.className = 'btn-small'; restore.textContent = 'Restore draft'; restore.hidden = true;
    const discard = document.createElement('button');
    discard.type = 'button'; discard.className = 'btn-small'; discard.textContent = 'Discard saved draft'; discard.hidden = true;
    const download = document.createElement('button');
    download.type = 'button'; download.className = 'btn-small'; download.textContent = 'Download older draft'; download.hidden = true;
    bar.append(status, restore, download, discard);
    form.prepend(bar);

    function remove() {
      try { localStorage.removeItem(key); } catch (_) { available = false; }
      pending = null;
      restore.hidden = download.hidden = discard.hidden = true;
    }
    try {
      const raw = localStorage.getItem(key);
      if (raw && raw.length <= maxBytes) {
        let record;
        try { record = JSON.parse(raw); } catch (_) { record = null; }
        if (valid(record)) {
          if (JSON.stringify(record.values) === initial && savedForm) remove();
          else if (JSON.stringify(record.values) === initial && record.baseline === baseline) {
            changed = true;
            discard.hidden = false;
            status.textContent = 'Your local draft is shown below. Review and save to update your profile.';
          }
          else {
            pending = record;
            discard.hidden = false;
            const same = record.baseline === baseline;
            restore.hidden = !same;
            download.hidden = same;
            status.textContent = same ? 'You have an unsaved local draft. Restore it or discard it before editing.'
              : 'An older local draft is available. The saved form has changed; download the draft to recover its text.';
          }
        } else remove();
      } else if (raw) remove();
    } catch (_) {
      available = false;
      status.textContent = 'Local draft recovery is unavailable in this browser. Keep this page open until you save.';
    }
    function save() {
      clearTimeout(timer);
      if (!changed || pending || !available) return;
      const current = values(form);
      if (savedForm && JSON.stringify(current) === initial && JSON.stringify(groups(form)) === initialGroups) { remove(); return; }
      const record = JSON.stringify({version: 1, saved: Date.now(), baseline, values: current, groups: groups(form)});
      try {
        if (record.length > maxBytes) throw new Error('Draft is too large');
        localStorage.setItem(key, record);
        status.textContent = 'Draft saved in this browser. Review and save to update your application.';
        discard.hidden = false;
      } catch (_) {
        status.textContent = 'The latest edits could not be backed up. Keep this page open until you save.';
      }
    }
    const initialGroups = JSON.stringify(groups(form));
    function schedule() { changed = true; clearTimeout(timer); timer = setTimeout(save, 500); }
    form.addEventListener('input', schedule);
    form.addEventListener('change', schedule);
    form.addEventListener('click', (event) => {
      if (event.target.closest('[data-add], [data-remove]')) schedule();
    });
    form.addEventListener('submit', save);
    window.addEventListener('pagehide', save);
    document.addEventListener('visibilitychange', () => { if (document.hidden) save(); });
    restore.addEventListener('click', () => {
      if (!pending || (changed && !window.confirm('Replace the edits on this page with the saved draft?'))) return;
      const record = pending;
      restoreGroups(form, record.groups);
      const saved = new Map(record.values.map((item) => [item[0], item]));
      fields(form).forEach((field) => {
        const item = saved.get(field.name);
        if (!item) return;
        field.value = item[1];
        if (['checkbox', 'radio'].includes(field.type)) field.checked = item[2];
        field.dispatchEvent(new Event('input', {bubbles: true}));
      });
      pending = null;
      restore.hidden = download.hidden = true;
      changed = true;
      status.textContent = 'Draft restored. Check the fields, then review and save.';
      save();
    });
    discard.addEventListener('click', () => {
      clearTimeout(timer);
      remove();
      changed = false;
      status.textContent = 'Local backup discarded. Your saved application has not changed.';
    });
    download.addEventListener('click', () => {
      if (!pending) return;
      const text = pending.values.map(([name, value]) => name + ': ' + value).join('\n\n');
      const url = URL.createObjectURL(new Blob([text], {type: 'text/plain;charset=utf-8'}));
      const link = document.createElement('a');
      link.href = url; link.download = 'resuapply-recovered-draft.txt'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  });
})();
