"""How many accepted resumes and answers cite each profile item (services/citations.py)."""

from __future__ import annotations

from app.db import utcnow
from app.schemas.package import Answer
from app.services import jobs, profile
from app.services.citations import Usage, citations, resume_sources
from tests.synthetic import DEMO_JOB
from tests.test_package import accept_resume


def make_job(session, title="Backend Intern"):
    return jobs.create(session, jobs.clean_input(**(DEMO_JOB | {"title": title})))


def test_usage_text():
    assert Usage().text == "not cited yet"
    assert Usage(resumes=1).text == "cited in 1 resume"
    assert Usage(resumes=2, answers=1).text == "cited in 2 resumes and 1 answer"
    assert Usage(answers=3).text == "cited in 3 answers"


def test_each_accepted_resume_and_answer_counts_once(session, sample_profile):
    candidate = profile.save(session, sample_profile).candidate
    first, second, unused = make_job(session), make_job(session, "Data Intern"), make_job(session, "Other Intern")
    record = accept_resume(session, first, candidate)
    accept_resume(session, second, candidate)
    cited = resume_sources(record.resume)
    assert "exp-1-b1" in cited
    first.application.answers = [
        Answer(question_id="q1", text="Built it", origin="ai_draft", sources=["exp-1-b1", "exp-1-b1"], at=utcnow()),
        Answer(question_id="q2", text="", origin="user", skipped=True, sources=["exp-1-b2"], at=utcnow()),
    ]
    session.commit()
    counts = citations([first, second, unused])
    assert counts["exp-1-b1"] == Usage(resumes=2, answers=1)  # a source listed twice in one answer counts once
    assert counts["exp-1-b2"].answers == 0  # a skipped answer cites nothing
    assert set(counts) >= cited and all(counts[s].resumes == 2 for s in cited)


def test_the_profile_shows_how_often_each_bullet_is_cited(client, sample_profile):
    from tests.test_resume_routes import accept, manual_proposal, setup

    job_id = setup(client, sample_profile)
    page = client.get("/profile").text
    assert "exp-1-b1</span> <span class=\"cite-count\">not cited yet</span>" in page
    assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303
    page = client.get("/profile").text
    assert "exp-1-b1</span> <span class=\"cite-count\">cited in 1 resume</span>" in page
