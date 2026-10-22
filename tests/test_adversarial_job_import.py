"""Hostile public posting responses stay errors on the import form, without saving a job."""

from __future__ import annotations

import json

import httpx
import pytest

from app.models import Job
from tests.greenhouse_fake import POSTING_URL


def _import_response(client, response):
    client.app.state.greenhouse_transport = httpx.MockTransport(lambda request: response)
    result = client.post("/jobs/import", data={"url": POSTING_URL})
    with client.app.state.session_factory() as session:
        assert session.query(Job).count() == 0
    return result


@pytest.mark.parametrize("length", ["9" * 5000, "0" * 5000 + "9999999"])
def test_oversized_numeric_content_length_is_a_plain_import_error(client, length):
    response = httpx.Response(200, headers={"Content-Length": length}, content=b"{}")
    result = _import_response(client, response)
    assert result.status_code == 502
    assert "over 2 MB" in result.text


@pytest.mark.parametrize("change", [
    {"title": "Engineer\ud800"},
    {"company_name": "Employer\udfff"},
    {"location": {"name": "City\ud800"}},
    {"content": "<p>Role\udfff</p>"},
    {"questions": [{"label": "Why?\ud800", "fields": [{"type": "input_text"}]}]},
    {"questions": [{"label": "Why?", "fields": [{"type": "unknown\ud800"}]}]},
    {"questions": [{"label": "Choose", "fields": [{"type": "multi_value_single_select",
                                                   "values": [{"label": "Choice\udfff"}]}]}]},
    {"demographic_questions": {"questions": [{"label": "Describe", "type": "unknown\ud800"}]}},
])
def test_unpaired_json_surrogates_are_a_plain_import_error(client, change):
    payload = {"title": "Engineer", "content": "<p>A role.</p>"} | change
    # JSON permits an escaped surrogate even though it cannot be rendered as UTF-8.
    response = httpx.Response(200, content=json.dumps(payload).encode("ascii"))
    result = _import_response(client, response)
    assert result.status_code == 502
    assert "wasn&#39;t a job posting" in result.text


def test_valid_surrogate_pairs_keep_their_unicode_text(client):
    payload = {"title": "Engineer 🚀", "content": "<p>Build useful tools 🌍.</p>"}
    response = httpx.Response(200, content=json.dumps(payload).encode("ascii"))
    result = _import_response(client, response)
    assert result.status_code == 200
    assert "Engineer 🚀" in result.text
    assert "Build useful tools 🌍." in result.text


def test_leading_zeroes_do_not_make_a_small_content_length_too_large(client):
    body = b'{"title":"Engineer","content":"<p>A role.</p>"}'
    response = httpx.Response(200, headers={"Content-Length": "0" * 5000 + str(len(body))}, content=body)
    assert _import_response(client, response).status_code == 200
