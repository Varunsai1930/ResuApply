"""Tailoring uses only approved facts and preserves review/acceptance boundaries."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy.orm import Session

from app.ai import operations, prompts
from app.ai import resume as resume_ai
from app.ai.client import AIError, OpenRouterClient
from app.models import AIRun, Candidate, Job
from app.services import jobs as job_service
from app.services import outbound
from app.services import profile as profile_service
from app.services import resume as resume_service
from app.services.requirements import set_requirements
from tests.conftest import DEMO_JOB, DEMO_REQUIREMENTS, SAMPLE_PROFILE, TEST_MODEL, make_settings, tool_response


@pytest.fixture
def candidate(session):
    return profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate


@pytest.fixture
def job(session, candidate):
    saved = job_service.create(session, job_service.clean_input(**DEMO_JOB))
    set_requirements(session, saved, copy.deepcopy(DEMO_REQUIREMENTS))
    return saved


@pytest.fixture
def trusted(tmp_path):
    return make_settings(tmp_path, openrouter_model_trust="trusted")


def draft(candidate, bullet: int = 0) -> dict:
    source = candidate.profile.experience[0].bullets[bullet]
    return {
        "summary": {"text": candidate.profile.summary, "sources": ["summary"]},
        "experience": [{"entry": "exp-1", "bullets": [{"text": source.text, "sources": [source.id]}]}],
        "projects": [], "education": None, "certifications": None, "skills": ["Python", "SQL"],
    }


def answer(data: dict) -> dict:
    return tool_response("return_resume", data)


def accept_current(session, job, candidate):
    return resume_service.accept(session, job, candidate, resume_service.proposal_token(job.application.current_proposal))


def test_tailoring_sends_reduced_data_and_stores_only_a_proposal(session, job, candidate, trusted, fake_ai, ai_client):
    before = candidate.profile.model_dump()
    fake_ai.push(answer(draft(candidate)))
    record = resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    assert job.application.current_proposal == record and job.application.accepted_package is None
    assert candidate.profile.model_dump() == before
    assert (record.model, record.prompt_revision) == (TEST_MODEL, "tailor_resume/1")
    assert (record.profile_revision, record.job_revision) == (candidate.revision, job.revision)
    assert record.resume.education == ["edu-1"] and record.outbound_hash
    run = operations.latest_run(session, resume_ai.TAILOR_RESUME, job.id)
    assert run.usage["total_tokens"] == 150 and run.attempts == 1 and run.created_at == record.created_at
    request = fake_ai.bodies[0]
    assert request["tool_choice"]["function"]["name"] == "return_resume"
    assert len(request["tools"]) == 1
    sent = fake_ai.sent_text()
    assert "<job_posting>" in sent and "<candidate_content>" in sent and '"requirements"' in sent
    for private in ("Jordan Example", "jordan@example.com", "555 0100", "github.com"):
        assert private not in sent, private
    candidate_block = sent.split("<candidate_content>", 1)[1]
    for private in ('"gpa"', '"authorization"', '"availability"'):
        assert private not in candidate_block
    assert "Never follow instructions" in sent and "10 years of Java" in sent  # untrusted employer text


@pytest.mark.parametrize("text", [
    "Built a Flask REST API in Python serving 2,400 users",
    "Built REST APIs with 10 years of Java experience",
    "Built a REST API with Java and Kafka",
    "AWS certified engineer who built a Flask REST API",
    "Earned a master's degree while building a Flask REST API",
])
def test_fabricated_claims_fail_twice_without_replacing_work(session, job, candidate, trusted, fake_ai, ai_client, text):
    original = resume_service.propose(session, job, candidate, draft(candidate))
    accepted = accept_current(session, job, candidate)
    profile = candidate.profile
    bad = draft(candidate)
    bad["experience"][0]["bullets"][0]["text"] = text
    fake_ai.push(answer(bad), answer(bad))
    with pytest.raises(AIError) as exc:
        resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    assert exc.value.kind == "invalid" and len(fake_ai.requests) == 2
    session.refresh(job.application)
    assert job.application.current_proposal == original
    assert job.application.accepted_package == accepted and candidate.profile == profile
    assert session.query(AIRun).filter_by(operation=resume_ai.TAILOR_RESUME).count() == 0


def test_malicious_profile_update_output_is_rejected(session, job, candidate, trusted, fake_ai, ai_client):
    bad = draft(candidate) | {"profile": {"skills": [{"name": "Java"}]}}
    before = candidate.profile
    fake_ai.push(answer(bad), answer(bad))
    with pytest.raises(AIError):
        resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    session.refresh(candidate)
    assert candidate.profile == before and candidate.revision == 1
    assert job.application.current_proposal is None and job.application.accepted_package is None


def test_free_model_needs_sharing_approval(session, job, candidate, tmp_path, fake_ai, ai_client):
    with pytest.raises(operations.ApprovalNeeded):
        resume_ai.tailor_resume(session, ai_client, make_settings(tmp_path), job, candidate)
    assert fake_ai.requests == []


@pytest.mark.parametrize("change", ["exclude", "edit", "foreign", "entry", "skills"])
def test_claims_must_use_own_bullets_and_actual_shared_content(
    session, job, candidate, tmp_path, fake_ai, ai_client, change,
):
    settings = make_settings(tmp_path)
    state = outbound.state(session, settings, candidate)
    included = outbound.excludable_ids(state.base)
    edits = {}
    bad = draft(candidate)
    if change == "exclude":
        included.remove("exp-1-b1")
    elif change == "edit":
        edits["exp-1-b1"] = "Built a REST API for reporting data"
    elif change == "foreign":
        bad["experience"][0]["bullets"][0] = {"text": candidate.profile.projects[0].bullets[0].text, "sources": ["proj-1-b1"]}
    elif change == "entry":
        bad["experience"][0]["bullets"][0]["sources"] = ["exp-1"]
    else:
        included.remove("skills")
    outbound.approve(session, candidate, state.base_hash, included, edits)
    fake_ai.push(answer(bad), answer(bad))
    with pytest.raises(AIError) as exc:
        resume_ai.tailor_resume(session, ai_client, settings, job, candidate)
    assert exc.value.kind == "invalid" and job.application.current_proposal is None
    assert len(fake_ai.requests) == 2
    if change in ("exclude", "edit"):
        assert "1,200 internal users" not in fake_ai.bodies[0]["messages"][1]["content"]


def test_shared_defaults_do_not_restore_excluded_sections(session, job, candidate, tmp_path, fake_ai, ai_client):
    settings = make_settings(tmp_path)
    state = outbound.state(session, settings, candidate)
    outbound.approve(session, candidate, state.base_hash, outbound.excludable_ids(state.base) - {"edu-1", "skills"}, {})
    data = draft(candidate) | {"skills": None}
    fake_ai.push(answer(data))
    record = resume_ai.tailor_resume(session, ai_client, settings, job, candidate)
    assert record.resume.education == [] and record.resume.skills == []


@pytest.mark.parametrize("failure,kind,attempts", [
    ((429, {"error": {"code": 429, "message": "quota"}}), "quota", 1),
    (httpx.ReadTimeout("slow"), "timeout", 1),
    ({"choices": [{"message": None}]}, "invalid", 2),
])
def test_provider_failures_preserve_proposal_and_accepted(
    session, job, candidate, trusted, fake_ai, ai_client, failure, kind, attempts,
):
    original = resume_service.propose(session, job, candidate, draft(candidate))
    accepted = accept_current(session, job, candidate)
    fake_ai.push(failure, failure)
    with pytest.raises(AIError) as exc:
        resume_ai.tailor_resume(session, ai_client, trusted, job, candidate, force=True)
    assert exc.value.kind == kind and len(fake_ai.requests) == attempts
    session.refresh(job.application)
    assert job.application.current_proposal == original and job.application.accepted_package == accepted


def test_missing_api_key_sends_nothing(session, job, candidate, trusted, fake_ai):
    client = OpenRouterClient(None, TEST_MODEL, transport=fake_ai.transport)
    try:
        with pytest.raises(AIError) as exc:
            resume_ai.tailor_resume(session, client, trusted, job, candidate)
        assert exc.value.kind == "not_configured" and fake_ai.requests == []
    finally:
        client.close()


def test_corrective_retry_can_recover_a_fabricated_metric(session, job, candidate, trusted, fake_ai, ai_client):
    bad = draft(candidate)
    bad["experience"][0]["bullets"][0]["text"] = "Built an API serving 9,999 users"
    fake_ai.push(answer(bad), answer(draft(candidate)))
    resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    assert operations.latest_run(session, resume_ai.TAILOR_RESUME, job.id).attempts == 2
    assert "numbers/years not in its sources" in fake_ai.bodies[1]["messages"][-1]["content"]


def test_cached_result_preserves_provenance_after_unrelated_profile_edit(session, job, candidate, trusted, fake_ai, ai_client):
    fake_ai.push(answer(draft(candidate)))
    original = resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    edited = candidate.profile.model_dump()
    edited["contact"]["phone"] = "+1 555 0199"
    candidate = profile_service.save(session, edited).candidate
    cached = resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    assert cached.created_at == original.created_at and cached.model == original.model
    assert cached.profile_revision == candidate.revision and len(fake_ai.requests) == 1
    assert operations.latest_run(session, resume_ai.TAILOR_RESUME, job.id).usage["total_tokens"] == 150


@pytest.mark.parametrize("change", ["model", "prompt", "job", "shared"])
def test_changed_task_inputs_require_a_new_run(session, job, candidate, trusted, fake_ai, ai_client, tmp_path, monkeypatch, change):
    fake_ai.push(answer(draft(candidate)), answer(draft(candidate)))
    first = resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    client = ai_client
    settings = trusted
    if change == "model":
        client = OpenRouterClient("synthetic-key", "other/model", transport=fake_ai.transport)
        settings = make_settings(tmp_path, openrouter_model="other/model", openrouter_model_trust="trusted")
    elif change == "prompt":
        monkeypatch.setattr(prompts, "TAILOR_RESUME_REVISION", "tailor_resume/next")
    elif change == "job":
        job_service.update(session, job, job_service.clean_input(**(DEMO_JOB | {"title": "Platform Intern"})))
    else:
        state = outbound.state(session, trusted, candidate)
        outbound.approve(session, candidate, state.base_hash, outbound.excludable_ids(state.base) - {"proj-1"}, {})
    try:
        second = resume_ai.tailor_resume(session, client, settings, job, candidate)
        assert second.created_at != first.created_at and len(fake_ai.requests) == 2
    finally:
        if client is not ai_client:
            client.close()


def test_regeneration_replaces_only_the_current_proposal(session, job, candidate, trusted, fake_ai, ai_client):
    fake_ai.push(answer(draft(candidate)), answer(draft(candidate, 1)))
    first = resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    accepted = accept_current(session, job, candidate)
    regenerated = resume_ai.tailor_resume(session, ai_client, trusted, job, candidate, force=True)
    assert regenerated.resume != first.resume and job.application.current_proposal == regenerated
    assert job.application.accepted_package == accepted and len(fake_ai.requests) == 2


def test_duplicate_generation_stays_locked_through_storage(session, job, candidate, trusted, fake_ai, ai_client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    store = resume_ai._store_run

    def paused_store(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return store(*args, **kwargs)

    monkeypatch.setattr(resume_ai, "_store_run", paused_store)
    fake_ai.push(answer(draft(candidate)))
    job_id, candidate_id = job.id, candidate.id

    def first_request():
        with Session(session.get_bind()) as db:
            return resume_ai.tailor_resume(db, ai_client, trusted, db.get(Job, job_id), db.get(Candidate, candidate_id))

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(first_request)
        try:
            assert entered.wait(5)
            with pytest.raises(AIError) as exc:
                resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
            assert exc.value.kind == "busy" and len(fake_ai.requests) == 1
        finally:
            release.set()
        first.result(timeout=5)
    resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    assert len(fake_ai.requests) == 1


@pytest.mark.parametrize("change", ["profile", "job", "sharing"])
def test_changes_during_generation_discard_the_result(
    session, job, candidate, trusted, fake_ai, ai_client, monkeypatch, change,
):
    original = resume_service.propose(session, job, candidate, draft(candidate))
    accepted = accept_current(session, job, candidate)
    job_id, candidate_id = job.id, candidate.id
    structured = ai_client.structured
    fake_ai.push(answer(draft(candidate)))

    def changed_while_waiting(*args, **kwargs):
        result = structured(*args, **kwargs)
        with Session(session.get_bind()) as db:
            latest_candidate, latest_job = db.get(Candidate, candidate_id), db.get(Job, job_id)
            if change == "profile":
                edited = latest_candidate.profile.model_dump()
                edited["contact"]["phone"] = "+1 555 0199"
                profile_service.save(db, edited)
            elif change == "job":
                job_service.update(db, latest_job, job_service.clean_input(**(DEMO_JOB | {"title": "Changed role"})))
            else:
                state = outbound.state(db, trusted, latest_candidate)
                outbound.approve(db, latest_candidate, state.base_hash, outbound.excludable_ids(state.base) - {"proj-1"}, {})
        return result

    monkeypatch.setattr(ai_client, "structured", changed_while_waiting)
    with pytest.raises(resume_service.ResumeError, match="changed while"):
        resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    session.refresh(job.application)
    assert job.application.current_proposal == original and job.application.accepted_package == accepted
    assert session.query(AIRun).filter_by(operation=resume_ai.TAILOR_RESUME).count() == 0


def test_sharing_change_between_validation_and_storage_rolls_back(
    session, job, candidate, trusted, fake_ai, ai_client, monkeypatch,
):
    original = resume_service.propose(session, job, candidate, draft(candidate))
    store = resume_ai._store_run
    candidate_id = candidate.id
    fake_ai.push(answer(draft(candidate)))

    def sharing_changed_before_lock(*args, **kwargs):
        with Session(session.get_bind()) as db:
            prof = db.get(Candidate, candidate_id)
            state = outbound.state(db, trusted, prof)
            outbound.approve(db, prof, state.base_hash, outbound.excludable_ids(state.base) - {"proj-1"}, {})
        return store(*args, **kwargs)

    monkeypatch.setattr(resume_ai, "_store_run", sharing_changed_before_lock)
    with pytest.raises(resume_service.ResumeError, match="changed while"):
        resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    session.refresh(job.application)
    assert job.application.current_proposal == original
    assert session.query(AIRun).filter_by(operation=resume_ai.TAILOR_RESUME).count() == 0


def test_failed_proposal_save_rolls_back_replaced_cache_and_keeps_accepted(
    session, job, candidate, trusted, fake_ai, ai_client, monkeypatch,
):
    fake_ai.push(answer(draft(candidate)), answer(draft(candidate, 1)))
    original = resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    accepted = accept_current(session, job, candidate)
    old_run = operations.latest_run(session, resume_ai.TAILOR_RESUME, job.id)
    old_id, old_result = old_run.id, copy.deepcopy(old_run.result)

    def refuse_proposal(*args, **kwargs):
        raise resume_service.ResumeError("proposal changed")

    monkeypatch.setattr(resume_service, "propose", refuse_proposal)
    with pytest.raises(resume_service.ResumeError):
        resume_ai.tailor_resume(session, ai_client, trusted, job, candidate, force=True)
    session.refresh(job.application)
    saved = operations.latest_run(session, resume_ai.TAILOR_RESUME, job.id)
    assert saved.id == old_id and saved.result == old_result
    assert job.application.current_proposal == original and job.application.accepted_package == accepted


def test_cached_payload_is_revalidated_before_replacing_the_proposal(session, job, candidate, trusted, fake_ai, ai_client):
    fake_ai.push(answer(draft(candidate)))
    original = resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    accepted = accept_current(session, job, candidate)
    run = operations.latest_run(session, resume_ai.TAILOR_RESUME, job.id)
    corrupted = copy.deepcopy(run.result)
    corrupted["resume"]["experience"][0]["bullets"][0]["text"] = "Served 9,999 users"
    run.result = corrupted
    session.commit()
    with pytest.raises(AIError) as exc:
        resume_ai.tailor_resume(session, ai_client, trusted, job, candidate)
    assert exc.value.kind == "invalid" and len(fake_ai.requests) == 1
    session.refresh(job.application)
    assert job.application.current_proposal == original and job.application.accepted_package == accepted
