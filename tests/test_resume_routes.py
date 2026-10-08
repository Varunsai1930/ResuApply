"""Resume proposal review, explicit acceptance and accepted-only printing over HTTP."""

from __future__ import annotations

import copy
import re

from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Job
from app.services import resume as resume_service
from tests.conftest import BASE_URL, SAMPLE_JOB, tool_response
from tests.test_routes import client_profile, hidden, profile_form, review_and_save


def setup(client, profile):
    _, saved = review_and_save(client, profile_form(profile))
    assert saved.status_code == 303
    created = client.post("/jobs", data=SAMPLE_JOB, follow_redirects=False)
    assert created.status_code == 303
    return int(re.match(r"/jobs/(\d+)", created.headers["location"]).group(1))


def manual_proposal(client, job_id):
    response = client.post(f"/jobs/{job_id}/resume/profile", follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/jobs/{job_id}/resume/review"
    review = client.get(response.headers["location"])
    assert review.status_code == 200, review.text
    return review


def accept(client, job_id, review):
    return client.post(f"/jobs/{job_id}/resume/accept", data={
        "proposal_token": hidden(review.text, "proposal_token"),
    }, follow_redirects=False)


def approval(client, excluded=()):
    page = client.get("/profile/sharing").text
    included = re.findall(r'name="include" value="([^"]+)" checked', page)
    response = client.post("/profile/sharing", data={
        "base_hash": hidden(page, "base_hash"), "include": [i for i in included if i not in excluded],
    }, follow_redirects=False)
    assert response.status_code == 303


def draft(client):
    return resume_service.profile_draft(client_profile(client)).model_dump(mode="json")


def stored(client, job_id):
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        return job.application.current_proposal, job.application.accepted_package, job.application.review_state, job.application.tracking_status


def test_manual_resume_review_accept_print_without_ai(client, sample_profile):
    job_id = setup(client, sample_profile)
    workspace = client.get(f"/jobs/{job_id}").text
    assert "Use profile as-is" in workspace
    assert 'action="/jobs/%d/resume/generate"' % job_id not in workspace
    assert "Milestone 3" not in workspace

    review = manual_proposal(client, job_id)
    assert "Original profile bullet" in review.text and "Proposed bullet" in review.text
    assert "Coursework: Data Structures, Operating Systems, Databases" in review.text
    assert "exp-1-b1" in review.text and "Sample Analytics" in review.text
    assert "Project link: github.com/jordan-example/taskbot" in review.text
    assert "Selection and order" in review.text and "passed source and format checks" in review.text
    assert "confirm the meaning, scope and claims" in review.text
    assert client.get(f"/jobs/{job_id}/resume/print").status_code == 400
    assert accept(client, job_id, review).status_code == 303
    printed = client.get(f"/jobs/{job_id}/resume/print")
    assert printed.status_code == 200
    assert "jordan@example.com" in printed.text and "+1 555 0100" in printed.text
    assert "Software Engineering Intern" in printed.text and "Sample Analytics" in printed.text
    assert "2024-06 – 2024-08" in printed.text
    assert "Example State University" in printed.text and "B.S., Computer Science" in printed.text
    assert "Coursework: Data Structures, Operating Systems, Databases" in printed.text
    assert "<h2>Skills</h2>" in printed.text and "Print / save PDF" in printed.text
    assert SAMPLE_JOB["title"] not in printed.text and SAMPLE_JOB["company"] not in printed.text
    _, package, review_state, tracking_status = stored(client, job_id)
    assert package is not None and review_state == "draft" and tracking_status == "saved"
    assert "Accepted resume" in client.get(f"/jobs/{job_id}").text


def test_resume_routes_require_profile_and_existing_job(client):
    created = client.post("/jobs", data=SAMPLE_JOB, follow_redirects=False)
    job_id = int(re.match(r"/jobs/(\d+)", created.headers["location"]).group(1))
    assert client.post(f"/jobs/{job_id}/resume/profile").status_code == 400
    assert client.post(f"/jobs/{job_id}/resume/generate").status_code == 400
    assert client.get(f"/jobs/{job_id}/resume/review").status_code == 404
    assert client.get("/jobs/999/resume/print").status_code == 404


def test_ai_disabled_direct_generation_is_a_friendly_error(client, sample_profile):
    job_id = setup(client, sample_profile)
    response = client.post(f"/jobs/{job_id}/resume/generate")
    assert response.status_code == 400 and "AI is off" in response.text
    assert stored(client, job_id)[:2] == (None, None)


def test_accepting_old_tab_token_cannot_accept_replacement(client, sample_profile):
    job_id = setup(client, sample_profile)
    first = manual_proposal(client, job_id)
    manual_proposal(client, job_id)
    response = accept(client, job_id, first)
    assert response.status_code == 409
    assert "proposal changed since you reviewed it" in response.text.lower()
    assert stored(client, job_id)[1] is None
    assert client.post(f"/jobs/{job_id}/resume/accept").status_code == 409


def test_profile_revision_change_blocks_review_acceptance_and_print(client, sample_profile):
    job_id = setup(client, sample_profile)
    review = manual_proposal(client, job_id)
    assert accept(client, job_id, review).status_code == 303
    changed = client_profile(client)
    changed["contact"]["phone"] = "+1 555 0199"
    review_and_save(client, profile_form(changed))
    stale_review = client.get(f"/jobs/{job_id}/resume/review")
    assert "out of date" in stale_review.text
    assert 'name="proposal_token"' not in stale_review.text
    assert accept(client, job_id, review).status_code == 409
    stale_print = client.get(f"/jobs/{job_id}/resume/print")
    assert stale_print.status_code == 409 and "before printing" in stale_print.text
    fresh = manual_proposal(client, job_id)
    assert accept(client, job_id, fresh).status_code == 303
    assert "+1 555 0199" in client.get(f"/jobs/{job_id}/resume/print").text


def test_job_revision_change_blocks_print(client, sample_profile):
    job_id = setup(client, sample_profile)
    review = manual_proposal(client, job_id)
    accept(client, job_id, review)
    client.post(f"/jobs/{job_id}/edit", data=SAMPLE_JOB | {"description": "Updated job description."})
    assert client.get(f"/jobs/{job_id}/resume/print").status_code == 409


def test_new_proposal_keeps_accepted_resume_until_explicit_replacement(client, sample_profile):
    job_id = setup(client, sample_profile)
    first = manual_proposal(client, job_id)
    accept(client, job_id, first)
    before = stored(client, job_id)[1]
    manual_proposal(client, job_id)
    assert stored(client, job_id)[1] == before
    assert client.get(f"/jobs/{job_id}/resume/print").status_code == 200


def test_long_print_text_is_escaped_complete_and_has_multipage_styles(client, sample_profile):
    text = 'Built internal tools <script>alert("example")</script> & documented them. ' * 50
    sample_profile["experience"][0]["bullets"] = [text]
    sample_profile["contact"]["name"] = "Jordan <Example>"
    job_id = setup(client, sample_profile)
    review = manual_proposal(client, job_id)
    accept(client, job_id, review)
    printed = client.get(f"/jobs/{job_id}/resume/print")
    assert printed.status_code == 200
    assert "Jordan &lt;Example&gt;" in printed.text
    assert '<script>alert("example")</script>' not in printed.text
    assert printed.text.count("&lt;script&gt;alert") == 50
    css = client.get("/static/resume.css").text
    assert "size: A4" in css and "overflow-wrap: anywhere" in css
    assert "break-inside: auto" in css and "display: none !important" in css


def test_accepted_resume_survives_app_restart(client, settings, sample_profile):
    job_id = setup(client, sample_profile)
    accept(client, job_id, manual_proposal(client, job_id))
    with TestClient(create_app(settings), base_url=BASE_URL) as restarted:
        response = restarted.get(f"/jobs/{job_id}/resume/print")
        assert response.status_code == 200 and "Sample Analytics" in response.text


def test_ai_resume_requires_sharing_then_generates_review(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile)
    response = client.post(f"/jobs/{job_id}/resume/generate", follow_redirects=False)
    assert response.headers["location"] == f"/profile/sharing?next=/jobs/{job_id}"
    assert fake_ai.requests == []
    approval(client)
    proposal = draft(client)
    fake_ai.push(tool_response("return_resume", proposal))
    response = client.post(f"/jobs/{job_id}/resume/generate", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("/resume/review")
    review = client.get(response.headers["location"])
    assert "Prepared with AI" in review.text
    assert stored(client, job_id)[1] is None
    assert "jordan@example.com" not in fake_ai.sent_text()
    assert accept(client, job_id, review).status_code == 303


def test_ai_regeneration_and_failures_preserve_accepted_resume(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile)
    approval(client)
    fake_ai.push(tool_response("return_resume", draft(client)))
    client.post(f"/jobs/{job_id}/resume/generate")
    first = client.get(f"/jobs/{job_id}/resume/review")
    accept(client, job_id, first)
    accepted = stored(client, job_id)[1]
    replacement = copy.deepcopy(draft(client))
    replacement["experience"][0]["bullets"].reverse()
    fake_ai.push(tool_response("return_resume", replacement))
    response = client.post(f"/jobs/{job_id}/resume/generate", data={"force": "1"}, follow_redirects=False)
    assert response.status_code == 303
    assert stored(client, job_id)[1] == accepted
    assert client.get(f"/jobs/{job_id}/resume/print").status_code == 200
    before = stored(client, job_id)[0]
    fake_ai.push((429, {"error": {"message": "quota"}}))
    failure = client.post(f"/jobs/{job_id}/resume/generate", data={"force": "1"})
    assert failure.status_code == 502 and "rate limit" in failure.text
    assert stored(client, job_id)[:2] == (before, accepted)


def test_changed_sharing_invalidates_ai_proposal_but_preserves_accepted_print(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile)
    approval(client)
    fake_ai.push(tool_response("return_resume", draft(client)))
    client.post(f"/jobs/{job_id}/resume/generate")
    review = client.get(f"/jobs/{job_id}/resume/review")
    accept(client, job_id, review)
    approval(client, excluded={"proj-1-b2"})
    # The workspace no longer offers the (already accepted) proposal; its review page says it is stale.
    assert "This proposal is out of date" in client.get(f"/jobs/{job_id}/resume/review").text
    assert accept(client, job_id, review).status_code == 409
    assert client.get(f"/jobs/{job_id}/resume/print").status_code == 200


def test_changed_model_and_prompt_invalidate_ai_proposal(ai_app_client, fake_ai, sample_profile, monkeypatch):
    from app.ai import prompts

    client = ai_app_client
    job_id = setup(client, sample_profile)
    approval(client)
    fake_ai.push(tool_response("return_resume", draft(client)))
    client.post(f"/jobs/{job_id}/resume/generate")
    review = client.get(f"/jobs/{job_id}/resume/review")
    monkeypatch.setattr(prompts, "TAILOR_RESUME_REVISION", "tailor_resume/changed")
    assert accept(client, job_id, review).status_code == 409
    monkeypatch.undo()
    client.app.state.settings.openrouter_model = "another/model"
    assert "out of date" in client.get(f"/jobs/{job_id}/resume/review").text


def test_sharing_change_before_accept_writer_lock_is_refreshed_and_rejected(ai_app_client, fake_ai, sample_profile, monkeypatch):
    from app.routes import resume as resume_routes
    from app.services import outbound
    from app.services.profile import get_candidate

    client = ai_app_client
    job_id = setup(client, sample_profile)
    approval(client)
    fake_ai.push(tool_response("return_resume", draft(client)))
    client.post(f"/jobs/{job_id}/resume/generate")
    review = client.get(f"/jobs/{job_id}/resume/review")
    lock = resume_routes._lock_acceptance

    def change_sharing_then_lock(session, job, candidate):
        # Force an old approval to remain in this request's identity map.
        session.info["earlier_approval"] = outbound.get_approval(session, candidate)
        with client.app.state.session_factory() as other_session:
            other_candidate = get_candidate(other_session)
            sharing = outbound.state(other_session, client.app.state.settings, other_candidate)
            included = outbound.excludable_ids(sharing.base) - {"proj-1-b2"}
            outbound.approve(other_session, other_candidate, sharing.base_hash, included, {})
        lock(session, job, candidate)

    monkeypatch.setattr(resume_routes, "_lock_acceptance", change_sharing_then_lock)
    response = accept(client, job_id, review)
    assert response.status_code == 409
    assert "proposal is out of date" in response.text
    assert stored(client, job_id)[1] is None
    # A failed accept releases its writer lock; another approved choice can save.
    approval(client)


def test_untrusted_ai_fixed_facts_are_rejected_without_replacing_proposal(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile)
    manual_proposal(client, job_id)
    previous = stored(client, job_id)[0]
    approval(client)
    invalid = draft(client)
    invalid["experience"][0]["organization"] = "Invented Employer"
    fake_ai.push(tool_response("return_resume", invalid), tool_response("return_resume", invalid))
    response = client.post(f"/jobs/{job_id}/resume/generate")
    assert response.status_code == 502
    assert stored(client, job_id)[0] == previous
    assert "Invented Employer" not in client.get(f"/jobs/{job_id}/resume/review").text


def test_accepted_proposal_is_not_offered_for_review_again(client, sample_profile):
    job_id = setup(client, sample_profile)
    review = manual_proposal(client, job_id)
    assert "Ready to review" in client.get(f"/jobs/{job_id}").text
    accept(client, job_id, review)
    page = client.get(f"/jobs/{job_id}").text
    assert "Accepted resume" in page and "Print / save PDF" in page
    assert "Ready to review" not in page and "Review resume proposal" not in page
    manual_proposal(client, job_id)  # a new proposal is offered alongside the accepted one
    assert "Review resume proposal" in client.get(f"/jobs/{job_id}").text
