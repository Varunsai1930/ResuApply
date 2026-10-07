"""Milestone 2 over HTTP: requirements editor, AI extraction, sharing preview, evidence and overrides."""

from __future__ import annotations

import copy
import re
import sqlite3
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.db import init_db, make_engine, make_session_factory
from app.models import Job
from tests.conftest import BASE_URL, DEMO_JOB, DEMO_REQUIREMENTS, tool_response
from tests.test_routes import profile_form, review_and_save


def create_job(client: TestClient) -> int:
    response = client.post("/jobs", data=DEMO_JOB, follow_redirects=False)
    return int(re.match(r"/jobs/(\d+)", response.headers["location"]).group(1))


def requirement_form(requirements: list[dict]) -> dict[str, str]:
    """Encode requirements the way the editor submits them."""
    tri = {True: "yes", False: "no", None: ""}
    fields: dict[str, str] = {}
    for i, req in enumerate(requirements):
        p = f"req-{i}"
        crit = req.get("criterion") or {}
        fields |= {f"{p}-id": req.get("id", ""), f"{p}-text": req["text"], f"{p}-category": req.get("category", "other"),
                   f"{p}-importance": req.get("importance", "unspecified"), f"{p}-excerpt": req["excerpt"],
                   f"{p}-ctype": crit.get("type", "")}
        if crit.get("type") == "skill":
            fields |= {f"{p}-skills": ", ".join(crit["skills"]), f"{p}-match": crit.get("match", "all")}
        elif crit.get("type") == "degree":
            fields |= {f"{p}-level": crit["level"], f"{p}-fields": ", ".join(crit.get("fields", [])), f"{p}-status": crit.get("status", "any")}
        elif crit.get("type") == "authorization":
            fields |= {f"{p}-country": crit["country"], f"{p}-sponsorship": tri[crit.get("sponsorship_available")]}
        elif crit.get("type") == "location":
            fields |= {f"{p}-locations": "; ".join(crit.get("locations", [])), f"{p}-work_mode": crit.get("work_mode") or ""}
    return fields


def setup(client: TestClient, sample_profile: dict, requirements: bool = True) -> int:
    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client)
    if requirements:
        response = client.post(f"/jobs/{job_id}/requirements", data=requirement_form(DEMO_REQUIREMENTS), follow_redirects=False)
        assert response.status_code == 303, response.text
    return job_id


def card(html: str, req_id: str) -> str:
    start = html.index(f'id="req-{req_id}"')
    return html[start:html.index("</article>", start)]


# ---------------------------------------------------------------- manual requirements and checklist

def test_manual_requirements_and_checklist_without_ai(client, sample_profile):
    job_id = setup(client, sample_profile)
    page = client.get(f"/jobs/{job_id}?msg=requirements_saved").text
    assert "Requirements saved." in page
    assert "AI is off" in page and "Extract requirements with AI" not in page
    assert '<span class="badge check-met">Met 4</span>' in page
    assert '<span class="badge check-unmet">Unmet 1</span>' in page
    assert '<span class="badge check-unknown">Unknown 2</span>' in page
    # Sources, gaps and unknowns are shown separately, each with excerpt and basis.
    assert page.index("Met: your sources") < page.index("Unmet: gaps") < page.index("Unknown: missing or not comparable")
    rust = card(page, "r6")
    assert "check-unmet" in rust and "“Experience with Rust.”" in rust and "You confirmed you don&#39;t have Rust" in rust
    apis = card(page, "r4")
    assert "check-unknown" in apis and "No comparable criterion; needs confirmed evidence" in apis


def test_editor_round_trips_saved_requirements(client, sample_profile):
    job_id = setup(client, sample_profile)
    editor = client.get(f"/jobs/{job_id}/requirements/edit").text
    assert 'name="req-0-id" value="r1"' in editor
    assert 'value="Python, SQL"' in editor
    assert re.search(r'name="req-2-sponsorship".*?<option value="no" selected>', editor, re.DOTALL)
    # Submitting it unchanged changes nothing.
    response = client.post(f"/jobs/{job_id}/requirements", data=editor_fields(editor), follow_redirects=False)
    assert response.headers["location"].endswith("msg=requirements_unchanged#requirements")


