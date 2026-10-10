"""The Job Workspace as five guided steps: each step's status, the one next action, and the pages."""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest

from app.models import Job
from app.services import package
from app.services.profile import get_candidate
from app.services.status import Status
from app.services.workspace import Step, next_action
from tests.conftest import SAMPLE_JOB
from tests.test_assessment_routes import setup as job_with_requirements
from tests.test_questions_routes import (
    AUTH_Q, GENDER_Q, WHY_Q, add, approve_package, complete_package, post, qid_of, record, workspace,
)
from tests.test_resume_routes import accept, manual_proposal
from tests.test_routes import profile_form, review_and_save


def guide_for(client, job_id):
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        return next_action(job, job.application, get_candidate(session))


def shown(guide):
    """Each step as (status, note), in order."""
    return {s.step: (s.meaning.status, s.meaning.note) for s in guide.steps}


def accept_resume(client, job_id):
    assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303


def new_job(client) -> int:
    created = client.post("/jobs", data=SAMPLE_JOB, follow_redirects=False)
    return int(re.match(r"/jobs/(\d+)", created.headers["location"]).group(1))


# ---------------------------------------------------------------- next_action

def test_without_a_profile_everything_waits_for_it(client):
    guide = guide_for(client, new_job(client))
    steps = shown(guide)
    assert steps[Step.REQUIREMENTS] == (Status.NEEDS_YOU, "Add the job's requirements")
    for step in (Step.RESUME, Step.QUESTIONS, Step.REVIEW):
        assert steps[step] == (Status.BLOCKED, "Create your profile first")
    assert steps[Step.TRACK] == (Status.INFO, "You submit on the employer's site")
    assert (guide.next.label, guide.next.href) == ("Create your profile", "/profile/edit")


