"""AI requests run in the background: progress page, status endpoint, reloads, duplicates and failures.

The mocked model here holds each answer until the test releases it, so the request is still running
when the test looks at its progress. All data is synthetic.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import wait
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai import tasks
from app.main import create_app
from app.models import Job
from tests.conftest import BASE_URL, DEMO_REQUIREMENTS, NO_NETWORK, FakeOpenRouter, tool_response
from tests.test_assessment_routes import setup as requirements_setup
from tests.test_questions_routes import WHY_Q, add, qid_of, ready_job
from tests.test_resume_routes import approval, draft
from tests.test_resume_routes import setup as resume_setup
from tests.test_routes import client_profile, profile_form, review_and_save

BULLET = "Built a Flask REST API in Python that served reporting data to 1,200 internal users"
PAGE = re.compile(r"^/jobs/(\d+)/ai/([0-9a-f]{16})$")


class HeldOpenRouter(FakeOpenRouter):
    """A stand-in model that answers only once ``release()`` is called."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.gate.set()
        self.arrived = threading.Event()

    def hold(self) -> None:
        self.gate.clear()
        self.arrived.clear()

    def release(self) -> None:
        self.gate.set()

    def _handle(self, request):
        self.arrived.set()
        assert self.gate.wait(10), "the test never released the model's answer"
        return super()._handle(request)


@pytest.fixture
def fake_ai() -> HeldOpenRouter:
    return HeldOpenRouter()


@pytest.fixture
def background(monkeypatch):
    """Form posts go straight to the progress page instead of waiting for the result."""
    monkeypatch.setattr(tasks, "WAIT_SECONDS", 0.0)


def task_of(client, location: str) -> tasks.Task:
    job_id, task_id = PAGE.match(location).groups()
    task = client.app.state.ai_tasks.get(int(job_id), task_id)
    assert task is not None
    return task


def finished(client, location: str) -> tasks.Task:
    task = task_of(client, location)
    wait([task.future], timeout=10)
    assert task.done
    return task


# Each AI request: how to get a job ready for it, the model's answer, the form post and where its result shows.

def _extract(client, profile):
    job_id = requirements_setup(client, profile, requirements=False)
    return job_id, tool_response("return_requirements", {"requirements": DEMO_REQUIREMENTS}), \
        f"/jobs/{job_id}/requirements/extract", f"/jobs/{job_id}/requirements/edit?proposal=1"


def _suggest(client, profile):
    job_id = requirements_setup(client, profile)
    approval(client)
    answer = tool_response("return_evidence_suggestions", {"suggestions": [
        {"requirement_id": "r4", "source_ids": ["exp-1-b1"], "reason": "Describes building a REST API"}]})
    return job_id, answer, f"/jobs/{job_id}/evidence/suggest", \
        f"/jobs/{job_id}?step=requirements&msg=suggestions_ready#requirements"


def _tailor(client, profile):
    job_id = resume_setup(client, profile)
    approval(client)
    return job_id, tool_response("return_resume", draft(client)), f"/jobs/{job_id}/resume/generate", \
        f"/jobs/{job_id}/resume/review"


def _answers(client, profile):
    job_id = ready_job(client, profile)
    approval(client)
    why = qid_of(add(client, job_id, WHY_Q, limit="300"))
    answer = tool_response("return_answers", {"answers": [{"question_id": why, "text": BULLET, "sources": ["exp-1-b1"]}]})
    return job_id, answer, f"/jobs/{job_id}/questions/draft", f"/jobs/{job_id}?step=questions&msg=drafts_ready#questions"


def _cover_letter(client, profile):
    job_id = resume_setup(client, profile)
    approval(client)
    answer = tool_response("return_cover_letter", {"text": f"I am applying for this role.\n\n{BULLET}.",
                                                   "sources": ["exp-1-b1"]})
    return job_id, answer, f"/jobs/{job_id}/cover-letter/draft", \
        f"/jobs/{job_id}?step=questions&msg=cover_letter_drafted#cover-letter"


REQUESTS = {
    "Extracting requirements": _extract,
    "Suggesting evidence": _suggest,
    "Tailoring your resume": _tailor,
    "Drafting answers": _answers,
    "Drafting your cover letter": _cover_letter,
}


