"""The public resume collection is readable and never changes an application."""

import copy
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest

from app.models import Job
from app.services import profile, resume_pdf
from tests.test_package import approve
from tests.test_resume_export_routes import accepted_job


class Markup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def elements(self, tag):
        return [attrs for kind, attrs in self.tags if kind == tag]


def test_public_collection_has_local_demos_and_attributed_hiring_sources(client, monkeypatch):
    def unavailable(*args, **kwargs):
        raise AssertionError("The public collection must not need PDF rendering")
    monkeypatch.setattr(resume_pdf, "_build", unavailable)

    page = client.get("/resume-examples")
    assert page.status_code == 200
    markup = Markup(page.text)
    assert len(markup.elements("main")) == 1
    images = markup.elements("img")
    assert len(images) == 3
    for image in images:
        assert urlsplit(image["src"]).netloc == ""
        assert "fictional" in image["alt"]
        response = client.get(image["src"])
        assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
        assert response.content.startswith(b"RIFF")
    sources = [link for link in markup.elements("a")
               if urlsplit(link.get("href", "")).netloc == "www.kickresume.com"]
    assert len(sources) == 6
    assert all(link.get("target") == "_blank" and "noopener" in link.get("rel", "") for link in sources)
    assert all(any(company in link["href"] for link in sources) for company in ("google", "amazon", "ibm"))
    assert page.text.count("Hire reported by Kickresume") == 6
    with client.app.state.session_factory() as session:
        assert profile.get_candidate(session) is None


def test_hiring_filters_preserve_job_context_and_have_an_empty_state(client, sample_profile):
    job_id = accepted_job(client, sample_profile)
    filtered = client.get(f'/resume-examples?job_id={job_id}&role=data&level=lead')
    assert filtered.status_code == 200
    assert filtered.text.count('Hire reported by Kickresume') == 1
    assert 'Lead Data Analyst' in filtered.text
    assert f'name="job_id" value="{job_id}"' in filtered.text
    assert 'No examples match' in client.get('/resume-examples?role=marketing&level=intern').text
    assert client.get('/resume-examples?role=made-up').status_code == 400


@pytest.mark.parametrize("layout", ["classic", "modern", "compact"])
def test_public_detail_has_readable_fictional_resume_without_profile_or_renderer(client, monkeypatch, layout):
    def unavailable(*args, **kwargs):
        raise AssertionError("A sample detail must not need PDF rendering")
    monkeypatch.setattr(resume_pdf, "_build", unavailable)
    page = client.get(f"/resume-examples/{layout}")
    assert page.status_code == 200
    markup = Markup(page.text)
    assert len(markup.elements("main")) == 1
    assert "Fictional sample" in page.text and "Read the sample resume as text" in page.text
    for fact in ("Avery Morgan", "Northstar Studio", "Open Trail", "Cascadia Institute", "Python"):
        assert fact in page.text
    assert 'href="/jobs"' in page.text
    with client.app.state.session_factory() as session:
        assert profile.get_candidate(session) is None


@pytest.mark.parametrize("path", ["/resume-examples/missing", "/resume-examples?job_id=99999",
                                  "/resume-examples/modern?job_id=99999"])
def test_collection_rejects_missing_layouts_and_jobs(client, path):
    assert client.get(path).status_code == 404


def test_collection_preserves_job_context_and_preview_keeps_accepted_package(client, sample_profile):
    job_id = accepted_job(client, sample_profile)
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        approve(session, job, profile.get_candidate(session))
        accepted = copy.deepcopy(job.application.accepted_package)
        approval = copy.deepcopy(job.application.approval)
    gallery = client.get(f"/resume-examples?job_id={job_id}")
    assert gallery.status_code == 200
    for layout in ("classic", "modern", "compact"):
        assert f'href="/resume-examples/{layout}?job_id={job_id}"' in gallery.text
        detail = client.get(f"/resume-examples/{layout}?job_id={job_id}")
        assert detail.status_code == 200
        assert f'href="/resume-examples?job_id={job_id}"' in detail.text
        assert f'href="/jobs/{job_id}/resume/print?preview_layout={layout}"' in detail.text
        assert client.get(f"/jobs/{job_id}/resume/print?preview_layout={layout}").status_code == 200
    assert client.get(f"/jobs/{job_id}/resume/print?preview_layout=missing").status_code == 409
    with client.app.state.session_factory() as session:
        application = session.get(Job, job_id).application
        assert application.resume_layout == "classic"
        assert application.accepted_package == accepted and application.approval == approval
