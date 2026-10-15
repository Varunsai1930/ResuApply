"""Local OCR is explicit, previewed, and never saves or overwrites profile facts."""

from __future__ import annotations

import json

import pytest

from app.services import resume_ocr
from tests.resume_files import blank_pdf_bytes, pdf_bytes
from tests.test_profile_import import candidates
from tests.test_profile_sections import form_fields
from tests.test_resume_draft import RESUME
from tests.test_routes import client_profile, hidden, profile_form, review_and_save


def upload(client, *, ocr=None, data=None):
    fields = {} if ocr is None else {"use_ocr": ocr}
    return client.post("/profile/import", data=fields,
                       files={"file": ("scanned.pdf", data or blank_pdf_bytes(), "application/pdf")})


def test_import_page_explains_opt_in_local_english_ocr(client):
    page = client.get("/profile/import").text
    assert 'name="use_ocr" type="checkbox" value="1"' in page
    assert "Tesseract installed on this computer" in page and "OCR can misread" in page
    assert "OCR" in page and "English" in page


@pytest.mark.parametrize("option", [None, "0", "true", "yes"])
def test_non_opted_in_requests_do_not_start_ocr(client, monkeypatch, option):
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: pytest.fail("not opted in"))
    response = upload(client, ocr=option)
    assert response.status_code == 422 and "Use OCR for scanned pages" in response.text
    assert candidates(client) == 0


def test_ocr_preview_warns_and_keeps_original_text_through_review(client, monkeypatch):
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: {0: RESUME})
    response = upload(client, ocr="1")
    assert response.status_code == 200 and candidates(client) == 0
    assert "OCR was used for scanned pages" in response.text and "OCR can misread names" in response.text
    fields = form_fields(response.text)
    assert fields["contact-name"] == "Jordan Example"
    assert json.loads(fields["imported"])["ocr"] is True
    error = client.post("/profile/review", data=fields | {"experience-0-end": "2024-01"})
    assert error.status_code == 422 and "OCR was used" in error.text
    reviewed = client.post("/profile/review", data=fields)
    assert reviewed.status_code == 200 and candidates(client) == 0
    back = client.post("/profile/edit", data={"payload": hidden(reviewed.text, "payload"),
                                              "imported": hidden(reviewed.text, "imported")})
    assert back.status_code == 200 and "OCR was used" in back.text and "Text read from the resume" in back.text
    saved = client.post("/profile/save", data={"payload": hidden(reviewed.text, "payload"),
                                              "base_revision": hidden(reviewed.text, "base_revision")},
                        follow_redirects=False)
    assert saved.status_code == 303 and candidates(client) == 1


def test_text_pdf_has_no_ocr_warning_when_box_is_checked(client, monkeypatch):
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: pytest.fail("no scan"))
    response = upload(client, ocr="1", data=pdf_bytes(RESUME.splitlines()))
    assert response.status_code == 200 and "OCR was used" not in response.text
    assert candidates(client) == 0


def test_existing_profile_merge_keeps_saved_facts(client, sample_profile, monkeypatch):
    sample_profile["contact"]["name"] = "Saved Correct Name"
    sample_profile["contact"]["phone"] = ""
    review_and_save(client, profile_form(sample_profile))
    original = client_profile(client)
    monkeypatch.setattr(resume_ocr, "recognize_pages", lambda *args, **kwargs: {0: RESUME})
    response = upload(client, ocr="1")
    assert response.status_code == 200 and "OCR was used" in response.text
    fields = form_fields(response.text)
    assert fields["contact-name"] == "Saved Correct Name" and fields["contact-phone"] == "+1 555 0100"
    assert client_profile(client) == original


@pytest.mark.parametrize("message", ["OCR needs Tesseract installed on PATH.", "OCR took too long."])
def test_ocr_failures_change_no_data(client, monkeypatch, message):
    def fail(*args, **kwargs):
        raise resume_ocr.OCRError(message)

    monkeypatch.setattr(resume_ocr, "recognize_pages", fail)
    response = upload(client, ocr="1")
    assert response.status_code == 422 and message in response.text
    assert candidates(client) == 0