def editor_fields(html: str) -> dict[str, str]:
    html = html.split("<template", 1)[0]
    fields = {}
    for match in re.finditer(r'<(input|textarea|select)[^>]*name="(req-[^"]+)"[^>]*>', html):
        tag, name = match.groups()
        if tag == "input":
            value = re.search(r'value="([^"]*)"', match.group(0))
            fields[name] = unescape(value.group(1)) if value else ""
        elif tag == "textarea":
            fields[name] = unescape(html[match.end():html.index("</textarea>", match.end())])
        else:
            body = html[match.end():html.index("</select>", match.end())]
            selected = re.search(r'<option value="([^"]*)"\s+selected', body)
            fields[name] = selected.group(1) if selected else ""
    return fields


def test_editor_rejects_bad_excerpts_and_keeps_input(client, sample_profile):
    job_id = setup(client, sample_profile, requirements=False)
    reqs = copy.deepcopy(DEMO_REQUIREMENTS[:2])
    reqs[1]["excerpt"] = "PhD in Astrophysics required."
    response = client.post(f"/jobs/{job_id}/requirements", data=requirement_form(reqs))
    assert response.status_code == 422
    assert "The requirements were not saved" in response.text
    assert 'href="#req-1-excerpt"' in response.text
    assert "PhD in Astrophysics required." in response.text
    assert "No requirements yet" in client.get(f"/jobs/{job_id}").text


def test_link_evidence_and_override_over_http(client, sample_profile):
    job_id = setup(client, sample_profile)
    linked = client.post(f"/jobs/{job_id}/requirements/r4/evidence", data={"sources": ["exp-1-b1", "proj-1-b1"]}, follow_redirects=False)
    assert linked.status_code == 303
    page = client.get(f"/jobs/{job_id}").text
    apis = card(page, "r4")
    assert "check-met" in apis and "You confirmed supporting evidence" in apis
    assert "Built a Flask REST API in Python" in apis and "exp-1-b1" in apis

    client.post(f"/jobs/{job_id}/requirements/r4/evidence/remove", data={"source": "proj-1-b1"})
    assert "proj-1-b1" not in card(client.get(f"/jobs/{job_id}").text, "r4").split("Link evidence")[0]

    missing_reason = client.post(f"/jobs/{job_id}/requirements/r5/override", data={"status": "met", "reason": ""})
    assert missing_reason.status_code == 422 and "An override needs a reason." in missing_reason.text
    client.post(f"/jobs/{job_id}/requirements/r5/override", data={"status": "met", "reason": "Kafka in a class project"})
    java = card(client.get(f"/jobs/{job_id}").text, "r5")
    assert "check-met" in java and "Your override: Kafka in a class project (computed: Unknown)" in java

    bad = client.post(f"/jobs/{job_id}/requirements/r4/evidence", data={"sources": ["exp-9-b9"]})
    assert bad.status_code == 422 and "Unknown profile item(s): exp-9-b9" in bad.text


# ---------------------------------------------------------------- AI over HTTP

def test_extract_then_review_then_save(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile, requirements=False)
    assert "Extract requirements with AI" in client.get(f"/jobs/{job_id}").text
    fake_ai.push(tool_response("return_requirements", {"requirements": DEMO_REQUIREMENTS}))
    response = client.post(f"/jobs/{job_id}/requirements/extract", follow_redirects=False)
    assert response.headers["location"] == f"/jobs/{job_id}/requirements/edit?proposal=1"
    editor = client.get(response.headers["location"]).text
    assert "proposed by the AI model" in editor and "Strong experience with Python and SQL." in editor
    assert "No requirements yet" in client.get(f"/jobs/{job_id}").text  # still only a proposal
    assert "Review the AI proposal" in client.get(f"/jobs/{job_id}").text

    saved = client.post(f"/jobs/{job_id}/requirements", data=editor_fields(editor), follow_redirects=False)
    assert saved.headers["location"].endswith("msg=requirements_saved#requirements")
    assert '<span class="badge check-met">Met 4</span>' in client.get(f"/jobs/{job_id}").text
    assert len(fake_ai.requests) == 1


def test_extraction_problems_are_shown_for_correction(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile, requirements=False)
    bad = copy.deepcopy(DEMO_REQUIREMENTS)
    bad[0]["excerpt"] = "Five years of Python."
    fake_ai.push(*[tool_response("return_requirements", {"requirements": bad})] * 2)
    response = client.post(f"/jobs/{job_id}/requirements/extract")
    assert response.status_code == 422
    assert "still has problems after one correction attempt" in response.text
    assert 'href="#req-0-excerpt"' in response.text


