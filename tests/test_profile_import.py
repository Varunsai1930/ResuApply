"""Importing a resume file into the profile form, over HTTP. Synthetic text only."""

from __future__ import annotations

from app.models import Candidate
from app.services.resume_import import MAX_BYTES
from tests.resume_files import blank_pdf_bytes, docx_bytes, pdf_bytes
from tests.test_profile_sections import form_fields
from tests.test_resume_draft import RESUME
from tests.test_routes import client_profile, hidden, profile_form, review_and_save

LINES = [line for line in RESUME.splitlines()]


def upload(client, data: bytes, name="resume.pdf", kind="application/pdf"):
    return client.post("/profile/import", files={"file": (name, data, kind)})


def candidates(client) -> int:
    with client.app.state.session_factory() as session:
        return session.query(Candidate).count()


def test_the_import_page_takes_a_pdf_or_word_file(client):
    page = client.get("/profile/import").text
    assert 'enctype="multipart/form-data"' in page and 'name="file" type="file" accept=".pdf,.docx' in page
    assert 'href="/profile/import">Import a resume' in client.get("/welcome").text
    assert 'href="/profile/import">Import from a resume' in client.get("/profile").text


def test_a_resume_fills_the_profile_form_and_saves_only_after_review(client):
    response = upload(client, pdf_bytes(LINES))
    assert response.status_code == 200 and candidates(client) == 0  # nothing saved yet
    page = response.text
    assert "Read from resume.pdf. Check every field before reviewing." in page
    assert "add your work authorization yourself" in page
    assert "Text read from the resume" in page and "Dean&#39;s List 2024" in page  # the whole text, to copy from
    fields = form_fields(page)
    assert fields["contact-name"] == "Jordan Example" and fields["contact-email"] == "jordan@example.com"
    assert fields["experience-0-organization"] == "Sample Analytics" and fields["experience-0-start"] == "2024-06"
    review = client.post("/profile/review", data=fields)
    assert review.status_code == 200, review.text
    saved = client.post("/profile/save", data={"payload": hidden(review.text, "payload"),
                                               "base_revision": hidden(review.text, "base_revision")},
                        follow_redirects=False)
    assert saved.status_code == 303
    profile = client_profile(client)
    assert profile["experience"][0]["bullets"][0]["text"].startswith("Built a Flask REST API")
    assert profile["experience"][0]["bullets"][0]["id"] == "exp-1-b1"  # IDs are given when saved


def test_with_a_profile_only_new_facts_are_added(client, sample_profile):
    sample_profile["contact"]["phone"] = ""
    review_and_save(client, profile_form(sample_profile))
    resume = docx_bytes(LINES + ["EXPERIENCE", "Research Assistant | Example Labs | Jan 2025 - Present",
                                 "- Cleaned survey data in Python"])
    response = upload(client, resume, "cv.docx",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    page = response.text
    assert response.status_code == 200
    assert "Added to your profile: Phone, 1 experience entry, 2 skills, 1 certification." in page
    fields = form_fields(page)
    assert fields["contact-name"] == "Jordan Example" and fields["contact-phone"] == "+1 555 0100"
    organizations = [v for k, v in fields.items() if k.endswith("-organization")]
    assert organizations == ["Sample Analytics", "Example Labs"]  # the saved entry kept, the new one added
    assert fields["experience-0-id"] == "exp-1"  # the saved entry keeps its ID


def test_a_resume_with_nothing_new_says_so(client, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    page = upload(client, pdf_bytes(["Jordan Example", "jordan@example.com", "", "SKILLS", "Python, SQL"])).text
    assert "Nothing new was found in resume.pdf" in page


def test_unreadable_files_say_why_and_change_nothing(client):
    for data, message in ((blank_pdf_bytes(), "scanned or image-only"), (b"GIF89a", "Choose a PDF or Word"),
                          (b"x" * (MAX_BYTES + 1), "over 10 MB")):
        response = upload(client, data)
        assert response.status_code == 422 and message in response.text
    missing = client.post("/profile/import", data={})
    assert missing.status_code == 422 and "The file is empty" in missing.text
    assert candidates(client) == 0


def test_the_sample_profile_must_go_first(client):
    client.post("/sample")
    refused = upload(client, pdf_bytes(LINES))
    assert refused.status_code == 409 and "Remove the sample data first" in refused.text
    assert 'name="file"' not in refused.text
    assert "Import from a resume" not in client.get("/profile").text


def test_the_file_name_and_text_are_escaped(client):
    lines = ["Jordan Example", "jordan@example.com", "SUMMARY", "<script>alert(1)</script> builds services"]
    page = upload(client, pdf_bytes(lines), name="<img src=x onerror=alert(1)>cv.pdf").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x" not in page and "Read from &lt;img src=x onerror=alert(1)&gt;cv.pdf" in page


def test_only_the_files_own_name_is_shown(client):
    page = upload(client, pdf_bytes(LINES), name="C:\\Users\\someone\\Documents\\cv.pdf").text
    assert "Read from cv.pdf." in page and "someone" not in page  # an old browser's full path is cut


def test_the_text_read_from_the_resume_stays_through_review_and_back(client):
    fields = form_fields(upload(client, pdf_bytes(LINES)).text)
    refused = client.post("/profile/review", data=fields | {"experience-0-end": "2024-01"})  # ends before it starts
    assert refused.status_code == 422
    assert "Read from resume.pdf" in refused.text and "Dean&#39;s List 2024" in refused.text
    review = client.post("/profile/review", data=form_fields(refused.text) | {"experience-0-end": "2024-08"})
    assert review.status_code == 200, review.text
    back = client.post("/profile/edit", data={"payload": hidden(review.text, "payload"),
                                              "imported": hidden(review.text, "imported")})
    assert back.status_code == 200
    assert "Read from resume.pdf" in back.text and "Dean&#39;s List 2024" in back.text
    assert "add your work authorization yourself" in back.text


def test_a_damaged_carried_import_note_is_left_out(client):
    fields = form_fields(upload(client, pdf_bytes(LINES)).text)
    for damaged in ("not json", '{"filename": 1}', '["text"]', '{"filename": "a", "text": "b", "added": [1]}'):
        page = client.post("/profile/review", data=fields | {"imported": damaged, "experience-0-end": "2024-01"})
        assert page.status_code == 422 and "Read from" not in page.text


def test_the_draft_is_read_off_the_event_loop(client, monkeypatch):
    import asyncio

    from app.services import resume_draft

    on_loop = []
    original = resume_draft.draft_profile

    def draft(text):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:  # a thread-pool worker has no running loop
            on_loop.append(False)
        return original(text)

    monkeypatch.setattr(resume_draft, "draft_profile", draft)
    assert upload(client, pdf_bytes(LINES)).status_code == 200
    assert on_loop == [False]


def test_an_upload_over_the_limit_is_refused_before_it_is_read(client, monkeypatch):
    from starlette.requests import Request

    read = []
    original = Request.form

    def form(self, *args, **kwargs):
        read.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Request, "form", form)
    response = upload(client, b"%PDF-" + b"x" * (MAX_BYTES + 200_000))
    assert response.status_code == 413 and "over 10 MB" in response.text
    assert read == []  # refused from its Content-Length, before the body was spooled
    assert candidates(client) == 0
