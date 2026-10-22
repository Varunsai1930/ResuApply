"""Read-only PDF previews, accepted-only downloads and frozen submitted exports."""

import copy
from collections import OrderedDict
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from app.models import Job
from app.services import profile, resume, resume_pdf
from tests.autofill_site import chromium
from tests.test_package import approve, record_applied
from tests.test_resume_export_routes import accepted_job, choose
from tests.test_resume_routes import accept, manual_proposal, setup


@pytest.fixture(autouse=True)
def isolated_pdf_cache(monkeypatch):
    monkeypatch.setattr(resume_pdf, "_cache", OrderedDict())


@pytest.fixture
def pdf_builder(monkeypatch):
    calls = []
    def build(html, executable):
        calls.append(html)
        stream = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=595.28, height=841.89)
        selected = next(key for key in ("classic", "modern", "compact")
                        if f'class="resume-document resume-layout-{key}"' in html)
        writer.add_metadata({"/Subject": selected})
        writer.write(stream)
        return stream.getvalue()
    monkeypatch.setattr(resume_pdf, "_build", build)
    return calls


@pytest.mark.parametrize("layout", ["classic", "modern", "compact"])
def test_preview_is_read_only_and_download_uses_saved_layout(client, sample_profile, pdf_builder, layout):
    job_id = accepted_job(client, sample_profile)
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        candidate = profile.get_candidate(session)
        approve(session, job, candidate)
        token = job.application.approval
        accepted = copy.deepcopy(job.application.accepted_package)
    preview = client.get(f"/jobs/{job_id}/resume/preview?layout={layout}")
    assert preview.status_code == 200 and preview.json()["page_count"] == 1
    assert f'class="resume-document resume-layout-{layout}"' in pdf_builder[-1]
    page = client.get(preview.json()["pages"][0])
    assert page.status_code == 200 and page.content.startswith(b"\x89PNG")
    assert client.get(preview.json()["pages"][0].replace("/1.png", "/2.png")).status_code == 404
    assert client.get(f"/jobs/{job_id}/resume/preview/1.png?layout={layout}&version=old").status_code == 409
    response = client.get(f"/jobs/{job_id}/resume/pdf?layout=compact")
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert '.pdf"' in response.headers["content-disposition"]
    assert PdfReader(BytesIO(response.content)).metadata.subject == "classic"
    with client.app.state.session_factory() as session:
        application = session.get(Job, job_id).application
        assert application.resume_layout == "classic" and application.approval == token
        assert application.accepted_package == accepted
    assert choose(client, job_id, layout).status_code == 303
    build_count = len(pdf_builder)
    saved_pdf = client.get(f"/jobs/{job_id}/resume/pdf")
    assert saved_pdf.status_code == 200
    # A saved choice reuses the exact PDF previously used for that preview.
    assert PdfReader(BytesIO(saved_pdf.content)).metadata.subject == layout
    assert len(pdf_builder) == build_count


def test_pdf_rejects_unaccepted_stale_and_invalid_layouts(client, sample_profile, pdf_builder):
    job_id = setup(client, sample_profile)
    for route in ("pdf", "preview"):
        assert client.get(f"/jobs/{job_id}/resume/{route}").status_code == 400
    assert not pdf_builder
    assert client.get("/jobs/999999/resume/pdf").status_code == 404
    assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303
    assert client.get(f"/jobs/{job_id}/resume/preview?layout=../../bad").status_code == 422
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        job.revision += 1
        session.commit()
    for route in ("pdf", "preview", "preview/1.png?version=old"):
        assert client.get(f"/jobs/{job_id}/resume/{route}").status_code == 409
    assert not pdf_builder


def test_pdf_renderer_failure_keeps_print_and_word_available(client, sample_profile, monkeypatch):
    job_id = accepted_job(client, sample_profile)
    def unavailable(*args):
        raise resume_pdf.PDFError(resume_pdf.SETUP_MESSAGE)
    monkeypatch.setattr(resume_pdf, "_build", unavailable)
    response = client.get(f"/jobs/{job_id}/resume/preview")
    assert response.status_code == 503 and "download Word" in response.json()["detail"]
    assert client.get(f"/jobs/{job_id}/resume/pdf").status_code == 503
    assert client.get(f"/jobs/{job_id}/resume/print").status_code == 200
    assert client.get(f"/jobs/{job_id}/resume/docx").status_code == 200


def test_pdf_cache_changes_with_selected_facts_and_preview_uses_accepted_selection(client, sample_profile, pdf_builder):
    job_id = accepted_job(client, sample_profile)
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        candidate = profile.get_candidate(session)
        selected = resume.profile_draft(candidate.profile).model_copy(update={"projects": [], "skills": []})
        proposal = resume.propose(session, job, candidate, selected)
        resume.accept(session, job, candidate, resume.proposal_token(proposal))
    response = client.get(f"/jobs/{job_id}/resume/pdf")
    assert response.status_code == 200
    assert "Sample Analytics" in pdf_builder[0]
    assert "TaskBot" not in pdf_builder[0] and "<h2>Skills</h2>" not in pdf_builder[0]
    client.get(f"/jobs/{job_id}/resume/preview")
    assert len(pdf_builder) == 1


def test_submitted_pdf_is_frozen_after_live_profile_and_layout_changes(client, sample_profile, pdf_builder):
    job_id = accepted_job(client, sample_profile)
    choose(client, job_id, "modern")
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        candidate = profile.get_candidate(session)
        approve(session, job, candidate)
        record_applied(session, job, candidate)
    url = f"/jobs/{job_id}/snapshots/1/resume.pdf"
    before = client.get(url)
    choose(client, job_id, "compact")
    with client.app.state.session_factory() as session:
        changed = copy.deepcopy(sample_profile)
        changed["contact"]["name"] = "New live name"
        profile.save(session, changed)
    after = client.get(url)
    assert before.status_code == after.status_code == 200 and before.content == after.content
    assert len(pdf_builder) == 1 and 'class="resume-document resume-layout-modern"' in pdf_builder[0]
    assert "New live name" not in pdf_builder[0]
    assert client.get(f"/jobs/{job_id}/snapshots/1/resume/preview").status_code == 200
    assert client.get(f"/jobs/{job_id}/snapshots/999/resume.pdf").status_code == 404


@pytest.mark.parametrize("layout", ["classic", "modern", "compact"])
def test_real_pdf_keeps_a4_selected_text_and_actual_page_previews(session, sample_profile, layout):
    pytest.importorskip("playwright.sync_api")
    try:
        executable = chromium()
    except LookupError:
        pytest.skip("Optional Chromium is not installed")
    # Make a long resume to exercise real pagination and image generation.
    sample_profile["experience"][0]["bullets"] *= 12
    profile.save(session, sample_profile)
    candidate = profile.get_candidate(session)
    rendered = resume.render_context(candidate.profile, resume.profile_draft(candidate.profile))
    document = resume_pdf.export_resume(rendered, layout, executable)
    reader = PdfReader(BytesIO(document.content))
    assert len(reader.pages) == document.pages > 1
    for page in reader.pages:
        assert float(page.mediabox.width) == pytest.approx(595.3, abs=1)
        assert float(page.mediabox.height) == pytest.approx(841.9, abs=1)
    text = " ".join(page.extract_text() for page in reader.pages)
    for value in ("Jordan Example", "Sample Analytics", "TaskBot", "Example State University"):
        assert value in text
    assert document.image(1).startswith(b"\x89PNG")
    assert document.image(document.pages).startswith(b"\x89PNG")
    assert resume_pdf.export_resume(rendered, layout, executable) is document