@pytest.mark.parametrize("criterion, field", [
    ({"type": "graduation_window", "from": 2026}, "from"),
    ({"type": "skill", "skills": [1], "match": "all"}, "skills"),
    ({"type": "degree", "level": "bachelor", "fields": [1]}, "fields"),
    ({"type": "location", "locations": [1]}, "locations"),
    ({"type": "authorization", "country": "US", "sponsorship_available": {}}, "sponsorship"),
])
def test_rejected_criterion_types_render_a_correction_form(
    ai_app_client, fake_ai, sample_profile, criterion, field,
):
    client = ai_app_client
    job_id = setup(client, sample_profile, requirements=False)
    bad = copy.deepcopy(DEMO_REQUIREMENTS[:1])
    bad[0]["criterion"] = criterion
    fake_ai.push(*[tool_response("return_requirements", {"requirements": bad})] * 2)
    response = client.post(f"/jobs/{job_id}/requirements/extract")
    assert response.status_code == 422, response.text
    assert "still has problems after one correction attempt" in response.text
    assert f'name="req-0-{field}"' in response.text
    assert len(fake_ai.requests) == 2
    assert "No requirements yet" in client.get(f"/jobs/{job_id}").text


def test_malformed_completion_envelope_is_a_friendly_http_error(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile)
    malformed = {"choices": [{"message": None}]}
    fake_ai.push(malformed, malformed)
    response = client.post(f"/jobs/{job_id}/requirements/extract")
    assert response.status_code == 502
    assert "response message must be an object" in response.text
    assert '<span class="badge check-met">Met 4</span>' in response.text
    assert len(fake_ai.requests) == 2


def test_ai_errors_are_shown_and_work_is_kept(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile)
    fake_ai.push((429, {"error": {"code": 429, "message": "free-models-per-day"}}))
    response = client.post(f"/jobs/{job_id}/requirements/extract")
    assert response.status_code == 502
    assert "rate limit or free quota was reached" in response.text
    assert '<span class="badge check-met">Met 4</span>' in response.text


def test_suggest_evidence_asks_for_sharing_approval_first(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile)
    page = client.get(f"/jobs/{job_id}").text
    assert "Review what's shared, then suggest evidence" in page
    redirect = client.post(f"/jobs/{job_id}/evidence/suggest", follow_redirects=False)
    assert redirect.headers["location"] == f"/profile/sharing?next=/jobs/{job_id}"
    assert fake_ai.requests == []

    preview = client.get(redirect.headers["location"]).text
    assert "Not approved yet" in preview and "Never sent" in preview
    assert "jordan@example.com" not in preview and "Sample Analytics" in preview
    base_hash = re.search(r'name="base_hash" value="([^"]+)"', preview).group(1)
    included = re.findall(r'name="include" value="([^"]+)" checked', preview)
    form = {"base_hash": base_hash, "next": f"/jobs/{job_id}",
            "include": [i for i in included if i != "proj-1-b2"],
            "text-exp-1-b1": "Built a REST API for reporting"}
    approved = client.post("/profile/sharing", data=form, follow_redirects=False)
    assert approved.headers["location"] == f"/jobs/{job_id}?msg=sharing_approved#requirements"

    sharing = client.get("/profile/sharing").text
    assert "Approved" in sharing and "Exactly what is sent now" in sharing
    assert "Built a REST API for reporting" in sharing

    fake_ai.push(tool_response("return_evidence_suggestions", {"suggestions": [
        {"requirement_id": "r4", "source_ids": ["exp-1-b1"], "reason": "Describes building a REST API"}]}))
    done = client.post(f"/jobs/{job_id}/evidence/suggest", follow_redirects=False)
    assert done.headers["location"].endswith("msg=suggestions_ready#requirements")
    sent = fake_ai.sent_text()
    assert "Built a REST API for reporting" in sent and "Used by 3 student clubs" not in sent
    assert "jordan@example.com" not in sent

    apis = card(client.get(f"/jobs/{job_id}").text, "r4")
    assert "AI suggestions" in apis and "Model's reason: Describes building a REST API" in apis
    assert "check-unknown" in apis  # a suggestion is not evidence
    client.post(f"/jobs/{job_id}/requirements/r4/suggestions/accept", data={"source": "exp-1-b1"})
    apis = card(client.get(f"/jobs/{job_id}").text, "r4")
    assert "check-met" in apis and "AI suggestions" not in apis


