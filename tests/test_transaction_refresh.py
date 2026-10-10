"""Reload dirty cached rows before a write can flush their obsolete JSON lists."""

from sqlalchemy.orm import Session

from app.services import answers, jobs, profile
from tests.synthetic import DEMO_JOB


def test_lock_reload_does_not_flush_dirty_cached_answers(session, sample_profile):
    profile.save(session, sample_profile)
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    first = answers.add_question(session, job, "Why this role?")
    second = answers.add_question(session, job, "Describe a project")
    answers.set_answer(session, job, job.application, first.id, "First answer")

    with Session(session.get_bind(), expire_on_commit=False) as stale:
        old_job = jobs.get(stale, job.id)
        assert [a.question_id for a in old_job.application.answers] == [first.id]
        answers.set_answer(session, job, job.application, second.id, "Second answer")

        # The writer must discard these pending edits, not autoflush them during
        # refresh and overwrite the answers saved by the other request.
        old_job.application.answers = []
        old_job.questions = [old_job.questions[1]]
        third = answers.add_question(stale, old_job, "Why this team?")

    session.expire_all()
    current = jobs.get(session, job.id)
    assert [q.id for q in current.questions] == [first.id, second.id, third.id]
    assert {a.question_id: a.text for a in current.application.answers} == {
        first.id: "First answer", second.id: "Second answer",
    }
