from datetime import date

from app.models import Job
from app.services import board, jobs
from tests.test_jobs_board import new_job
from tests.test_routes import hidden
from tests.test_resume_export_routes import accepted_job
from tests.test_package import approve
from app.services.profile import get_candidate


def plan(client, job_id, **values):
    page = client.get(f'/jobs/{job_id}?step=track')
    token = hidden(page.text, 'plan_token')
    return client.post(f'/jobs/{job_id}/plan', data={'plan_token': token, 'priority': 'normal', 'deadline': ''} | values, follow_redirects=False)


def test_plan_preserves_status_and_approval_and_rejects_stale_forms(client):
    job_id = new_job(client)
    old = hidden(client.get(f'/jobs/{job_id}?step=track').text, 'plan_token')
    assert plan(client, job_id, priority='high', deadline='2026-10-10').status_code == 303
    with client.app.state.session_factory() as session:
        app = session.get(Job, job_id).application
        assert app.priority == 'high' and app.deadline == date(2026, 10, 10)
        assert app.tracking_status == 'saved' and app.approval is None
    stale = client.post(f'/jobs/{job_id}/plan', data={'plan_token': old, 'priority': 'low', 'deadline': ''})
    assert stale.status_code == 409 and 'changed in another tab' in stale.text
    assert plan(client, job_id, deadline='bad date').status_code == 422
    assert plan(client, job_id, priority='invented').status_code == 422


def test_board_sorts_deadlines_and_priorities_and_keeps_search(client):
    first, second, third = [new_job(client, title=title) for title in ('Backend A', 'Backend B', 'Frontend C')]
    plan(client, first, priority='high', deadline='2026-10-10')
    plan(client, second, priority='low', deadline='2026-10-09')
    with client.app.state.session_factory() as session:
        items = jobs.list_all(session)
        result = board.board(items, None, date(2026, 10, 11), sort='deadline')
        assert [c.job.id for c in result.columns[0].cards] == [second, first, third]
        assert all(c.deadline_due for c in result.columns[0].cards[:2])
        result = board.board(items, None, date(2026, 10, 11), 'Backend', 'priority')
        assert [c.job.id for c in result.columns[0].cards] == [first, second]
    page = client.get('/jobs?q=Backend&sort=priority').text
    assert 'value="priority" selected' in page and 'High priority' in page


def test_planning_changes_keep_an_approved_package_approved(client, sample_profile):
    job_id = accepted_job(client, sample_profile)
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        approve(session, job, get_candidate(session))
        approval = job.application.approval
    assert plan(client, job_id, priority='high', deadline='2026-12-01').status_code == 303
    with client.app.state.session_factory() as session:
        app = session.get(Job, job_id).application
        assert app.approval == approval and app.review_state == 'approved'
