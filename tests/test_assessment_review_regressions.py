"""Reviewed evidence, overlapping checklist saves and job-local suggestion caching."""

from __future__ import annotations

import copy
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from html import unescape

import pytest
from sqlalchemy.orm import Session

from app.ai import operations
from app.models import AIRun, Job
from app.services import checklist
from app.services import jobs as jobs_service
from app.services import profile as profile_service
from app.services.requirements import RequirementsConflict, set_requirements
from tests.conftest import DEMO_JOB, DEMO_REQUIREMENTS, SAMPLE_PROFILE, tool_response
from tests.test_assessment_routes import card, create_job, editor_fields, evidence_token, requirement_form, setup
from tests.test_routes import client_profile, profile_form, review_and_save


def _form(page, action, source=None, bulk=False):
    for match in re.finditer(r'<form[^>]+action="([^"]+)"[^>]*>(.*?)</form>', page, re.DOTALL):
        if match.group(1) != action or (bulk and 'class="stack"' not in match.group(0)):
            continue
        fields = {name: unescape(value) for name, value in
                  re.findall(r'<input[^>]+type="hidden"[^>]+name="([^"]+)" value="([^"]*)"', match.group(2))}
        if source is None or fields.get("source", fields.get("sources")) == source:
            return fields
    raise AssertionError(f"No reviewed form for {action}")


def _approve_sharing(client):
    preview = client.get("/profile/sharing").text
    response = client.post("/profile/sharing", data={
        "base_hash": re.search(r'name="base_hash" value="([^"]+)"', preview).group(1),
        "include": re.findall(r'name="include" value="([^"]+)" checked', preview),
    }, follow_redirects=False)
    assert response.status_code == 303


def _suggest(client, fake_ai, job_id):
    fake_ai.push(tool_response("return_evidence_suggestions", {"suggestions": [
        {"requirement_id": "r4", "source_ids": ["exp-1-b1"], "reason": "REST API experience"},
    ]}))
    assert client.post(f"/jobs/{job_id}/evidence/suggest", follow_redirects=False).status_code == 303


def _saved_job(client, job_id):
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        return (job.revision, job.requirement_counter, job.requirements, job.evidence, job.overrides,
                job.application.approval, job.application.submitted_snapshots)


@pytest.mark.parametrize("action", ["link", "reconfirm", "accept"])
@pytest.mark.parametrize("change", ["source", "source_removed", "requirement", "requirement_removed"])
def test_old_evidence_forms_do_not_confirm_unreviewed_content(ai_app_client, fake_ai, sample_profile, action, change):
    client = ai_app_client
    job_id = setup(client, sample_profile)
    route = f"/jobs/{job_id}/requirements/r4/evidence"
    if action == "reconfirm":
        assert client.post(route, data={"sources": "exp-1-b1", "review_token": evidence_token(client, job_id, "r4")},
                           follow_redirects=False).status_code == 303
        profile = client_profile(client)
        profile["experience"][0]["bullets"][0]["text"] = "Built another reporting REST API"
        review_and_save(client, profile_form(profile))
    elif action == "accept":
        _approve_sharing(client)
        _suggest(client, fake_ai, job_id)
        route = f"/jobs/{job_id}/requirements/r4/suggestions/accept"
    page = card(client.get(f"/jobs/{job_id}").text, "r4")
    fields = _form(page, route, source="exp-1-b1" if action != "link" else None, bulk=action == "link")
    if action == "link":
        fields["sources"] = "exp-1-b1"
    if change.startswith("source"):
        profile = client_profile(client)
        if change == "source_removed":
            profile["experience"][0]["bullets"].pop(0)
        else:
            profile["experience"][0]["bullets"][0]["text"] = "Answered customer-service telephone calls"
        review_and_save(client, profile_form(profile))
    else:
        fields_now = editor_fields(client.get(f"/jobs/{job_id}/requirements/edit").text)
        if change == "requirement_removed":
            fields_now = {key: value for key, value in fields_now.items() if not key.startswith("req-3-")}
        else:
            fields_now["req-3-text"] = "Customer service experience"
        assert client.post(f"/jobs/{job_id}/requirements", data=fields_now, follow_redirects=False).status_code == 303
    before = _saved_job(client, job_id)
    rejected = client.post(route, data=fields, follow_redirects=False)
    assert rejected.status_code == 409, rejected.text
    assert _saved_job(client, job_id) == before
    assert "Review the current content" in rejected.text