@pytest.mark.parametrize("label", REQUESTS)
def test_each_ai_request_runs_in_the_background_and_its_page_shows_the_result(
        ai_app_client, fake_ai, sample_profile, background, label):
    client = ai_app_client
    job_id, answer, action, result = REQUESTS[label](client, sample_profile)
    fake_ai.hold()
    fake_ai.push(answer)

    started = client.post(action, follow_redirects=False)
    assert started.status_code == 303
    page_url = started.headers["location"]
    assert PAGE.match(page_url) and PAGE.match(page_url).group(1) == str(job_id)
    assert fake_ai.arrived.wait(10)  # the request reached the model and is waiting for its answer

    # Until it finishes, the page shows progress and reloads itself, without JavaScript.
    page = client.get(page_url)
    assert page.status_code == 200
    assert '<meta http-equiv="refresh" content="2">' in page.text
    assert f"{label}…" in page.text and "Running for" in page.text and "up to 90 seconds" in page.text
    assert f'href="/jobs/{job_id}?step=' in page.text  # back to the job, which keeps working
    status = client.get(f"{page_url}/status").json()
    assert status["state"] == "running" and status["label"] == label and status["finished_at"] is None
    assert status["page"] == page_url
    workspace = client.get(f"/jobs/{job_id}").text
    assert f"{label} with AI, running for" in workspace and f'href="{page_url}"' in workspace

    # Asking again shows the same request; nothing is sent twice.
    again = client.post(action, follow_redirects=False)
    assert again.headers["location"] == page_url

    fake_ai.release()
    finished(client, page_url)
    assert len(fake_ai.requests) == 1
    # A reload now goes to the result, and keeps doing so.
    for _ in range(2):
        done = client.get(page_url, follow_redirects=False)
        assert done.status_code == 303 and done.headers["location"] == result
    status = client.get(f"{page_url}/status").json()
    assert status["state"] == "done" and status["finished_at"] is not None
    assert f"{label} with AI" not in client.get(f"/jobs/{job_id}").text
    assert client.get(result).status_code == 200


def _stored(client, job_id):
    with client.app.state.session_factory() as session:
        application = session.get(Job, job_id).application
        return application.current_proposal, application.accepted_package


def test_a_failed_background_request_leaves_work_untouched_and_reloads_the_same_error(
        ai_app_client, fake_ai, sample_profile, background):
    client = ai_app_client
    job_id, answer, action, _ = _tailor(client, sample_profile)
    fake_ai.push(answer)
    first = finished(client, client.post(action, follow_redirects=False).headers["location"])
    assert first.state == "done"
    before = _stored(client, job_id)
    assert before[0] is not None

    fake_ai.hold()
    fake_ai.push((429, {"error": {"message": "quota"}}))
    page_url = client.post(action, data={"force": "1"}, follow_redirects=False).headers["location"]
    fake_ai.release()
    task = finished(client, page_url)
    assert task.state == "failed"
    for _ in range(2):
        failed = client.get(page_url)
        assert failed.status_code == 502 and "rate limit" in failed.text
    status = client.get(f"{page_url}/status").json()
    assert status["state"] == "failed" and "rate limit" in status["message"]
    assert _stored(client, job_id) == before


def test_a_profile_change_while_a_request_runs_is_never_overwritten(ai_app_client, fake_ai, sample_profile, background):
    client = ai_app_client
    job_id, answer, action, _ = _tailor(client, sample_profile)
    fake_ai.hold()
    fake_ai.push(answer)
    page_url = client.post(action, follow_redirects=False).headers["location"]
    assert fake_ai.arrived.wait(10)

    # The user keeps working while the model thinks: the page they're on still answers at once.
    edited = client_profile(client)
    edited["summary"] = "Data engineer who also builds small web tools."
    _, saved = review_and_save(client, profile_form(edited))
    assert saved.status_code == 303

    fake_ai.release()
    finished(client, page_url)
    refused = client.get(page_url)
    assert refused.status_code == 409
    assert "changed while the resume was being generated" in refused.text and "Nothing was replaced" in refused.text
    assert _stored(client, job_id) == (None, None)


def test_a_quick_result_skips_the_progress_page(ai_app_client, fake_ai, sample_profile, monkeypatch):
    monkeypatch.setattr(tasks, "WAIT_SECONDS", 5.0)
    client = ai_app_client
    job_id, answer, action, result = _extract(client, sample_profile)
    fake_ai.push(answer)
    assert client.post(action, follow_redirects=False).headers["location"] == result
    # The validated result is cached: asking again doesn't reach the model.
    assert client.post(action, follow_redirects=False).headers["location"] == result
    assert len(fake_ai.requests) == 1


def test_an_unknown_request_says_it_is_not_running(ai_app_client, fake_ai, sample_profile, background):
    client = ai_app_client
    job_id, answer, action, _ = _extract(client, sample_profile)
    missing = client.get(f"/jobs/{job_id}/ai/0123456789abcdef")
    assert missing.status_code == 404 and "running any more" in missing.text
    status = client.get(f"/jobs/{job_id}/ai/0123456789abcdef/status")
    assert status.status_code == 404 and status.json()["state"] == "unknown"

    fake_ai.push(answer)
    page_url = client.post(action, follow_redirects=False).headers["location"]
    finished(client, page_url)
    other_job = client.post("/jobs", data={"title": "Other", "company": "Elsewhere", "description": "Python."},
                            follow_redirects=False).headers["location"].split("?")[0]
    assert client.get(page_url.replace(f"/jobs/{job_id}/", f"{other_job}/")).status_code == 404
    assert client.get(f"/jobs/999/ai/{task_of(client, page_url).id}").status_code == 404


