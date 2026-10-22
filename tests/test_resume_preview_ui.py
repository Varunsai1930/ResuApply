"""Layout previews change immediately and stay separate from saved exports."""

import copy
import re
from urllib.parse import urlsplit

import pytest
from pypdf import PdfReader

from app.models import Job
from app.services import profile, resume_pdf
from tests.autofill_site import chromium
from tests.test_resume_export_routes import accepted_job
from tests.test_package import approve
from tests.test_resume_pdf import isolated_pdf_cache, pdf_builder  # noqa: F401 (fixtures)


def browser_runtime():
    api = pytest.importorskip("playwright.sync_api")
    try:
        executable = chromium()
    except LookupError:
        pytest.skip("Optional Chromium is not installed")
    return api, executable


def route_app(page, client, requests):
    def serve(route):
        url = urlsplit(route.request.url)
        requests.append((route.request.method, url.path))
        # Resolve redirects inside TestClient: Playwright doesn't re-route a
        # browser request that follows a fulfilled redirect response.
        response = client.request(route.request.method, url.path + ("?" + url.query if url.query else ""),
                                  content=route.request.post_data_buffer, follow_redirects=True,
                                  headers={"content-type": route.request.headers.get("content-type", "")})
        headers = {key: value for key, value in response.headers.items()
                   if key in ("content-type", "content-disposition", "location", "cache-control")}
        route.fulfill(status=response.status_code, body=response.content, headers=headers)
    page.route("**/*", serve)


@pytest.mark.parametrize("width,height", [(1273, 900), (390, 844)])
def test_live_layout_preview_apply_and_direct_download(client, sample_profile, pdf_builder, tmp_path, width, height):
    api, executable = browser_runtime()
    job_id = accepted_job(client, sample_profile)
    requests = []
    with api.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable)
        try:
            page = browser.new_page(viewport={"width": width, "height": height}, accept_downloads=True)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            route_app(page, client, requests)
            page.goto(f"http://127.0.0.1:8000/jobs/{job_id}/resume/print")
            panel = page.locator('[data-resume-preview]')
            api.expect(panel).to_have_class(re.compile("has-page-preview"))
            page.get_by_role("radio", name=re.compile("^Modern")).check()
            api.expect(page.locator('.resume-document')).to_have_class(re.compile("resume-layout-modern"))
            api.expect(page.get_by_role("link", name="Download PDF", exact=True)).to_have_attribute("aria-disabled", "true")
            api.expect(panel).to_have_class(re.compile("has-page-preview"))
            assert "layout=modern" in panel.locator('img').first.get_attribute("src")
            assert not any(method == "POST" for method, _ in requests)
            with client.app.state.session_factory() as session:
                assert session.get(Job, job_id).application.resume_layout == "classic"
            page.get_by_role("button", name="Apply layout", exact=True).click()
            api.expect(page.get_by_role("radio", name=re.compile("^Modern"))).to_be_checked()
            api.expect(panel).to_have_class(re.compile("has-page-preview"))
            api.expect(page.get_by_role("link", name="Download PDF", exact=True)).not_to_have_attribute("aria-disabled", "true")
            with page.expect_download() as download:
                page.get_by_role("link", name="Download PDF", exact=True).click()
            target = tmp_path / "resume.pdf"
            download.value.save_as(target)
            assert download.value.suggested_filename.endswith(".pdf")
            assert PdfReader(target).metadata.subject == "modern"
            # A newer selection must win over any earlier pending preview.
            for layout in ("Classic", "Compact", "Modern"):
                page.get_by_role("radio", name=re.compile("^" + layout)).check()
            api.expect(panel).to_have_class(re.compile("has-page-preview"))
            api.expect(panel.locator('img').first).to_have_attribute("src", re.compile("layout=modern"))
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
        finally:
            browser.close()


def test_page_preview_can_retry_without_losing_printable_text(client, sample_profile, pdf_builder, monkeypatch):
    api, executable = browser_runtime()
    job_id = accepted_job(client, sample_profile)
    build = resume_pdf._build
    def unavailable(*args):
        raise resume_pdf.PDFError("The PDF could not be prepared. Use Print / save PDF.")
    monkeypatch.setattr(resume_pdf, "_build", unavailable)
    with api.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable)
        try:
            page = browser.new_page()
            route_app(page, client, [])
            page.goto(f"http://127.0.0.1:8000/jobs/{job_id}/resume/print")
            api.expect(page.locator('[data-preview-status]')).to_contain_text("could not be prepared")
            api.expect(page.get_by_role("heading", name="Jordan Example", exact=True)).to_be_visible()
            monkeypatch.setattr(resume_pdf, "_build", build)
            page.get_by_role("button", name="Retry preview", exact=True).click()
            api.expect(page.locator('[data-resume-preview]')).to_have_class(re.compile("has-page-preview"))
            # Printing must use the selectable HTML, never duplicate its preview images.
            page.emulate_media(media="print")
            api.expect(page.locator('[data-resume-pages]')).to_be_hidden()
            api.expect(page.get_by_role("heading", name="Jordan Example", exact=True)).to_be_visible()
        finally:
            browser.close()


@pytest.mark.parametrize("width,height", [(1273, 900), (390, 844)])
def test_gallery_layout_preview_preserves_reviewed_resume(client, sample_profile, pdf_builder, width, height):
    api, executable = browser_runtime()
    job_id = accepted_job(client, sample_profile)
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        approve(session, job, profile.get_candidate(session))
        accepted = copy.deepcopy(job.application.accepted_package)
        approval = copy.deepcopy(job.application.approval)
    requests = []
    with api.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable)
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            route_app(page, client, requests)
            page.goto(f"http://127.0.0.1:8000/resume-examples?job_id={job_id}")
            assert page.get_by_role("main").count() == 1
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("link", name="Preview Modern", exact=True).click()
            api.expect(page).to_have_url(re.compile(rf"/resume-examples/modern\?job_id={job_id}$"))
            assert page.get_by_role("main").count() == 1
            page.get_by_text("Read the sample resume as text", exact=True).click()
            api.expect(page.get_by_role("heading", name="Avery Morgan", exact=True)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.get_by_role("link", name="Preview with my resume").click()
            api.expect(page.get_by_role("radio", name=re.compile("^Modern"))).to_be_checked()
            api.expect(page.locator('.resume-document')).to_have_class(re.compile("resume-layout-modern"))
            api.expect(page.locator('[data-resume-preview]')).to_have_class(re.compile("has-page-preview"))
            api.expect(page.get_by_role("link", name="Download PDF", exact=True)).to_have_attribute("aria-disabled", "true")
            api.expect(page.get_by_role("button", name="Apply layout", exact=True)).to_be_enabled()
            assert not any(method == "POST" for method, _ in requests)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
            with client.app.state.session_factory() as session:
                application = session.get(Job, job_id).application
                assert application.resume_layout == "classic"
                assert application.accepted_package == accepted and application.approval == approval
        finally:
            browser.close()