@pytest.mark.parametrize("action", ["link", "accept"])
@pytest.mark.parametrize("token", [None, "incorrect"])
def test_confirmation_requires_a_reviewed_token(client, sample_profile, action, token):
    job_id = setup(client, sample_profile)
    suffix = "evidence" if action == "link" else "suggestions/accept"
    field = "sources" if action == "link" else "source"
    fields = {field: "exp-1-b1"}
    if token is not None:
        fields["review_token"] = token
    before = _saved_job(client, job_id)
    rejected = client.post(f"/jobs/{job_id}/requirements/r4/{suffix}", data=fields, follow_redirects=False)
    assert rejected.status_code == 409
    assert _saved_job(client, job_id) == before


def test_confirmation_tokens_do_not_cross_requirements_or_jobs(client, sample_profile):
    job_id = setup(client, sample_profile)
    token = evidence_token(client, job_id, "r4")
    second_id = create_job(client)
    assert client.post(f"/jobs/{second_id}/requirements", data=requirement_form(DEMO_REQUIREMENTS),
                       follow_redirects=False).status_code == 303
    for target_id, req_id in [(job_id, "r5"), (second_id, "r4")]:
        before = _saved_job(client, target_id)
        response = client.post(f"/jobs/{target_id}/requirements/{req_id}/evidence",
                               data={"sources": "exp-1-b1", "review_token": token}, follow_redirects=False)
        assert response.status_code == 409
        assert _saved_job(client, target_id) == before


def test_requirements_editor_rejects_stale_save_and_keeps_input(client, sample_profile):
    job_id = setup(client, sample_profile)
    stale = editor_fields(client.get(f"/jobs/{job_id}/requirements/edit").text)
    first = stale | {"req-3-text": "First editor's qualification"}
    assert client.post(f"/jobs/{job_id}/requirements", data=first, follow_redirects=False).status_code == 303
    before = _saved_job(client, job_id)
    second = stale | {"req-3-text": "Second editor's qualification"}
    rejected = client.post(f"/jobs/{job_id}/requirements", data=second, follow_redirects=False)
    assert rejected.status_code == 409
    assert "Second editor&#39;s qualification" in rejected.text
    assert "Your input is kept below" in rejected.text
    assert _saved_job(client, job_id) == before
    # The input stays based on the old revision: saving again unchanged is refused again.
    assert editor_fields(rejected.text)["base_revision"] == stale["base_revision"]
    assert "Requirements as they are saved now" in rejected.text and "First editor&#39;s qualification" in rejected.text
    again = client.post(f"/jobs/{job_id}/requirements", data=editor_fields(rejected.text), follow_redirects=False)
    assert again.status_code == 409
    assert _saved_job(client, job_id) == before


def test_requirements_conflict_saves_over_the_newer_list_only_when_ticked(client, sample_profile):
    job_id = setup(client, sample_profile)
    stale = editor_fields(client.get(f"/jobs/{job_id}/requirements/edit").text)
    assert client.post(f"/jobs/{job_id}/requirements", data=stale | {"req-3-text": "First editor's qualification"},
                       follow_redirects=False).status_code == 303
    rejected = client.post(f"/jobs/{job_id}/requirements", data=stale | {"req-3-text": "Second editor's qualification"},
                           follow_redirects=False)
    replace = re.search(r'name="replace_revision" value="(\d+)"', rejected.text).group(1)
    assert replace == str(_saved_job(client, job_id)[0])
    fields = editor_fields(rejected.text) | {"replace_revision": replace}
    saved = client.post(f"/jobs/{job_id}/requirements", data=fields, follow_redirects=False)
    assert saved.status_code == 303
    page = client.get(f"/jobs/{job_id}/requirements/edit").text
    assert "Second editor&#39;s qualification" in page and "First editor&#39;s qualification" not in page


def test_requirements_validation_error_keeps_the_reviewed_revision(client, sample_profile):
    job_id = setup(client, sample_profile)
    fields = editor_fields(client.get(f"/jobs/{job_id}/requirements/edit").text)
    response = client.post(f"/jobs/{job_id}/requirements", data=fields | {"req-3-excerpt": "words not in the description"},
                           follow_redirects=False)
    assert response.status_code == 422
    assert editor_fields(response.text)["base_revision"] == fields["base_revision"]
    assert "Requirements as they are saved now" not in response.text


@pytest.mark.parametrize("revision", [None, "oops", "-1", ""])
def test_requirements_editor_requires_a_base_revision(client, sample_profile, revision):
    job_id = setup(client, sample_profile)
    fields = editor_fields(client.get(f"/jobs/{job_id}/requirements/edit").text)
    fields["req-3-text"] = "Input preserved after invalid revision"
    if revision is None:
        fields.pop("base_revision")
    else:
        fields["base_revision"] = revision
    before = _saved_job(client, job_id)
    response = client.post(f"/jobs/{job_id}/requirements", data=fields, follow_redirects=False)
    assert response.status_code == 409
    assert "Input preserved after invalid revision" in response.text
    assert _saved_job(client, job_id) == before


