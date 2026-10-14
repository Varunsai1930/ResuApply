"""Unsaved workspace edits and exact textarea values survive the browser form boundary."""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from app.models import Job
from app.templating import templates
from tests.test_profile_sections import form_fields
from tests.test_resume_routes import accept, manual_proposal, setup
from tests.test_routes import hidden, job_review


def textarea_value(page: str, field_id: str) -> str:
    """Apply HTML's textarea rule: the parser discards one LF immediately after the tag."""
    match = re.search(rf'<textarea\b[^>]*\bid="{re.escape(field_id)}"[^>]*>(.*?)</textarea>',
                      page, re.S)
    assert match, field_id
    text = unescape(match.group(1))
    return text[1:] if text.startswith("\n") else text


class EditableForms(HTMLParser):
    def __init__(self, page: str):
        super().__init__()
        self.forms: list[dict] = []
        self.current = None
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"attrs": attrs, "editable": False}
            self.forms.append(self.current)
        elif self.current and (tag in {"textarea", "select"} or
                               tag == "input" and attrs.get("type", "text") not in {"hidden", "submit", "button"}):
            self.current["editable"] = True

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


@pytest.mark.parametrize("description", [
    "About the role\nWe need Python.",
    "\nAbout the role\nWe need Python.",
    "\n\nAbout the role\nWe need Python.",
    "\nRead <requirements> & qualifications.\nWe need Python.",
])
def test_unchanged_job_editor_keeps_description_revision_and_approval(client, sample_profile, description):
    job_id = setup(client, sample_profile)
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        job.description = description
        session.commit()
    assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303
    review = client.get(f"/jobs/{job_id}?step=review").text
    assert client.post(f"/jobs/{job_id}/approve", data={
        "package_token": hidden(review, "package_token"),
    }, follow_redirects=False).status_code == 303

    page = client.get(f"/jobs/{job_id}/edit").text
    browser_description = textarea_value(page, "description")
    assert browser_description == description
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        revision = job.revision
        fields = {name: getattr(job, name) for name in ("title", "company", "location", "url")}
    response = client.post(f"/jobs/{job_id}/edit", data={
        **fields, **job_review(client, job_id), "description": browser_description,
    }, follow_redirects=False)
    assert response.headers["location"].endswith("msg=job_unchanged")
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        assert job.description == description and job.revision == revision
        assert job.application.review_state == "approved"
    assert client.get(f"/jobs/{job_id}/resume/print").status_code == 200


@pytest.mark.parametrize("text", ["", "First line", "\nFirst line", "\n\nFirst line", "\n</textarea><script>unsafe</script>&"])
def test_shared_profile_textareas_preserve_exact_values_and_escape_markup(text):
    page = templates.env.from_string('''{% import "macros.html" as m %}
<form action="/profile/review">
{{ m.textarea('summary', 'Summary', text) }}
{{ m.bullet_row('experience-0', '0', {'text': text}) }}</form>''').render(text=text)
    assert textarea_value(page, "summary") == text
    assert textarea_value(page, "experience-0-bullets-0-text") == text
    assert form_fields(page)["summary"] == text
    assert "<script>" not in page


