"""The answer bank's search, sort and usage counts."""

from __future__ import annotations

import re

from app.services import answers as answer_service
from tests.test_jobs_board import new_job
from tests.test_questions_routes import add, post, qid_of, ready_job

WHY_Q = "Why do you want this role?"
PROJECT_Q = "Describe a project you're proud of"
HEARD_Q = "How did you hear about this position?"


def saved(client, job_id, question, text):
    qid = qid_of(add(client, job_id, question))
    assert post(client, job_id, qid, "answer", text=text).status_code == 303
    assert post(client, job_id, qid, "bank").status_code == 303


def reuse(client, entry_id, question, text):
    job_id = new_job(client)
    qid = qid_of(add(client, job_id, question))
    assert post(client, job_id, qid, "answer", text=text, origin="bank", bank_id=str(entry_id)).status_code == 303


def listing(client, query="", sort="recent"):
    with client.app.state.session_factory() as session:
        return [(e.question, use.jobs) for e, use in answer_service.bank_listing(session, query, sort)]


def bank_with_three(client, sample_profile):
    job_id = ready_job(client, sample_profile)
    saved(client, job_id, WHY_Q, "I like building Python services.")  # entry 1
    saved(client, job_id, PROJECT_Q, "I built a campus events app with a Python backend.")  # entry 2
    saved(client, job_id, HEARD_Q, "Through my university's career fair.")  # entry 3
    reuse(client, 1, WHY_Q, "I like building Python services.")
    reuse(client, 3, HEARD_Q, "Through my university's career fair.")
    reuse(client, 3, HEARD_Q, "Through my university's career fair.")


def test_usage_counts_the_jobs_that_reused_each_answer(client, sample_profile):
    bank_with_three(client, sample_profile)
    assert dict(listing(client)) == {WHY_Q: 1, PROJECT_Q: 0, HEARD_Q: 2}
    page = client.get("/answers").text
    assert "Used for 2 jobs" in page and "Used for 1 job<" in page and "Not reused yet" in page


def test_the_three_sorts(client, sample_profile):
    bank_with_three(client, sample_profile)
    assert [q for q, _ in listing(client, sort="most_used")] == [HEARD_Q, WHY_Q, PROJECT_Q]
    assert [q for q, _ in listing(client, sort="saved")] == [HEARD_Q, PROJECT_Q, WHY_Q]  # newest saved first
    assert [q for q, _ in listing(client, sort="recent")] == [HEARD_Q, WHY_Q, PROJECT_Q]  # last reuse, else save
    page = client.get("/answers", params={"sort": "most_used"}).text
    assert '<option value="most_used" selected>Most used</option>' in page
    assert page.index(HEARD_Q) < page.index(WHY_Q) < page.index("Describe a project")


def test_an_unknown_sort_is_a_bad_link(client):
    assert client.get("/answers", params={"sort": "alphabetical"}).status_code == 400


def test_search_matches_every_word_in_the_question_or_answer(client, sample_profile):
    bank_with_three(client, sample_profile)
    assert [q for q, _ in listing(client, "python")] == [WHY_Q, PROJECT_Q]
    assert [q for q, _ in listing(client, "CAREER fair")] == [HEARD_Q]
    assert listing(client, "python fair") == []
    page = client.get("/answers", params={"q": "  campus   events "}).text
    assert "1 of 3 saved answers match “campus events”" in page and 'value="campus events"' in page
    assert re.findall(r"<h4>([^<]+)</h4>", page) == ["Describe a project you&#39;re proud of"]
    none = client.get("/answers", params={"q": "<b>nothing</b>"}).text
    assert "No saved answers match" in none and "<b>nothing" not in none and "&lt;b&gt;nothing" in none


def test_no_controls_on_an_empty_bank(client):
    page = client.get("/answers").text
    assert "No saved answers yet" in page and 'id="bank-search"' not in page


def test_a_deleted_entrys_reuses_never_count_for_a_new_entry(client, sample_profile):
    from datetime import timedelta

    from app.db import utcnow
    from app.models import AnswerBankEntry

    bank_with_three(client, sample_profile)  # entry 3 (the newest) is reused by two jobs
    assert client.post("/answers/3/delete").status_code == 200
    with client.app.state.session_factory() as session:
        # As an older database without AUTOINCREMENT would: the deleted entry's ID is given again.
        session.add(AnswerBankEntry(id=3, question="Anything else?", answer="No.", category="open",
                                    created_at=utcnow() + timedelta(seconds=1), updated_at=utcnow() + timedelta(seconds=1)))
        session.commit()
    assert dict(listing(client))["Anything else?"] == 0
    assert listing(client, sort="most_used")[0] == (WHY_Q, 1)  # the only answer actually reused leads


def test_new_databases_never_reuse_an_entry_id(client, sample_profile):
    job_id = ready_job(client, sample_profile)
    saved(client, job_id, WHY_Q, "I like building Python services.")
    client.post("/answers/1/delete")
    saved(client, job_id, PROJECT_Q, "I built a campus events app.")
    with client.app.state.session_factory() as session:
        from app.models import AnswerBankEntry

        assert [e.id for e in session.query(AnswerBankEntry)] == [2]
