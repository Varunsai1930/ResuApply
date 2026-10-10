"""An accepted AI answer the edited profile no longer supports can be drafted again over HTTP (synthetic data only)."""

from __future__ import annotations

from tests.test_questions_routes import (
    BULLET, WHY_Q, add, approve_package, draft_reply, page_token, post, qid_of, ready_job, review_part, row,
    stored_job, workspace,
)
from tests.test_resume_routes import accept, approval, manual_proposal
from tests.test_routes import client_profile, profile_form, review_and_save

REPLACEMENT = "Wrote unit tests for the billing module"
EDITED_BULLET = "Built a Flask REST API in Python for internal reporting"


def unsupported_answer(client, fake_ai, profile, fresh_resume=True) -> tuple[int, str]:
    """An approved package whose accepted AI answer the profile then stops supporting (its metric is gone).

    With ``fresh_resume`` the resume is accepted again for the edited profile, which clears the approval.
    """
    job_id = ready_job(client, profile)
    approval(client)
    why = qid_of(add(client, job_id, WHY_Q))
    fake_ai.push(draft_reply({"question_id": why, "text": BULLET, "sources": ["exp-1-b1"]}))
    assert client.post(f"/jobs/{job_id}/questions/draft", follow_redirects=False).status_code == 303
    assert post(client, job_id, why, "draft/accept").status_code == 303
    assert approve_package(client, job_id).status_code == 303

    edited = client_profile(client)
    edited["experience"][0]["bullets"][0]["text"] = EDITED_BULLET
    review_and_save(client, profile_form(edited))
    approval(client)
    if fresh_resume:
        assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303
    return job_id, why


def draft_replacement(client, fake_ai, job_id, why):
    fake_ai.push(draft_reply({"question_id": why, "text": REPLACEMENT, "sources": ["exp-1-b3"]}))
    return client.post(f"/jobs/{job_id}/questions/draft", follow_redirects=False)


def answer_and_approval(client, job_id):
    _, app_, answers = stored_job(client, job_id)
    return [(a.text, a.origin) for a in answers], app_.approval


def test_an_unsupported_answer_can_be_drafted_again_beside_the_accepted_one(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id, why = unsupported_answer(client, fake_ai, sample_profile)
    page = workspace(client, job_id)
    assert "no longer supports this AI-drafted answer" in row(page, why)
    assert "Draft open answers with AI" in page  # the unsupported answer counts as one to draft
    before_token = page_token(workspace(client, job_id, "review"), "package_token")
    before = answer_and_approval(client, job_id)

    drafted = draft_replacement(client, fake_ai, job_id, why)
    assert drafted.status_code == 303
    sent = fake_ai.sent_text().rsplit("<application_questions>", 1)[1]
    assert WHY_Q in sent and why in sent

    page = workspace(client, job_id)
    shown = row(page, why)
    accepted_at, replacement_at = shown.index(BULLET), shown.index(REPLACEMENT)
    assert accepted_at < shown.index("Replacement AI draft") < replacement_at  # shown apart, accepted first
    assert "your accepted answer above stays until you accept this" in shown
    assert "Accept replacement" in shown and "Discard replacement" in shown
    assert 'name="draft_token"' in shown
    # Storing the replacement changed nothing that approval covers.
    assert answer_and_approval(client, job_id) == before
    review = workspace(client, job_id, "review")
    assert page_token(review, "package_token") == before_token
    assert "no longer supported by your profile" in review_part(review)
    assert "AI answer drafts waiting for your review" in review_part(review)


def test_accepting_the_replacement_swaps_the_answer_and_clears_approval(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id, why = unsupported_answer(client, fake_ai, sample_profile, fresh_resume=False)
    assert stored_job(client, job_id)[1].approval is not None  # recorded before the profile edit (now Stale)
    draft_replacement(client, fake_ai, job_id, why)

    accepted = post(client, job_id, why, "draft/accept")
    assert accepted.headers["location"] == f"/jobs/{job_id}?step=questions&msg=draft_accepted#question-{why}"
    answers, approval_now = answer_and_approval(client, job_id)
    assert answers == [(REPLACEMENT, "ai_draft")] and approval_now is None
    assert stored_job(client, job_id)[1].answer_drafts == []
    page = workspace(client, job_id)
    assert "no longer supports" not in row(page, why) and "Replacement AI draft" not in row(page, why)
    assert "Draft open answers with AI" not in page
    assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303
    assert approve_package(client, job_id).status_code == 303


def test_discarding_the_replacement_keeps_the_accepted_answer(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id, why = unsupported_answer(client, fake_ai, sample_profile)
    before = answer_and_approval(client, job_id)
    draft_replacement(client, fake_ai, job_id, why)

    discarded = post(client, job_id, why, "draft/discard")
    assert discarded.headers["location"] == f"/jobs/{job_id}?step=questions&msg=draft_discarded#question-{why}"
    assert answer_and_approval(client, job_id) == before
    assert stored_job(client, job_id)[1].answer_drafts == []
    shown = row(workspace(client, job_id), why)
    assert BULLET in shown and "Replacement AI draft" not in shown and "no longer supports" in shown


def test_a_provider_failure_keeps_the_accepted_answer(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id, why = unsupported_answer(client, fake_ai, sample_profile)
    before = answer_and_approval(client, job_id)
    fake_ai.push((500, {"error": {"message": "upstream down"}}), (500, {"error": {"message": "upstream down"}}))
    failed = client.post(f"/jobs/{job_id}/questions/draft")
    assert failed.status_code == 502
    assert answer_and_approval(client, job_id) == before
    assert stored_job(client, job_id)[1].answer_drafts == []
    assert BULLET in row(failed.text, why)
