"""Recommended actions open useful controls while the existing guide owns readiness."""

import re

from app.models import Job
from tests.test_draft_recovery import editor  # noqa: F401 (fixture)
from tests.test_workspace_steps import (
    accept_resume, add, approve_package, complete_package, job_with_requirements,
    manual_proposal, new_job, qid_of, WHY_Q,
)


def action(client, job_id, step):
    page = client.get(f'/jobs/{job_id}?step={step}')
    assert page.status_code == 200
    target = re.search(r'data-next-action[^>]*href="([^"]+)"', page.text).group(1)
    assert 'id="next-action-hint"' in page.text
    return target, page.text


def test_prepare_and_review_actions_open_the_right_resume_controls(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    target, text = action(client, job_id, 'resume')
    assert target == '#resume-actions' and 'id="resume-actions" tabindex="-1"' in text
    manual_proposal(client, job_id)
    target, _ = action(client, job_id, 'resume')
    assert target == f'/jobs/{job_id}/resume/review'
    assert client.get(target).status_code == 200


def test_missing_profile_and_cross_step_navigation_keep_the_guide_target(client, sample_profile):
    target, _ = action(client, new_job(client), 'resume')
    assert target == '/profile/edit'
    job_id = job_with_requirements(client, sample_profile)
    target, _ = action(client, job_id, 'review')
    assert target == f'/jobs/{job_id}?step=resume#resume'


def test_requirements_action_opens_the_editor(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile, requirements=False)
    target, _ = action(client, job_id, 'requirements')
    assert target == f'/jobs/{job_id}/requirements/edit'
    assert client.get(target).status_code == 200


def test_questions_review_and_tracking_actions_do_not_submit_anything(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    accept_resume(client, job_id)
    question_id = qid_of(add(client, job_id, WHY_Q))
    assert action(client, job_id, 'questions')[0] == '#questions'
    assert client.post(f'/jobs/{job_id}/questions/{question_id}/answer',
                       data={'text': 'I enjoy building Python services.', 'origin': 'user'},
                       follow_redirects=False).status_code == 303
    complete_package(client, job_id)
    assert action(client, job_id, 'review')[0] == '#review-contents'
    approve_package(client, job_id)
    assert action(client, job_id, 'track')[0] == '#status'
    with client.app.state.session_factory() as session:
        assert session.get(Job, job_id).application.tracking_status == 'saved'


def test_same_step_action_keeps_unsaved_answer_and_does_not_reload(client, sample_profile, editor):
    job_id = job_with_requirements(client, sample_profile)
    accept_resume(client, job_id)
    question_id = qid_of(add(client, job_id, WHY_Q))
    page, expect, requests = editor
    page.goto(f'http://127.0.0.1:8000/jobs/{job_id}?step=questions')
    answer = page.locator(f'#answer-{question_id}')
    answer.fill('An answer I am still editing')
    before = len([entry for entry in requests if entry[0] == 'GET' and entry[1] == f'/jobs/{job_id}'])
    page.locator('[data-next-action]').click()
    expect(answer).to_have_value('An answer I am still editing')
    assert page.url.endswith('#questions')
    assert len([entry for entry in requests if entry[0] == 'GET' and entry[1] == f'/jobs/{job_id}']) == before