def test_every_editable_workspace_form_has_an_unsaved_changes_guard(client, sample_profile):
    job_id = setup(client, sample_profile)
    for question in ("Why do you want this role?", "Your preferred team?", "What is your email?"):
        assert client.post(f"/jobs/{job_id}/questions", data={"text": question, "required": "1"},
                           follow_redirects=False).status_code == 303
    assert client.post(f"/jobs/{job_id}/requirements", data={
        "base_revision": "1", "req-0-id": "", "req-0-text": "Python experience",
        "req-0-category": "skill", "req-0-importance": "required", "req-0-excerpt": "Python",
        "req-0-ctype": "",
    }, follow_redirects=False).status_code == 303
    assert client.post(f"/jobs/{job_id}/status", data={"status": "applied"},
                       follow_redirects=False).status_code == 303

    count = 0
    for url in (f"/jobs/{job_id}?step=questions", f"/jobs/{job_id}?step=requirements",
                f"/jobs/{job_id}?step=track", "/jobs/new", "/profile/edit", "/profile/sharing"):
        response = client.get(url)
        assert response.status_code == 200
        for form in EditableForms(response.text).forms:
            if form["editable"] and form["attrs"].get("method") == "post":
                assert "data-guard" in form["attrs"], (url, form["attrs"])
                count += 1
    assert count >= 10  # answers, categories, question entry, evidence, override, tracking and notes


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js exercises the existing browser script")
def test_unsaved_guard_tracks_inputs_and_keeps_edits_after_canceled_or_prevented_submits():
    script = r'''
const fs = require('fs'), vm = require('vm');
const documentHandlers = {}, windowHandlers = {};
const document = {documentElement: {}, querySelectorAll: () => [], addEventListener(name, fn) {
  (documentHandlers[name] ||= []).push(fn);
}};
const window = {addEventListener(name, fn) { (windowHandlers[name] ||= []).push(fn); }};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document, window, MutationObserver: class {observe() {}},
});
const formA = {matches: () => false}, formB = {matches: () => false};
const dispatch = (name, target) => {
  const event = {target, defaultPrevented: false};
  for (const fn of documentHandlers[name]) fn(event);
  return event;
};
const box = form => ({id: '', matches: () => false, closest: () => form});
const leaving = () => {
  const event = {warned: false, preventDefault() {this.warned = true;}};
  for (const fn of windowHandlers.beforeunload) fn(event);
  return event.warned;
};
dispatch('input', box(formA));
const afterTyping = leaving();
dispatch('change', box(formB));  // e.g. a category, evidence checkbox or tracking select
dispatch('submit', formA);
const afterSavingOne = leaving();
const afterCancelingSave = leaving();
// A second script may prevent the submit. It must not silence the unsaved-change warning.
dispatch('submit', formB).defaultPrevented = true;
console.log(JSON.stringify({afterTyping, afterSavingOne, afterCancelingSave, afterPreventedSave: leaving()}));
'''
    result = subprocess.run([shutil.which("node"), "-e", script,
                             str(Path(__file__).resolve().parents[1] / "app/static/forms.js")],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {
        "afterTyping": True, "afterSavingOne": True, "afterCancelingSave": True, "afterPreventedSave": True,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js exercises the existing browser script")
def test_ai_navigation_cancel_preserves_controls_and_confirmed_request_stays_locked():
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const documentHandlers = {}, windowHandlers = {};
const button = (html, disabled = false) => ({
  innerHTML: html, disabled, dataset: {busyLabel: 'Working'}, attrs: {},
  set textContent(text) {this.innerHTML = text;},
  getAttribute(name) {return this.attrs[name] ?? null;},
  setAttribute(name, value) {this.attrs[name] = value;},
  removeAttribute(name) {delete this.attrs[name];},
});
const first = button('<span>Draft answers</span>'), second = button('Suggest evidence');
const unavailable = button('Unavailable', true);
const aiForm = b => ({dataset: {}, matches: () => true, querySelector: () => b});
const aiA = aiForm(first), aiB = aiForm(second), dirty = {matches: () => false};
let confirm = false, confirmations = 0;
const document = {documentElement: {},
  querySelectorAll: selector => selector === 'form[data-ai] button' ? [first, second, unavailable] : [],
  addEventListener(name, fn) { (documentHandlers[name] ||= []).push(fn); },
};
const window = {
  confirm() {confirmations++; return confirm;},
  addEventListener(name, fn) { (windowHandlers[name] ||= []).push(fn); },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document, window, queueMicrotask, MutationObserver: class {observe() {}},
});
const dispatch = (name, target) => {
  const event = {target, defaultPrevented: false, preventDefault() {this.defaultPrevented = true;}};
  for (const fn of documentHandlers[name]) fn(event);
  return event;
};
const leaving = () => {
  const event = {warned: false, preventDefault() {this.warned = true;}};
  for (const fn of windowHandlers.beforeunload) fn(event);
  return event.warned;
};
const restored = () => !first.disabled && !second.disabled && unavailable.disabled &&
  first.innerHTML === '<span>Draft answers</span>' && !first.getAttribute('aria-busy') && !aiA.dataset.sent;
(async () => {
  dispatch('input', {id: '', matches: () => false, closest: () => dirty});
  const canceled = dispatch('submit', aiA);
  assert(canceled.defaultPrevented && restored());
  assert(leaving()); // Canceling the AI action still protects the unsaved answer.

  confirm = true;
  dispatch('submit', aiA);
  assert(first.disabled && second.disabled && unavailable.disabled && aiA.dataset.sent);
  assert.strictEqual(first.innerHTML, 'Working');
  assert.strictEqual(first.getAttribute('aria-busy'), 'true');
  assert(dispatch('submit', aiA).defaultPrevented);
  assert(dispatch('submit', aiB).defaultPrevented);
  assert(!leaving()); // The accepted confirmation is used once, with no second native prompt.
  await Promise.resolve();
  assert(first.disabled && aiA.dataset.sent); // A slow real request must remain protected.

  for (const fn of windowHandlers.pageshow) fn({persisted: true});
  assert(restored());
  assert(leaving()); // Back/forward cache restoration retains the unsaved edits.

  const prevented = dispatch('submit', aiA);
  prevented.preventDefault(); // A later submit listener canceled the navigation.
  await Promise.resolve();
  assert(restored() && leaving());
  assert.strictEqual(confirmations, 3);
  console.log(JSON.stringify({passed: true}));
})().catch(error => {console.error(error); process.exit(1);});
'''
    result = subprocess.run([shutil.which("node"), "-e", script,
                             str(Path(__file__).resolve().parents[1] / "app/static/forms.js")],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"passed": True}
