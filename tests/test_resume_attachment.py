"""The resume PDF attached to an approved package for autofill, and the app without Playwright. Fictional data only."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.services import answers, autofill_browser, jobs, package, profile, resume_attachment
from app.services.profile import get_candidate
from app.services.resume_attachment import AttachmentError
from tests.autofill_site import POSTING_URL
from tests.resume_files import encrypted_pdf_bytes, pdf_bytes
from tests.synthetic import DEMO_JOB
from tests.test_package import approve, ready

ROOT = Path(__file__).resolve().parent.parent
RESUME = pdf_bytes(["Jordan  Example", "jordan@example.com", "Experience"])


@pytest.fixture
def approved(session, sample_profile):
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**{**DEMO_JOB, "url": POSTING_URL}))
    ready(session, job, candidate)
    approve(session, job, candidate)
    return job, candidate


def attach(session, settings, job, candidate, data=RESUME, name="Jordan resume.pdf", token=None):
    token = token if token is not None else package.package_token(job, job.application, candidate)
    return resume_attachment.attach(session, settings.resolved_data_dir, job, candidate, data, name, token)


def test_attaches_the_pdf_to_the_approved_package(session, settings, approved):
    job, candidate = approved
    attachment = attach(session, settings, job, candidate)
    assert attachment.filename == "Jordan resume.pdf" and attachment.pages == 1 and attachment.size == len(RESUME)
    assert attachment.package_hash == job.application.approval.content_hash
    assert resume_attachment.current(job, job.application, candidate) == attachment
    assert resume_attachment.load(settings.resolved_data_dir, job, attachment) == RESUME
    # Attaching is not package content: approval and tracking are untouched.
    assert package.review_state(job, job.application, candidate).value == "approved"
    assert job.application.tracking_status == "saved"


@pytest.mark.parametrize("data, message", [
    (b"not a pdf", "isn't a PDF"),
    (b"%PDF-1.4 damaged", "couldn't be read"),
    (encrypted_pdf_bytes(["Jordan Example"]), "password-protected"),
    (pdf_bytes(["Someone Else", "Experience"]), "doesn't show your name"),
    (pdf_bytes(["Jordan Example"]) + b"%" + b"x" * (10 * 1024 * 1024), "over 10 MB"),
])
def test_refuses_files_that_are_not_the_packages_resume(session, settings, approved, data, message):
    job, candidate = approved
    with pytest.raises(AttachmentError, match=message):
        attach(session, settings, job, candidate, data=data)
    assert job.application.resume_attachment is None
    assert not resume_attachment.path_for(settings.resolved_data_dir, job.id).exists()


def test_refuses_a_draft_package_and_a_page_that_showed_another(session, settings, approved):
    job, candidate = approved
    with pytest.raises(AttachmentError, match="package changed"):
        attach(session, settings, job, candidate, token="stale")
    package_token = package.package_token(job, job.application, candidate)
    question = job.questions[-1]
    answers.set_answer(session, job, job.application, question.id, "I'd rather say this.")
    with pytest.raises(AttachmentError, match="Approve the current package first"):
        attach(session, settings, job, candidate, token=package_token)


def test_a_changed_package_needs_the_pdf_again(session, settings, approved):
    job, candidate = approved
    attach(session, settings, job, candidate)
    answers.set_answer(session, job, job.application, job.questions[2].id, "A different answer.")
    approve(session, job, candidate)
    assert job.application.resume_attachment is not None
    assert resume_attachment.current(job, job.application, candidate) is None


def test_remove_deletes_the_file(session, settings, approved):
    job, candidate = approved
    attach(session, settings, job, candidate)
    resume_attachment.remove(session, settings.resolved_data_dir, job, candidate)
    assert job.application.resume_attachment is None
    assert not resume_attachment.path_for(settings.resolved_data_dir, job.id).exists()


@pytest.mark.parametrize("given, kept", [
    ("C:\\Users\\me\\Jordan Example — Resume.pdf", "Jordan Example - Resume.pdf"),
    ("../../etc/passwd", "passwd.pdf"),
    ("résumé.PDF", "résumé.pdf"),
    ("", "Resume.pdf"),
    ("<>.pdf", "Resume.pdf"),
])
def test_file_names_sent_to_the_employer_are_plain(given, kept):
    assert resume_attachment.safe_name(given) == kept


def test_upload_route_attaches_and_shows_it(client, session, approved):
    job, candidate = approved
    token = package.package_token(job, job.application, candidate)
    response = client.post(f"/jobs/{job.id}/autofill/resume", data={"package_token": token},
                           files={"file": ("cv.pdf", RESUME, "application/pdf")}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get(f"/jobs/{job.id}?step=track").text
    assert "cv.pdf" in page and "Remove" in page
    response = client.post(f"/jobs/{job.id}/autofill/resume", data={"package_token": token},
                           files={"file": ("x.pdf", b"nope", "application/pdf")})
    assert response.status_code == 422 and "isn&#39;t a PDF" in response.text


# ---------------------------------------------------------------- without Playwright

def test_the_track_step_explains_setup_when_playwright_is_missing(client, session, approved, monkeypatch):
    job, candidate = approved
    monkeypatch.setattr(autofill_browser, "installed", lambda: False)
    monkeypatch.setattr("app.services.autofill.installed", lambda: False)
    page = client.get(f"/jobs/{job.id}?step=track").text
    assert "requirements-autofill.txt" in page and "Open the form in a browser" not in page
    assert "Update status" in page  # tracking works as before
    response = client.post(f"/jobs/{job.id}/autofill/open",
                           data={"package_token": package.package_token(job, job.application, get_candidate(session))})
    assert response.status_code == 409 and "optional add-on" in response.text


def test_the_app_starts_and_serves_pages_without_playwright(tmp_path):
    """Run in a fresh interpreter where importing Playwright fails, as on an install without the add-on."""
    script = textwrap.dedent(f"""
        import sys
        sys.modules["playwright"] = None  # any import of it raises ImportError
        from fastapi.testclient import TestClient
        from app.config import Settings
        from app.main import create_app
        from app.services import autofill_browser
        settings = Settings(data_dir={str(tmp_path)!r}, _env_file=None, openrouter_api_key=None)
        with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
            assert client.get("/jobs").status_code == 200
            assert client.get("/profile/edit").status_code == 200
        try:
            autofill_browser.FormBrowser().open("https://job-boards.greenhouse.io/x/jobs/1")
        except autofill_browser.BrowserError as exc:
            assert "optional add-on" in str(exc)
        else:
            raise AssertionError("opened a browser without Playwright")
        assert not any(name.startswith("playwright.") for name in sys.modules)
        print("ok")
    """)
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
