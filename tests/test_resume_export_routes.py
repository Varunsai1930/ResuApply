"""Accepted-only downloads, layout review tokens, and frozen Word export over HTTP."""

import copy
from email.parser import HeaderParser
from io import BytesIO

import pytest
from docx import Document
from sqlalchemy import text

from app.models import Job
from app.routes.resume_download import docx_response
from app.services import package, profile, resume
from app.services.resume_layouts import layout_key
from tests.test_package import approve, record_applied
from tests.test_resume_routes import accept, manual_proposal, setup
from tests.test_routes import hidden


def accepted_job(client, sample_profile):
    job_id = setup(client, sample_profile)
    assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303
    return job_id


def word_text(response):
    assert response.status_code == 200, response.text[:300]
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    document = Document(BytesIO(response.content))
    return document, "\n".join(p.text for p in document.paragraphs)


def choose(client, job_id, layout, token=None):
    if token is None:
        token = hidden(client.get(f"/jobs/{job_id}/resume/print").text, "package_token")
    return client.post(f"/jobs/{job_id}/resume/layout", data={"layout": layout, "package_token": token},
                       follow_redirects=False)


@pytest.mark.parametrize("layout", ["classic", "modern", "compact"])
def test_current_download_and_print_use_persisted_layout_and_selected_facts(client, sample_profile, layout):
    job_id = accepted_job(client, sample_profile)
    assert choose(client, job_id, layout).status_code == 303
    printed = client.get(f"/jobs/{job_id}/resume/print")
    assert f"resume-layout-{layout}" in printed.text
    assert f'/jobs/{job_id}/resume/docx' in printed.text
    response = client.get(f"/jobs/{job_id}/resume/docx")
    document, output = word_text(response)
    assert sample_profile["contact"]["name"] in output and sample_profile["contact"]["email"] in output
    assert "Sample Analytics" in output and "Coursework: Data Structures, Operating Systems, Databases" in output
    assert "Technologies: Python, Flask, PostgreSQL" in output
    assert document.sections[0].page_width.mm == pytest.approx(210, abs=0.1)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    with client.app.state.session_factory() as session:
        application = session.get(Job, job_id).application
        assert layout_key(application) == layout
        assert application.accepted_package is not None


def test_download_excludes_unselected_entries_bullets_and_skills(client, sample_profile):
    job_id = setup(client, sample_profile)
    with client.app.state.session_factory() as session:
        candidate = profile.get_candidate(session)
        job = session.get(Job, job_id)
        draft = resume.profile_draft(candidate.profile).model_copy(update={"projects": [], "skills": []})
        record = resume.propose(session, job, candidate, draft)
        resume.accept(session, job, candidate, resume.proposal_token(record))
    _, output = word_text(client.get(f"/jobs/{job_id}/resume/docx"))
    assert "Sample Analytics" in output
    assert sample_profile["projects"][0]["name"] not in output
    assert "Projects" not in output and "Skills" not in output


def test_current_download_refuses_unaccepted_and_stale_resumes(client, sample_profile):
    job_id = setup(client, sample_profile)
    assert client.get(f"/jobs/{job_id}/resume/docx").status_code == 400
    assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        job.revision += 1
        session.commit()
    response = client.get(f"/jobs/{job_id}/resume/docx")
    assert response.status_code == 409 and "out of date" in response.text
    assert choose(client, job_id, "modern", "old-token").status_code == 409
    assert client.get("/jobs/99999/resume/docx").status_code == 404


def test_layout_change_requires_fresh_token_and_clears_only_current_approval(client, sample_profile):
    job_id = accepted_job(client, sample_profile)
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        candidate = profile.get_candidate(session)
        approve(session, job, candidate)
        accepted = job.application.accepted_package.model_dump(mode="json")
        original_hash = job.application.approval.content_hash
        assert "_layout" not in package.resolved_package(job, job.application, candidate)["resume"]
    original_token = hidden(client.get(f"/jobs/{job_id}/resume/print").text, "package_token")
    assert choose(client, job_id, "classic", original_token).status_code == 303
    with client.app.state.session_factory() as session:
        assert session.get(Job, job_id).application.approval.content_hash == original_hash
    assert choose(client, job_id, "modern", original_token).status_code == 303
    with client.app.state.session_factory() as session:
        application = session.get(Job, job_id).application
        assert application.resume_layout == "modern" and application.approval is None
        assert application.accepted_package.model_dump(mode="json") == accepted
    assert choose(client, job_id, "compact", original_token).status_code == 409
    assert choose(client, job_id, "../../other").status_code == 422
    with client.app.state.session_factory() as session:
        assert session.get(Job, job_id).application.resume_layout == "modern"


def test_layout_and_word_export_of_snapshot_survive_live_profile_and_layout_changes(client, sample_profile):
    job_id = accepted_job(client, sample_profile)
    assert choose(client, job_id, "modern").status_code == 303
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        candidate = profile.get_candidate(session)
        approve(session, job, candidate)
        snapshot = record_applied(session, job, candidate)
        assert snapshot.resume["_layout"] == "modern"
        assert "resume-layout-modern" in snapshot.resume_html
        frozen_raw = session.scalar(text("SELECT submitted_snapshots FROM applications WHERE job_id = :job_id"),
                                    {"job_id": job_id})
    frozen_url = f"/jobs/{job_id}/snapshots/1/resume.docx"
    before, before_text = word_text(client.get(frozen_url))
    assert choose(client, job_id, "compact").status_code == 303
    with client.app.state.session_factory() as session:
        edited = copy.deepcopy(sample_profile)
        edited["contact"]["name"] = "Changed Current Name"
        profile.save(session, edited)
        assert session.scalar(text("SELECT submitted_snapshots FROM applications WHERE job_id = :job_id"),
                              {"job_id": job_id}) == frozen_raw
    after, after_text = word_text(client.get(frozen_url))
    assert before_text == after_text and "Changed Current Name" not in after_text
    assert before.styles["Normal"].font.name == after.styles["Normal"].font.name == "Calibri"
    assert client.get(f"/jobs/{job_id}/resume/docx").status_code == 409
    saved_print = client.get(f"/jobs/{job_id}/snapshots/1/resume").text
    assert "resume-layout-modern" in saved_print and 'name="layout"' not in saved_print
    assert frozen_url in saved_print
    assert client.get(f"/jobs/{job_id}/snapshots/999/resume.docx").status_code == 404


def test_download_attachment_safely_encodes_unicode_filename(client, sample_profile):
    changed = copy.deepcopy(sample_profile)
    changed["contact"]["name"] = 'Zoë / "Example"'
    job_id = accepted_job(client, changed)
    response = client.get(f"/jobs/{job_id}/resume/docx")
    _, output = word_text(response)
    assert changed["contact"]["name"] in output
    header = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in header and "Zo%C3%AB" in header
    assert "\r" not in header and "\n" not in header


@pytest.mark.parametrize("name", ['Ａ＂Ｂ', 'Ａ／Ｂ', 'Ａ＼Ｂ', 'Ａ：Ｂ', '姓名'])
def test_normalized_attachment_filename_remains_a_valid_docx_filename(name):
    header = docx_response(b"synthetic docx", name, "Acme").headers["content-disposition"]
    parsed = HeaderParser().parsestr("Content-Disposition: " + header + "\n\n")
    assert parsed.get_content_disposition() == "attachment"
    assert parsed.get_filename().endswith(".docx")
    assert header.count('"') == 2
    assert "filename*=UTF-8''" in header
