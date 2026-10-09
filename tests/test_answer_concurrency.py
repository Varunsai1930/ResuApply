"""Overlapping requests must not lose each other's questions or answers (synthetic data only).

Each request has its own session. A session that loaded the job earlier holds an older copy of
the question and answer lists; every change must start from the committed lists instead.
"""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy.orm import Session

from app.services import answers as svc
from app.services import jobs as job_service
from app.services import profile as profile_service
from app.services import transactions
from app.services.answers import AnswerError
from tests.synthetic import DEMO_JOB, SAMPLE_PROFILE


@pytest.fixture
def job(session):
    profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE))
    created = job_service.create(session, job_service.clean_input(**DEMO_JOB))
    svc.add_question(session, created, "Why do you want this role?")
    svc.add_question(session, created, "Describe a project you are proud of")
    return created


def other_session(session) -> Session:
    return Session(session.get_bind(), expire_on_commit=False)


def stored(session, job_id):
    session.expire_all()
    job = job_service.get(session, job_id)
    return job, {a.question_id: a.text for a in job.application.answers}


def test_a_stale_session_keeps_questions_another_session_added(session, job):
    with other_session(session) as stale:
        stale_job = job_service.get(stale, job.id)
        assert [q.id for q in stale_job.questions] == ["q1", "q2"]
        first = svc.add_question(session, job, "What excites you about our team?")
        second = svc.add_question(stale, stale_job, "Tell us about a challenge you overcame")
    assert (first.id, second.id) == ("q3", "q4")
    saved, _ = stored(session, job.id)
    assert [q.id for q in saved.questions] == ["q1", "q2", "q3", "q4"] and saved.question_counter == 4


def test_a_stale_session_keeps_unrelated_answers(session, job):
    with other_session(session) as stale:
        stale_job = job_service.get(stale, job.id)
        assert stale_job.application.answers == []
        svc.set_answer(session, job, job.application, "q1", "Because of the team.")
        svc.set_answer(stale, stale_job, stale_job.application, "q2", "A chat bot for clubs.")
        svc.add_question(stale, stale_job, "Anything else?", required=False)
    saved, answers = stored(session, job.id)
    assert answers == {"q1": "Because of the team.", "q2": "A chat bot for clubs."}
    assert [q.id for q in saved.questions] == ["q1", "q2", "q3"]


def test_a_stale_session_keeps_removals_and_never_reuses_their_ids(session, job):
    svc.set_answer(session, job, job.application, "q2", "Original.")
    with other_session(session) as stale:
        stale_job = job_service.get(stale, job.id)
        svc.remove_question(session, job, job.application, "q2")
        with pytest.raises(AnswerError, match="no question 'q2'"):
            svc.set_answer(stale, stale_job, stale_job.application, "q2", "Answered on an old page.")
        svc.set_answer(stale, stale_job, stale_job.application, "q1", "Still here.")
        added = svc.add_question(stale, stale_job, "What excites you about our team?")
    assert added.id == "q3"
    saved, answers = stored(session, job.id)
    assert [q.id for q in saved.questions] == ["q1", "q3"]
    assert answers == {"q1": "Still here."}


def test_a_stale_session_cannot_bring_back_an_answer_it_still_holds(session, job):
    svc.set_answer(session, job, job.application, "q1", "First.")
    with other_session(session) as stale:
        stale_job = job_service.get(stale, job.id)
        svc.set_category(session, job, job.application, "q1", "sensitive")  # drops q1's answer
        assert [a.question_id for a in stale_job.application.answers] == ["q1"]  # its old copy
        svc.set_answer(stale, stale_job, stale_job.application, "q2", "Second.")
    saved, answers = stored(session, job.id)
    assert answers == {"q2": "Second."} and saved.questions[0].category == "sensitive"


def run_together(session, monkeypatch, *actions):
    """Run each action in its own session and thread; both reach the writer lock at the same moment."""
    barrier = Barrier(len(actions))
    lock = transactions.lock_and_reload

    def together(*args, **kwargs):
        barrier.wait(timeout=5)
        return lock(*args, **kwargs)

    monkeypatch.setattr(transactions, "lock_and_reload", together)

    def run(action):
        with other_session(session) as own:
            return action(own, job_service.get(own, action.job_id))

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(run, actions))


def action(job_id, fn):
    fn.job_id = job_id
    return fn


def test_simultaneous_additions_both_survive_with_distinct_increasing_ids(session, job, monkeypatch):
    ids = run_together(
        session, monkeypatch,
        action(job.id, lambda s, j: svc.add_question(s, j, "What excites you about our team?").id),
        action(job.id, lambda s, j: svc.add_question(s, j, "Tell us about a challenge you overcame").id),
    )
    assert sorted(ids) == ["q3", "q4"]
    saved, _ = stored(session, job.id)
    assert [q.id for q in saved.questions] == ["q1", "q2", "q3", "q4"] and saved.question_counter == 4
    assert {q.text for q in saved.questions[2:]} == {"What excites you about our team?",
                                                    "Tell us about a challenge you overcame"}


def test_simultaneous_answers_and_additions_all_survive(session, job, monkeypatch):
    run_together(
        session, monkeypatch,
        action(job.id, lambda s, j: svc.set_answer(s, j, j.application, "q1", "Because of the team.")),
        action(job.id, lambda s, j: svc.set_answer(s, j, j.application, "q2", "A chat bot for clubs.")),
        action(job.id, lambda s, j: svc.add_question(s, j, "Anything else?", required=False)),
    )
    saved, answers = stored(session, job.id)
    assert answers == {"q1": "Because of the team.", "q2": "A chat bot for clubs."}
    assert [q.id for q in saved.questions] == ["q1", "q2", "q3"]