def test_after_a_restart_an_old_request_page_explains_and_the_result_is_kept(
        ai_settings, fake_ai, sample_profile, background):
    with TestClient(create_app(ai_settings, ai_transport=fake_ai.transport, greenhouse_transport=NO_NETWORK),
                    base_url=BASE_URL) as client:
        job_id, answer, action, result = _extract(client, sample_profile)
        fake_ai.push(answer)
        page_url = client.post(action, follow_redirects=False).headers["location"]
        finished(client, page_url)
    with TestClient(create_app(ai_settings, ai_transport=fake_ai.transport, greenhouse_transport=NO_NETWORK),
                    base_url=BASE_URL) as restarted:
        gone = restarted.get(page_url)
        assert gone.status_code == 404 and "the app may have restarted" in gone.text
        assert "proposed by the AI model" in restarted.get(result).text  # the validated proposal was saved


def test_finished_requests_are_forgotten_after_a_while(ai_app_client, fake_ai, sample_profile, background, monkeypatch):
    client = ai_app_client
    job_id, answer, action, _ = _extract(client, sample_profile)
    fake_ai.push(answer)
    old = finished(client, client.post(action, follow_redirects=False).headers["location"])
    # Already past its keep time, whatever the clock's resolution (Windows' can return the same instant twice).
    monkeypatch.setattr(tasks, "KEEP_FINISHED", timedelta(seconds=-1))
    registry = client.app.state.ai_tasks
    # Looking a request up (a progress page, the workspace) is enough to forget old ones.
    assert registry.get(job_id, old.id) is None and old.id not in registry._tasks
    new = registry.start("check", job_id, lambda session, job, candidate: job.id, lambda *args: None)
    wait([new.future], timeout=10)
    assert new.result() == job_id


def test_a_request_for_a_deleted_job_saves_nothing(ai_app_client, fake_ai, sample_profile, background):
    client = ai_app_client
    job_id, _, _, _ = _extract(client, sample_profile)
    with client.app.state.session_factory() as session:
        session.delete(session.get(Job, job_id))
        session.commit()
    task = client.app.state.ai_tasks.start("check", job_id, lambda *args: pytest.fail("ran without its job"),
                                           lambda *args: None)
    wait([task.future], timeout=10)
    assert task.state == "failed" and "deleted" in task.message()


def test_shutdown_is_bounded_cancels_waiting_requests_and_never_holds_the_process(client, sample_profile):
    import time

    from app.ai.tasks import AITasks

    job_id = requirements_setup(client, sample_profile, requirements=False)
    registry = AITasks(client.app.state.session_factory, workers=1)
    running_started, release = threading.Event(), threading.Event()

    def blocking(*_):
        running_started.set()
        release.wait(10)

    running = registry.start("check", job_id, blocking, lambda *args: None)
    assert running_started.wait(10)
    waiting = registry.start("other", job_id, lambda *_: pytest.fail("a waiting request ran"), lambda *args: None)

    began = time.monotonic()
    registry.shutdown(timeout=0.2)
    assert time.monotonic() - began < 2  # the running request is left, not waited for
    assert waiting.future.cancelled() and not running.done
    assert all(t.daemon for t in threading.enumerate() if t.name.startswith("resuapply-ai-"))
    with pytest.raises(RuntimeError, match="stopping"):
        registry.start("third", job_id, blocking, lambda *args: None)
    release.set()
    wait([running.future], timeout=10)
    assert running.state == "done"


def test_an_extraction_for_a_job_edited_meanwhile_is_not_shown(ai_app_client, fake_ai, sample_profile, background):
    client = ai_app_client
    job_id, answer, action, _ = _extract(client, sample_profile)
    fake_ai.hold()
    fake_ai.push(answer)
    page_url = client.post(action, follow_redirects=False).headers["location"]
    assert fake_ai.arrived.wait(10)
    with client.app.state.session_factory() as session:  # the user edits the job while the model works
        job = session.get(Job, job_id)
        job.description += "\nAlso: Rust."
        job.revision += 1
        session.commit()
    fake_ai.release()
    finished(client, page_url)
    page = client.get(page_url)
    assert page.status_code == 409 and "The job changed while its requirements were being extracted" in page.text
    assert "proposal" not in client.get(page_url, follow_redirects=False).headers.get("location", "")


@pytest.mark.parametrize("label", ["Suggesting evidence", "Tailoring your resume", "Drafting answers",
                                   "Drafting your cover letter"])
def test_a_profile_removed_before_the_request_runs_is_refused_the_same_way(
        ai_app_client, fake_ai, sample_profile, background, monkeypatch, label):
    from app.ai import tasks as tasks_module

    client = ai_app_client
    _, _, action, _ = REQUESTS[label](client, sample_profile)
    monkeypatch.setattr(tasks_module, "get_candidate", lambda session: None)  # removed while it waited
    page_url = client.post(action, follow_redirects=False).headers["location"]
    finished(client, page_url)
    page = client.get(page_url)
    assert page.status_code == 409 and "Your profile was removed before the request ran" in page.text
    assert fake_ai.requests == []