def test_sharing_rejects_outside_redirects_and_stale_reviews(ai_app_client, sample_profile):
    client = ai_app_client
    review_and_save(client, profile_form(sample_profile))
    preview = client.get("/profile/sharing?next=https://evil.example").text
    assert 'name="next" value=""' in preview
    response = client.post("/profile/sharing", data={"base_hash": "stale", "next": "https://evil.example"})
    assert response.status_code == 409 and "Your profile changed while you were reviewing" in response.text
    ok = client.post("/profile/sharing", data={"base_hash": re.search(r'name="base_hash" value="([^"]+)"', preview).group(1),
                                               "next": "https://evil.example"}, follow_redirects=False)
    assert ok.headers["location"] == "/profile/sharing?msg=sharing_approved"
    withdrawn = client.post("/profile/sharing/withdraw", follow_redirects=False)
    assert withdrawn.headers["location"] == "/profile/sharing?msg=sharing_withdrawn"
    assert "Not approved yet" in client.get("/profile/sharing").text


def test_ai_buttons_block_duplicates_in_the_page(ai_app_client, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile, requirements=False)
    page = client.get(f"/jobs/{job_id}").text
    assert 'class="inline-form" data-ai' in page
    assert 'data-busy-label="Working… this can take up to 90 seconds"' in page


# ---------------------------------------------------------------- upgrade from Milestone 1

def test_milestone_1_database_is_upgraded_in_place(settings):
    settings.resolved_data_dir.mkdir(parents=True)
    with sqlite3.connect(settings.database_path) as db:  # the jobs table as Milestone 1 created it
        db.execute("""CREATE TABLE jobs (id INTEGER PRIMARY KEY, company VARCHAR(200) NOT NULL, title VARCHAR(200) NOT NULL,
            location VARCHAR(200) NOT NULL, url VARCHAR(2000) NOT NULL, description TEXT NOT NULL, revision INTEGER NOT NULL,
            requirements JSON NOT NULL, evidence JSON NOT NULL, overrides JSON NOT NULL, questions JSON NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)""")
        db.execute("""INSERT INTO jobs VALUES (1, 'Example Corp', 'Intern', '', '', 'Need Python.', 1, '[]', '{}', '{}', '[]',
            '2026-10-01 10:00:00', '2026-10-01 10:00:00')""")
    engine = make_engine(settings.database_url)
    init_db(engine)
    assert "requirement_counter" in {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert {"ai_runs", "outbound_approvals"} <= set(inspect(engine).get_table_names())
    with make_session_factory(engine)() as session:
        job = session.get(Job, 1)
        assert (job.title, job.requirement_counter, job.requirements, job.evidence) == ("Intern", 0, [], {})
    init_db(engine)  # running the upgrade again is harmless
    engine.dispose()


# ---------------------------------------------------------------- found in the browser pass

def test_empty_suggestions_say_so(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile)
    preview = client.get("/profile/sharing").text
    client.post("/profile/sharing", data={"base_hash": re.search(r'name="base_hash" value="([^"]+)"', preview).group(1),
                                          "include": re.findall(r'name="include" value="([^"]+)" checked', preview)})
    fake_ai.push(tool_response("return_evidence_suggestions", {"suggestions": [
        {"requirement_id": "r4", "source_ids": [], "reason": "Nothing fits"}]}))
    response = client.post(f"/jobs/{job_id}/evidence/suggest", follow_redirects=False)
    assert response.headers["location"].endswith("msg=suggestions_none#requirements")
    page = client.get(response.headers["location"]).text
    assert "The model found nothing in what you shared" in page
    assert "nothing waiting" in page


def test_saved_requirements_demote_the_proposal_link(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile, requirements=False)
    fake_ai.push(tool_response("return_requirements", {"requirements": DEMO_REQUIREMENTS}))
    client.post(f"/jobs/{job_id}/requirements/extract")
    assert "Review the AI proposal" in client.get(f"/jobs/{job_id}").text
    client.post(f"/jobs/{job_id}/requirements", data=requirement_form(DEMO_REQUIREMENTS))
    page = client.get(f"/jobs/{job_id}").text
    assert "Review the AI proposal" not in page and "Start again from the AI proposal" in page


def test_errors_far_down_the_page_take_focus(client, sample_profile):
    job_id = setup(client, sample_profile)
    response = client.post(f"/jobs/{job_id}/requirements/r5/override", data={"status": "met", "reason": ""})
    assert '<p class="field-error" role="alert" tabindex="-1" data-focus>An override needs a reason.</p>' in response.text
    assert response.text.count("data-focus") == 1