def _seed(session):
    candidate = profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate
    job = jobs_service.create(session, jobs_service.clean_input(**DEMO_JOB))
    set_requirements(session, job, copy.deepcopy(DEMO_REQUIREMENTS))
    return job, candidate


def _link(session, job, candidate, req_id, sources):
    checklist.link(session, job, candidate, req_id, sources,
                   reviewed_token=checklist.review_token(job, candidate.profile, req_id, sources))


@pytest.mark.parametrize("action", ["link", "reject", "override", "unlink"])
def test_cached_sessions_preserve_unrelated_checklist_decisions(session, action):
    job, candidate = _seed(session)
    if action == "unlink":
        _link(session, job, candidate, "r4", ["exp-1-b1", "proj-1-b1"])
    with Session(session.get_bind(), expire_on_commit=False) as stale:
        stale_job = stale.get(Job, job.id)
        stale_candidate = profile_service.get_candidate(stale)
        assert stale_job.evidence == job.evidence and stale_job.overrides == job.overrides
        if action == "link":
            _link(session, job, candidate, "r4", ["exp-1-b1"])
            _link(stale, stale_job, stale_candidate, "r4", ["proj-1-b1"])
        elif action == "reject":
            checklist.reject_suggestion(session, job, "r4", "exp-1-b1")
            checklist.reject_suggestion(stale, stale_job, "r4", "proj-1-b1")
        elif action == "override":
            checklist.set_override(session, job, "r4", "met", "First requirement")
            checklist.set_override(stale, stale_job, "r5", "unmet", "Second requirement")
        else:
            checklist.unlink(session, job, "r4", "exp-1-b1")
            checklist.unlink(stale, stale_job, "r4", "proj-1-b1")
    session.refresh(job)
    if action == "link":
        assert job.evidence["r4"].sources == ["exp-1-b1", "proj-1-b1"]
    elif action == "reject":
        assert job.evidence["r4"].rejected == ["exp-1-b1", "proj-1-b1"]
    elif action == "override":
        assert set(job.overrides) == {"r4", "r5"}
    else:
        assert job.evidence == {}


def test_simultaneous_requirement_editors_serialize_before_allocating_ids(session):
    job, _ = _seed(session)
    baseline = job.revision
    original = [r.model_dump(by_alias=True) for r in job.requirements]
    rendezvous = threading.Barrier(3)

    def save(index):
        with Session(session.get_bind(), expire_on_commit=False) as writer:
            loaded = writer.get(Job, job.id)
            assert loaded.revision == baseline
            rendezvous.wait(timeout=5)
            items = original + [{"text": f"Added qualification {index}", "excerpt": "Experience building REST APIs."}]
            try:
                set_requirements(writer, loaded, items, base_revision=baseline)
                return "saved"
            except RequirementsConflict:
                return "conflict"

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(save, range(3)))
    assert sorted(results) == ["conflict", "conflict", "saved"]
    session.refresh(job)
    assert job.revision == baseline + 1 and job.requirement_counter == 8
    assert [r.id for r in job.requirements] == [f"r{i}" for i in range(1, 9)]
    set_requirements(session, job, [r.model_dump(by_alias=True) for r in job.requirements] + [
        {"text": "Later qualification", "excerpt": "Experience building REST APIs."}])
    assert job.requirements[-1].id == "r9" and job.revision == baseline + 2


def test_identical_jobs_have_visible_independent_evidence_suggestions(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    first_id = setup(client, sample_profile)
    second_id = create_job(client)
    assert client.post(f"/jobs/{second_id}/requirements", data=requirement_form(DEMO_REQUIREMENTS),
                       follow_redirects=False).status_code == 303
    _approve_sharing(client)
    _suggest(client, fake_ai, first_id)
    _suggest(client, fake_ai, second_id)
    assert len(fake_ai.requests) == 2
    for job_id in (first_id, second_id):
        page = card(client.get(f"/jobs/{job_id}").text, "r4")
        assert "AI suggestions" in page and "REST API experience" in page
        assert f'/jobs/{job_id}/requirements/r4/suggestions/accept' in page
        # Repeat stays within that job's cache and never calls the provider.
        assert client.post(f"/jobs/{job_id}/evidence/suggest", follow_redirects=False).status_code == 303
    assert len(fake_ai.requests) == 2
    with client.app.state.session_factory() as session:
        runs = session.query(AIRun).filter(AIRun.operation == operations.SUGGEST_EVIDENCE).all()
        assert {run.job_id for run in runs} == {first_id, second_id}
        assert len({run.cache_key for run in runs}) == 2
