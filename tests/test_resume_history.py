from app.models import Job
from tests.test_resume_routes import setup, manual_proposal, accept, stored
from tests.test_routes import profile_form, review_and_save


def test_history_compares_frozen_facts_and_restores_as_unaccepted_proposal(client, sample_profile):
    job = setup(client, sample_profile)
    assert 'No saved versions yet' in client.get(f'/jobs/{job}/resume/history').text
    assert accept(client, job, manual_proposal(client, job)).status_code == 303
    with client.app.state.session_factory() as session:
        old = session.get(Job, job).application.resume_versions[0]
        version = old.id
    sample_profile['contact']['location'] = 'New location'
    review_and_save(client, profile_form(sample_profile))
    assert accept(client, job, manual_proposal(client, job)).status_code == 303
    before = stored(client, job)
    page = client.get(f'/jobs/{job}/resume/history?version={version}')
    assert page.status_code == 200
    assert 'Austin, TX' in page.text and 'New location' in page.text
    result = client.post(f'/jobs/{job}/resume/history/{version}/restore', follow_redirects=False)
    assert result.status_code == 303 and result.headers['location'].endswith('/resume/review')
    assert stored(client, job)[1:] == before[1:]
    with client.app.state.session_factory() as session:
        assert len(session.get(Job, job).application.resume_versions) == 2


def test_history_rejects_deleted_sources_and_other_job_versions(client, sample_profile):
    job = setup(client, sample_profile)
    accept(client, job, manual_proposal(client, job))
    with client.app.state.session_factory() as session:
        version = session.get(Job, job).application.resume_versions[0].id
    sample_profile['experience'] = []
    review_and_save(client, profile_form(sample_profile))
    response = client.post(f'/jobs/{job}/resume/history/{version}/restore')
    assert response.status_code == 409 and 'no longer matches your profile' in response.text
    assert client.get(f'/jobs/{job}/resume/history?version=missing').status_code == 404
    assert client.post(f'/jobs/{job}/resume/history/missing/restore').status_code == 404


def test_history_keeps_twenty_versions_and_deduplicates_acceptance(client, sample_profile):
    job_id = setup(client, sample_profile)
    from app.services import resume, profile
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        candidate = profile.get_candidate(session)
        for _ in range(22):
            record = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
            resume.accept(session, job, candidate, resume.proposal_token(record))
        resume.accept(session, job, candidate, resume.proposal_token(record))
        assert len(job.application.resume_versions) == 20
        assert job.application.resume_versions[-1].id == resume.proposal_token(record)
