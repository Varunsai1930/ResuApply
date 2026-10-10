"""Regression coverage for the additional answer and question review findings."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.ai import answers as answers_ai
from app.ai.client import AIError
from app.db import utcnow
from app.models import AIRun, Job
from app.schemas.package import Answer, Question
from app.services import answers, jobs, outbound, package, profile, resume
from app.services.questions import classify, factual_value
from tests.conftest import SAMPLE_JOB, make_settings, tool_response
from tests.test_answers import accept, make_draft
from tests.test_package import approve
from tests.test_questions_routes import add, post, qid_of, ready_job, row, workspace


@pytest.fixture
def prepared(session, sample_profile):
    sample_profile["summary"] = "Built a separate reporting tool for 5,000 users with Git."
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    question = answers.add_question(session, job, "Why do you want this role?")
    return candidate, job, question


@pytest.mark.parametrize("text,problem", [
    ("Built an API serving 5,000 users", "numbers/years not in its sources: 5000"),
    ("Built an API with Git", "technologies not in its sources: Git"),
])
def test_uncited_summary_and_global_skills_cannot_support_claims(prepared, text, problem):
    candidate, job, _ = prepared
    assert problem in answers.validate_answer(candidate.profile, job, text, ["exp-1-b1"], None, "chars")


def test_explicitly_cited_summary_and_entry_technologies_are_allowed(prepared):
    candidate, job, _ = prepared
    assert answers.validate_answer(candidate.profile, job, "Built a tool for 5,000 users with Git",
                                   ["summary"], None, "chars") == []
    # Flask is recorded on this entry, though this individual bullet does not mention it.
    assert answers.validate_answer(candidate.profile, job, "Wrote unit tests in Flask",
                                   ["exp-1-b3"], None, "chars") == []
    assert answers.validate_answer(candidate.profile, job, "Built a Flask service",
                                   ["exp-1"], None, "chars") == []


@pytest.mark.parametrize("text", ["Built an API serving 5,000 users", "Built an API with Git"])
def test_ai_drafting_rejects_uncited_support(session, prepared, tmp_path, fake_ai, ai_client, text):
    candidate, job, question = prepared
    settings = make_settings(tmp_path, openrouter_model_trust="trusted")
    response = tool_response("return_answers", {"answers": [
        {"question_id": question.id, "text": text, "sources": ["exp-1-b1"]},
    ]})
    fake_ai.push(response, response)
    with pytest.raises(AIError) as exc:
        answers_ai.draft_answers(session, ai_client, settings, job, candidate)
    assert exc.value.kind == "invalid"
    assert len(fake_ai.requests) == 2
    assert job.application.answer_drafts == []
    assert session.query(AIRun).filter_by(operation="draft_answers").count() == 0


def test_exact_shared_validation_does_not_borrow_the_shared_summary(
    session, prepared, tmp_path, fake_ai, ai_client,
):
    candidate, job, question = prepared
    edited = candidate.profile.model_dump()
    edited["summary"] = "Built a different tool for 1,200 users."
    candidate = profile.save(session, edited).candidate
    settings = make_settings(tmp_path)
    state = outbound.state(session, settings, candidate)
    outbound.approve(session, candidate, state.base_hash, outbound.excludable_ids(state.base),
                     {"exp-1-b1": "Built a Flask REST API in Python for reporting"})
    response = tool_response("return_answers", {"answers": [
        {"question_id": question.id, "text": "Built a Flask API serving 1,200 users", "sources": ["exp-1-b1"]},
    ]})
    fake_ai.push(response, response)
    with pytest.raises(AIError) as exc:
        answers_ai.draft_answers(session, ai_client, settings, job, candidate)
    assert any("shared content: numbers/years not in its sources: 1200" in p for p in exc.value.problems)
    assert job.application.answer_drafts == []


def test_ai_drafting_accepts_cited_entry_technologies(session, prepared, tmp_path, fake_ai, ai_client):
    candidate, job, question = prepared
    settings = make_settings(tmp_path, openrouter_model_trust="trusted")
    fake_ai.push(tool_response("return_answers", {"answers": [
        {"question_id": question.id, "text": "Wrote unit tests in Flask", "sources": ["exp-1-b3"]},
    ]}))
    answers_ai.draft_answers(session, ai_client, settings, job, candidate)
    assert accept(session, job, question.id, candidate).text == "Wrote unit tests in Flask"


def test_draft_acceptance_rejects_an_unsafe_older_draft(session, prepared):
    candidate, job, question = prepared
    answers.replace_drafts(job.application, [make_draft(question.id, "Built an API serving 5,000 users")])
    session.commit()
    with pytest.raises(answers.AnswerError) as exc:
        accept(session, job, question.id, candidate)
    assert "numbers/years not in its sources: 5000" in exc.value.details
    assert job.application.answers == []
    assert len(job.application.answer_drafts) == 1


def test_reapproval_revalidates_accepted_answers_from_the_older_validator(session, prepared):
    candidate, job, question = prepared
    proposal = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
    resume.accept(session, job, candidate, resume.proposal_token(proposal))
    # Simulate an answer accepted before the stricter validator was installed.
    job.application.answers = [Answer(question_id=question.id, origin="ai_draft", sources=["exp-1-b1"],
                                      text="Built an API serving 5,000 users", at=utcnow())]
    session.commit()
    blockers, _ = package.check(job, job.application, candidate)
    assert any("no longer supported" in p and "5000" in p for p in blockers)
    assert [q.id for q in answers.draft_targets(job, job.application, candidate.profile)] == [question.id]
    with pytest.raises(package.PackageError):
        approve(session, job, candidate)


@pytest.mark.parametrize("question", [
    "Describe your email marketing experience",
    "Please describe your telephone support experience",
    "Describe a project you built at university",
    "Tell us about your GitHub projects",
    "What did you learn at school?",
    "Share an example of how you improved a website",
])
def test_narrative_questions_do_not_become_profile_fields(question):
    assert classify(question) == ("open", None)


@pytest.mark.parametrize("question", [
    "Can you commute to this office location?",
    "Are you able to work in our city?",
    "Which office location do you prefer?",
    "Can you start before graduation?",
    "Email marketing experience",
])
def test_ambiguous_field_mentions_require_manual_categorization(question):
    assert classify(question) == ("unknown", None)


@pytest.mark.parametrize("question,key", [
    ("Please provide your email address", "email"),
    ("Phone number", "phone"),
    ("Share your LinkedIn profile URL", "linkedin"),
    ("What is your current city?", "location"),
    ("Where are you currently based?", "location"),
    ("Which university do you attend?", "school"),
    ("Expected graduation date", "graduation_date"),
])
def test_explicit_field_requests_are_still_factual(question, key):
    assert classify(question) == ("factual", key)


@pytest.mark.parametrize("question,key", [
    ("Describe your email marketing experience", "email"),
    ("Describe a project you built at university", "school"),
    ("Can you commute to this office location?", "location"),
])
def test_legacy_keyword_mappings_no_longer_fill_the_wrong_value(prepared, question, key):
    candidate, _, _ = prepared
    legacy = Question(id="q99", text=question, category="factual", detected_category="factual", factual_key=key,
                      added_at=utcnow())
    assert factual_value(key, question, candidate.profile) is None
    resolved = answers.resolve_answer(legacy, None, None, candidate.profile)
    assert not resolved.resolved and resolved.value is None and resolved.text == ""


@pytest.mark.parametrize("question,key", [
    ("Mobile phone", "phone"),
    ("Cell phone number", "phone"),
    ("Phone Number (with country code)", "phone"),
    ("Phone number (required) *", "phone"),
    ("Full legal name", "name"),
    ("LinkedIn URL (optional)", "linkedin"),
    ("Expected graduation date (MM/YYYY)", "graduation_date"),
    ("What is your GPA? (out of 4.0)", "gpa"),
    ("Major / Field of study", "major"),
    ("Name of university", "school"),
    ("Portfolio / Website", "portfolio"),
    ("Location (City, State)", "location"),
    ("Current city and state", "location"),
])
def test_common_form_labels_and_format_hints_are_factual(question, key):
    assert classify(question) == ("factual", key)


@pytest.mark.parametrize("question", [
    "Name (of your reference)",
    "Email (of your manager)",
    "Graduation year",  # the profile stores a date, not a year: the user answers it
])
def test_other_parentheticals_and_formats_stay_unrecognized(question):
    assert classify(question) == ("unknown", None)


def test_saved_factual_question_with_a_format_hint_keeps_its_profile_value(prepared):
    candidate, _, _ = prepared
    question = "Phone Number (with country code)"
    assert factual_value("phone", question, candidate.profile) == candidate.profile.contact.phone


def test_sensitive_detection_keeps_precedence_over_narratives():
    assert classify("Describe your age and email marketing experience") == ("sensitive", None)
    assert classify("Tell us about your work authorization in Canada") == ("sensitive_factual", "authorization")


@pytest.mark.parametrize("kind", ["sensitive", "user", "ai", "unsupported_ai", "profile"])
def test_answered_optional_questions_keep_the_skip_action(client, sample_profile, kind):
    job_id = ready_job(client, sample_profile)
    question = {"sensitive": "What is your gender?", "profile": "Email"}.get(kind, "Why do you want this role?")
    qid = qid_of(add(client, job_id, question, required=False))
    if kind in ("sensitive", "user"):
        assert post(client, job_id, qid, "answer", text="My optional answer").status_code == 303
    elif kind in ("ai", "unsupported_ai"):
        with client.app.state.session_factory() as session:
            job = session.get(Job, job_id)
            job.application.answers = [Answer(
                question_id=qid, origin="ai_draft", sources=["exp-1-b1"], at=utcnow(),
                text="Built an API serving " + ("1,200" if kind == "ai" else "5,000") + " users",
            )]
            session.commit()
    shown = row(workspace(client, job_id), qid)
    assert "Skip this optional question" in shown
    assert post(client, job_id, qid, "skip").status_code == 303
    shown = row(workspace(client, job_id), qid)
    assert "Skipped (optional)" in shown
    assert "Skip this optional question" not in shown
    with client.app.state.session_factory() as session:
        answer = next(a for a in session.get(Job, job_id).application.answers if a.question_id == qid)
        assert answer.skipped and answer.text == "" and answer.sources == []


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed to exercise the browser script")
@pytest.mark.parametrize("text", ["😀", "a😀", "e\u0301", "👨‍👩‍👧‍👦"])
def test_browser_character_counter_matches_server_codepoints(text):
    script = r"""
const fs = require('fs');
const vm = require('vm');
const handlers = {};
const box = {id: 'answer', value: process.argv[2], matches: () => false, closest: () => null};
const counter = {dataset: {limit: String(Array.from(box.value).length), unit: 'chars'},
  textContent: '', classList: {toggle: (name, active) => { counter.overLimit = active; }}};
const document = {documentElement: {}, addEventListener: (name, fn) => {
  (handlers[name] ||= []).push(fn);
}, querySelectorAll: selector => selector.startsWith('[data-counter-for=') ? [counter] : []};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document, window: {addEventListener: () => {}}, MutationObserver: class {observe() {}},
});
for (const fn of handlers.input) fn({target: box});
process.stdout.write(JSON.stringify({label: counter.textContent, overLimit: counter.overLimit}));
"""
    browser_script = Path(__file__).resolve().parents[1] / "app/static/forms.js"
    result = subprocess.run([shutil.which("node"), "-e", script, str(browser_script), text],
                            capture_output=True, check=True, text=True)
    observed = json.loads(result.stdout)
    assert observed == {"label": f"{len(text)} of {len(text)} characters", "overLimit": False}
    assert answers.check_length(text, len(text), "chars") is None