def test_requirements_are_done_once_saved_whatever_the_gaps(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    guide = guide_for(client, job_id)
    # The demo requirements include an Unmet one and Unknowns: still Done, with the counts.
    assert shown(guide)[Step.REQUIREMENTS] == (Status.DONE, "4 met, 1 unmet, 2 unknown")
    assert guide.next.step is Step.RESUME and guide.next.label == "Prepare a resume"


def test_a_job_without_requirements_asks_for_them_but_blocks_nothing(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile, requirements=False)
    guide = guide_for(client, job_id)
    assert shown(guide)[Step.REQUIREMENTS] == (Status.NEEDS_YOU, "Add the job's requirements")
    assert guide.next.step is Step.REQUIREMENTS and guide.next.label == "Add the requirements"
    assert guide.next.href == f"/jobs/{job_id}?step=requirements#requirements"
    assert shown(guide)[Step.RESUME][0] is Status.NEEDS_YOU  # not Blocked: requirements never block


def test_review_is_blocked_until_the_steps_that_stop_approval(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    add(client, job_id, WHY_Q)
    steps = shown(guide_for(client, job_id))
    assert steps[Step.RESUME] == (Status.NEEDS_YOU, "Prepare a resume")
    assert steps[Step.QUESTIONS] == (Status.NEEDS_YOU, "1 of 1 to finish")
    assert steps[Step.REVIEW] == (Status.BLOCKED, "until steps 2 and 3")

    manual_proposal(client, job_id)
    guide = guide_for(client, job_id)
    assert shown(guide)[Step.RESUME] == (Status.NEEDS_YOU, "Review and accept")
    assert guide.next.label == "Review and accept the resume"

    accept_resume(client, job_id)
    guide = guide_for(client, job_id)
    assert shown(guide)[Step.RESUME] == (Status.DONE, "Accepted")
    assert shown(guide)[Step.REVIEW] == (Status.BLOCKED, "until step 3")
    assert guide.next.step is Step.QUESTIONS and guide.next.label == "Finish the questions"


def test_an_unanswered_optional_question_needs_you_but_never_blocks(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    accept_resume(client, job_id)
    gender = qid_of(add(client, job_id, GENDER_Q, required=False))
    why = qid_of(add(client, job_id, "Anything else you'd like to share?", required=False))
    assert post(client, job_id, gender, "skip").status_code == 303
    steps = shown(guide_for(client, job_id))
    assert steps[Step.QUESTIONS] == (Status.NEEDS_YOU, "1 of 2 to finish")
    assert steps[Step.REVIEW] == (Status.NEEDS_YOU, "Review and approve")  # a warning, not a blocker
    assert post(client, job_id, why, "skip").status_code == 303
    assert shown(guide_for(client, job_id))[Step.QUESTIONS] == (Status.DONE, "2 answered")


def test_no_questions_is_info_and_the_next_action_moves_on(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    accept_resume(client, job_id)
    guide = guide_for(client, job_id)
    assert shown(guide)[Step.QUESTIONS] == (Status.INFO, "None added; add any the form asks")
    assert guide.next.step is Step.REVIEW and guide.next.label == "Review and approve"


def test_after_approval_the_next_action_is_to_record_applied_then_to_track(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    accept_resume(client, job_id)
    complete_package(client, job_id)
    assert approve_package(client, job_id).status_code == 303
    guide = guide_for(client, job_id)
    assert shown(guide)[Step.REVIEW] == (Status.DONE, "Approved")
    assert shown(guide)[Step.TRACK] == (Status.NEEDS_YOU, "Submit it, then record Applied")
    assert (guide.next.step, guide.next.label) == (Step.TRACK, "Record that you applied")

    assert record(client, job_id, on="2026-10-01").status_code == 303
    guide = guide_for(client, job_id)
    assert shown(guide)[Step.TRACK] == (Status.DONE, "Applied 2026-10-01")
    assert (guide.next.step, guide.next.label) == (Step.TRACK, "Update the status")


def test_a_stale_approval_needs_you_again(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    accept_resume(client, job_id)
    complete_package(client, job_id)
    approve_package(client, job_id)
    edited = dict(sample_profile, summary="A different summary.")
    review_and_save(client, profile_form(edited))
    steps = shown(guide_for(client, job_id))
    # The profile edit leaves the accepted resume out of date, so approving again waits on step 2.
    assert steps[Step.RESUME] == (Status.NEEDS_YOU, "Out of date; prepare a fresh one")
    assert steps[Step.REVIEW] == (Status.BLOCKED, "until step 2")


def test_answer_blocker_is_the_rule_package_check_uses(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    accept_resume(client, job_id)
    add(client, job_id, WHY_Q)
    add(client, job_id, AUTH_Q)
    add(client, job_id, GENDER_Q, required=False)
    add(client, job_id, "Anything else you'd like to share?", required=False)
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        candidate = get_candidate(session)
        items = package._resolved_answers(job, job.application, candidate)
        blockers, _ = package.check(job, job.application, candidate)
        found = [package.answer_blocker(i) for i in items]
    assert [b is not None for b in found] == [True, True, True, False]  # only the optional open one is a warning
    assert [b for b in found if b] == blockers


# ---------------------------------------------------------------- the pages

def steps_nav(html: str) -> str:
    return re.search(r'<nav aria-label="Steps".*?</nav>', html, re.S).group(0)


def test_the_workspace_opens_on_the_next_action_with_one_section(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    page = client.get(f"/jobs/{job_id}").text
    nav = steps_nav(page)
    assert re.findall(r'href="/jobs/\d+\?step=(\w+)"', nav) == [s.value for s in Step]
    assert re.findall(r'aria-current="step"', nav) == ['aria-current="step"']
    assert re.search(r'step=resume" class="step-link here" aria-current="step"', nav)
    assert "Done · 4 met, 1 unmet, 2 unknown" in nav and 'class="step-status st-done"' in nav
    assert "Blocked · until step 2" in nav and 'class="step-status st-blocked"' in nav
    assert 'id="resume-title"' in page
    for other in ("requirements-title", "questions-title", "review-title"):
        assert f'id="{other}"' not in page
    assert page.count('class="btn-primary" href=') == 1
    assert f'<a class="btn-primary" href="/jobs/{job_id}?step=resume#resume">Prepare a resume' in page


@pytest.mark.parametrize("step, heading", [
    ("requirements", 'id="requirements-title"'), ("resume", 'id="resume-title"'), ("questions", 'id="questions-title"'),
    ("review", 'id="review-title"'), ("track", 'id="tracking"'),
])
def test_each_step_shows_its_own_section(client, sample_profile, step, heading):
    job_id = job_with_requirements(client, sample_profile)
    page = client.get(f"/jobs/{job_id}?step={step}").text
    assert heading in page
    assert f'step={step}" class="step-link here" aria-current="step"' in page
    assert "Job description" in page  # the description is on every step


def test_an_unknown_step_is_a_bad_link(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    response = client.get(f"/jobs/{job_id}?step=submit")
    assert response.status_code == 400


def test_another_step_shows_where_the_next_action_is(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    page = client.get(f"/jobs/{job_id}?step=questions").text
    assert f'href="/jobs/{job_id}?step=resume#resume">Prepare a resume' in page
    assert "Next: step 2, Resume" in page


def test_a_refused_form_reopens_its_own_step(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    refused = client.post(f"/jobs/{job_id}/status", data={"status": "saved"})
    assert refused.status_code == 422 and "already Saved" in refused.text
    assert 'step=track" class="step-link here" aria-current="step"' in refused.text
    note = client.post(f"/jobs/{job_id}/notes", data={"text": " "})
    assert note.status_code == 422 and 'step=track" class="step-link here"' in note.text


def test_review_step_lists_what_you_are_approving(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    accept_resume(client, job_id)
    complete_package(client, job_id)
    review = workspace(client, job_id, "review")
    table = review[review.index("What you're approving"):]
    assert "Resume accepted" in table and f'href="/jobs/{job_id}/resume/print"' in table
    assert "I like building Python services." in table and "User answer" in table
    assert "Skipped (optional)" in table and 'class="st st-info"' in table
    assert "jordan@example.com" in table and 'class="st st-done"' in table


def test_review_step_never_shows_an_unconfirmed_value_as_the_answer(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    accept_resume(client, job_id)
    add(client, job_id, AUTH_Q)  # sensitive: the profile value is only an answer once confirmed
    review = workspace(client, job_id, "review")
    table = review[review.index("What you're approving"):]
    assert "Not answered yet" in table and "User input required" in table
    assert "<td class=\"answer-cell\">Yes" not in table


def test_side_notes_show_the_latest_and_link_to_the_track_step(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    client.post(f"/jobs/{job_id}/notes", data={"text": "First note"})
    client.post(f"/jobs/{job_id}/notes", data={"text": "Recruiter: Sam Example"})
    page = client.get(f"/jobs/{job_id}?step=requirements").text
    start = page.index('id="side-notes-title"')
    side = page[start:page.index("</section>", start)]
    assert "Recruiter: Sam Example" in side and "First note" not in side
    assert f'href="/jobs/{job_id}?step=track#notes"' in side


# ---------------------------------------------------------------- returning from the sharing approval

def test_sharing_returns_to_the_step_that_asked_for_it(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    preview = client.get(f"/profile/sharing?next={quote(f'/jobs/{job_id}?step=questions')}").text
    assert f'href="/jobs/{job_id}?step=questions#questions">← Back to the job' in preview
    base_hash = re.search(r'name="base_hash" value="([^"]*)"', preview).group(1)
    included = re.findall(r'name="include" value="([^"]+)" checked', preview)
    approved = client.post("/profile/sharing", data={"base_hash": base_hash, "include": included,
                                                     "next": f"/jobs/{job_id}?step=questions"}, follow_redirects=False)
    assert approved.headers["location"] == f"/jobs/{job_id}?step=questions&msg=sharing_approved#questions"


@pytest.mark.parametrize("next_url", [
    "/jobs/1?step=submit", "/jobs/1?step=questions&x=1", "/jobs/1?msg=x", "/jobs/1?step=track\n", "//evil.example",
])
def test_sharing_refuses_any_other_return_link(client, sample_profile, next_url):
    job_with_requirements(client, sample_profile)
    preview = client.get(f"/profile/sharing?next={quote(next_url)}").text
    assert "Back to the job" not in preview and 'name="next" value=""' in preview
