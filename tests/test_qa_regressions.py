"""Edge cases found by black-box testing: form limits, odd input and error handling."""

from __future__ import annotations

import copy
import re

from tests.test_assessment_routes import editor_fields
from tests.test_routes import create_job, profile_form, review_and_save


def _editor_rows(client, job_id: int, count: int) -> dict[str, str]:
    """``count`` requirement rows named and shaped exactly as the real editor submits them."""
    fields = editor_fields(client.get(f"/jobs/{job_id}/requirements/edit").text)
    key = next(k.split("-")[1] for k in fields if k.startswith("req-"))
    blank = {k.removeprefix(f"req-{key}-"): v for k, v in fields.items() if k.startswith(f"req-{key}-")}
    data = {"base_revision": fields["base_revision"]}
    for i in range(count):
        data |= {f"req-n{i}-{name}": value for name, value in blank.items()}
        data |= {f"req-n{i}-text": f"Python {i}", f"req-n{i}-excerpt": "We need Python and SQL", f"req-n{i}-id": ""}
    return data


def test_requirements_editor_saves_more_rows_than_the_default_form_limit(client, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client)
    data = _editor_rows(client, job_id, 60)
    assert len(data) > 1000  # Starlette's default limit
    response = client.post(f"/jobs/{job_id}/requirements", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text[:500]
    assert len(re.findall(r'name="req-[^"]+-text"', client.get(f"/jobs/{job_id}/requirements/edit").text)) >= 60


def test_too_many_requirements_get_a_message_and_keep_the_input(client, sample_profile):
    from app.services.requirements import MAX_REQUIREMENTS

    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client)
    response = client.post(f"/jobs/{job_id}/requirements", data=_editor_rows(client, job_id, MAX_REQUIREMENTS + 1),
                           follow_redirects=False)
    assert response.status_code == 422
    assert f"Keep it to {MAX_REQUIREMENTS:,} or fewer" in response.text
    assert f"Python {MAX_REQUIREMENTS}" in response.text  # the rows are still there to edit


def test_large_profiles_can_be_reviewed_and_saved(client, sample_profile):
    many = copy.deepcopy(sample_profile)
    many["experience"] = [dict(sample_profile["experience"][0], organization=f"Org {i}",
                               bullets=[f"Did task {i}.{j}" for j in range(14)]) for i in range(30)]
    review, saved = review_and_save(client, profile_form(many))
    assert saved.status_code == 303, saved.text[:300]

    long = copy.deepcopy(sample_profile)
    long["experience"][0]["bullets"] = [("Built services " * 9000)[:120_000] for _ in range(10)]
    review, saved = review_and_save(client, profile_form(long))
    assert saved.status_code == 303, saved.text[:300]  # the reviewed payload is over 1 MB


def test_no_page_may_be_framed_by_another_site(client):
    for response in (client.get("/jobs"), client.get("/static/style.css"), client.get("/missing"),
                     client.get("/jobs", headers={"host": "evil.example"}),
                     client.post("/jobs", data={}, headers={"origin": "https://evil.example"})):
        assert response.headers["x-frame-options"] == "DENY", response.request.url
        assert response.headers["content-security-policy"] == "frame-ancestors 'none'"


def test_ids_too_large_for_the_database_are_not_found(client, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client)
    huge = "99999999999999999999"
    for response in (client.get(f"/jobs/{huge}"), client.get("/jobs/9223372036854775808/edit"),
                     client.post("/jobs/9223372036854775808/status", data={"status": "applied"}),
                     client.post(f"/answers/{huge}/delete")):
        assert response.status_code == 404, (response.request.url, response.status_code)
    question = client.post(f"/jobs/{job_id}/questions", data={"text": "Why this team?", "required": "1"},
                           follow_redirects=False).headers["location"].split("#question-")[1]
    response = client.post(f"/jobs/{job_id}/questions/{question}/answer",
                           data={"text": "x", "origin": "bank", "bank_id": huge})
    assert response.status_code == 422 and "no longer exists" in response.text


def test_damaged_profile_review_data_is_refused_not_crashed(client, sample_profile):
    import json

    from tests.test_routes import hidden

    review, _ = review_and_save(client, profile_form(sample_profile))
    damaged = ["[" * 50000 + "]" * 50000, json.dumps({"contact": "x"}), json.dumps(sample_profile),
               json.dumps({"contact": {"links": []}, "preferences": {}, "availability": {}})]
    for payload in damaged:
        for path, data in (("/profile/save", {"payload": payload, "base_revision": "1"}), ("/profile/edit", {"payload": payload})):
            response = client.post(path, data=data, follow_redirects=False)
            assert response.status_code == 400, (path, payload[:40], response.status_code)
            assert "damaged" in response.text

    # A genuine review payload with a wrong revision still gets the conflict page.
    fresh = client.post("/profile/review", data=profile_form(sample_profile | {"summary": "Changed"}))
    for revision in ("-1", "99999999999999999999"):
        response = client.post("/profile/save", data={"payload": hidden(fresh.text, "payload"), "base_revision": revision},
                               follow_redirects=False)
        assert response.status_code == 409, (revision, response.status_code)


def test_years_of_experience_must_be_a_finite_sensible_number(client, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client)
    for years, ok in (("nan", False), ("inf", False), ("1e400", False), ("1e308", False), ("101", False),
                      ("-1", False), ("2.5", True), ("0", True), ("100", True)):
        data = _editor_rows(client, job_id, 1)
        data |= {"req-n0-ctype": "years_experience", "req-n0-years": years, "req-n0-area": "backend"}
        response = client.post(f"/jobs/{job_id}/requirements", data=data, follow_redirects=False)
        assert (response.status_code == 303) is ok, (years, response.status_code)
        if not ok:
            assert "years must be a number from 0 to 100" in response.text
    with client.app.state.session_factory() as session:
        from app.models import Job
        assert "NaN" not in str(session.get(Job, job_id).requirements)


def test_date_ranges_that_end_before_they_start_are_rejected(client, sample_profile):
    reversed_role = copy.deepcopy(sample_profile)
    reversed_role["experience"][0] |= {"start": "2024-08", "end": "2024-06"}
    review = client.post("/profile/review", data=profile_form(reversed_role))
    assert review.status_code == 422 and "end date is before the start date" in review.text
    for start, end in (("2024-06", "2024-06"), ("2024-06", "2024"), ("2030-01", "present")):
        fine = copy.deepcopy(sample_profile)
        fine["experience"][0] |= {"start": start, "end": end}
        assert client.post("/profile/review", data=profile_form(fine)).status_code == 200, (start, end)

    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client)
    cases = [("graduation_window", {"from": "2027-05", "to": "2026-05"}, "the graduation window ends before it starts"),
             ("availability", {"start_from": "2026-09", "start_by": "2026-06"}, "the start-by date is before the start-from date")]
    for ctype, values, message in cases:
        data = _editor_rows(client, job_id, 1) | {"req-n0-ctype": ctype} | {f"req-n0-{k}": v for k, v in values.items()}
        response = client.post(f"/jobs/{job_id}/requirements", data=data, follow_redirects=False)
        assert response.status_code == 422 and message in response.text, ctype
    data = _editor_rows(client, job_id, 1) | {"req-n0-ctype": "graduation_window", "req-n0-from": "2026-05", "req-n0-to": "2026"}
    assert client.post(f"/jobs/{job_id}/requirements", data=data, follow_redirects=False).status_code == 303


def test_control_characters_are_removed_from_every_form_value(client, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client, title="Back\x00end\x07 Intern\x1b", description="We need Python\x0band SQL.\x0cThanks\x00")
    client.post(f"/jobs/{job_id}/notes", data={"text": "Called\x00 back\x7f\ttoday"})
    with client.app.state.session_factory() as session:
        from app.models import Job
        job = session.get(Job, job_id)
        assert job.title == "Backend Intern"
        assert job.description == "We need Python\nand SQL.\nThanks"
        assert job.application.notes[-1].text == "Called back\ttoday"
    # Unicode text, plus signs and ampersands survive the rewrite untouched.
    job_id = create_job(client, title="C++ & Go — Ünïcødé 😀 Intern", description="Line one\r\nLine two & more")
    with client.app.state.session_factory() as session:
        from app.models import Job
        job = session.get(Job, job_id)
        assert job.title == "C++ & Go — Ünïcødé 😀 Intern" and job.description == "Line one\nLine two & more"


def test_status_dates_must_be_plain_and_plausible(client):
    job_id = create_job(client)
    for on, message in (("2026-W41", "Enter the date as YYYY-MM-DD."), ("20261010", "Enter the date as YYYY-MM-DD."),
                        ("0001-01-01", "Enter a date from 1990 onwards."), ("1900-01-01", "Enter a date from 1990 onwards.")):
        response = client.post(f"/jobs/{job_id}/status", data={"status": "applied", "on": on})
        assert response.status_code == 422 and message in response.text, on
    assert client.post(f"/jobs/{job_id}/status", data={"status": "applied", "on": "1990-01-01"},
                       follow_redirects=False).status_code == 303


def test_question_limits_stay_within_the_longest_answer_kept(client, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client)
    for limit, ok in (("10000", True), ("10001", False), ("99999999999999999999", False)):
        response = client.post(f"/jobs/{job_id}/questions", data={"text": "Why us?", "required": "1", "limit": limit},
                               follow_redirects=False)
        assert (response.status_code == 303) is ok, limit
        if not ok:
            assert "at most 10,000" in response.text


def test_profile_links_must_be_web_addresses(client, sample_profile):
    for link, ok in (("javascript:alert(1)", False), ("JaVaScRiPt:alert(1)", False), ("data:text/html,x", False),
                     ("mailto:me@example.com", False), ("github.com/you", True), ("https://github.com/you", True),
                     ("example.com:8080/me", True)):
        profile = copy.deepcopy(sample_profile)
        profile["contact"]["links"] = {"github": link}
        response = client.post("/profile/review", data=profile_form(profile))
        assert (response.status_code == 200) is ok, link
        if not ok:
            assert "GitHub link must be a web address" in response.text
    project = copy.deepcopy(sample_profile)
    project["projects"][0]["link"] = "javascript:alert(1)"
    response = client.post("/profile/review", data=profile_form(project))
    assert response.status_code == 422 and "link must be a web address" in response.text


def test_sharing_only_returns_to_a_plain_job_url():
    from app.routes.sharing import _safe_next

    assert _safe_next("/jobs/12") == "/jobs/12"
    for value in ("/jobs/12\n", "/jobs/12\r\nSet-Cookie: x=1", "/jobs/١", "//evil.example", "/jobs/12/../x", "javascript:alert(1)", ""):
        assert _safe_next(value) == "", repr(value)


def test_errors_use_the_app_error_page(client, monkeypatch):
    from fastapi.testclient import TestClient

    from app.services import jobs as job_service
    from tests.conftest import BASE_URL

    job_id = create_job(client)
    for path in ("/jobs/abc", "/jobs/1.5", f"/jobs/{job_id}/snapshots/x"):
        response = client.get(path)
        assert response.status_code == 404 and response.headers["content-type"].startswith("text/html"), path
        assert "There is no page at this address." in response.text and "Back to jobs" in response.text

    damaged = client.post(f"/jobs/{job_id}/requirements/extract", data={"force": "abc"})
    assert damaged.status_code == 422 and "incomplete or damaged" in damaged.text and "Back to jobs" in damaged.text

    too_large = client.post("/jobs", data={"title": "T", "company": "C", "description": "x" * 17_000_000})
    assert too_large.status_code == 400 and "too large to read" in too_large.text

    def broken(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(job_service, "list_all", broken)
    quiet = TestClient(client.app, base_url=BASE_URL, raise_server_exceptions=False)
    response = quiet.get("/jobs")
    assert response.status_code == 500 and response.headers["content-type"].startswith("text/html")
    assert "Something unexpected went wrong" in response.text and "simulated failure" not in response.text


def test_startup_problems_end_with_a_plain_message(tmp_path, monkeypatch):
    import socket

    import pytest

    from app import __main__ as start
    from app.config import HOST, Settings, get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("RESUAPPLY_PORT", "abc")  # the environment wins over any .env file
    with pytest.raises(SystemExit) as bad_setting:
        start.load_settings()
    assert "RESUAPPLY_PORT" in str(bad_setting.value) and "Traceback" not in str(bad_setting.value)
    monkeypatch.delenv("RESUAPPLY_PORT")
    get_settings.cache_clear()

    a_file = tmp_path / "a-file"
    a_file.write_text("")
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "resuapply.db").write_text("this is not sqlite")
    for folder in (a_file, corrupt):
        with pytest.raises(SystemExit) as storage:
            start.check_storage(Settings(_env_file=None, data_dir=folder))
        assert "can't use its data folder" in str(storage.value)
    start.check_storage(Settings(_env_file=None, data_dir=tmp_path / "fresh"))  # a usable folder passes

    with socket.socket() as busy:
        busy.bind((HOST, 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(SystemExit) as in_use:
            start.check_port(port)
    assert f"Port {port} is already in use" in str(in_use.value)


def test_answer_length_is_counted_as_employer_forms_count_it():
    from app.services.answers import check_length, measure

    assert measure("😀", "chars") == 2 and measure("👨‍👩‍👧‍👦", "chars") == 11 and measure("🇺🇸", "chars") == 4
    assert measure("é", "chars") == 2 and measure("abc", "chars") == 3
    assert measure("line one\r\nline two", "chars") == len("line one\nline two")  # a break is one character
    assert measure("one—two–three four", "words") == 4 and measure("well-known  fact\n", "words") == 2
    assert check_length("😀" * 5, 10, "chars") is None and check_length("😀" * 6, 10, "chars") is not None


def test_head_requests_are_answered_like_get_without_a_body(client):
    job_id = create_job(client)
    for path in ("/jobs", f"/jobs/{job_id}", "/profile", "/static/style.css", "/"):
        head, get = client.head(path, follow_redirects=False), client.get(path, follow_redirects=False)
        assert head.status_code == get.status_code, path
        assert head.content == b"" and head.headers.get("content-type") == get.headers.get("content-type"), path
    assert client.head("/missing").status_code == 404
    assert client.post("/jobs", data={}, follow_redirects=False).status_code == 422  # other methods unchanged


def test_bank_answers_never_stay_on_a_question_that_becomes_sensitive(client, sample_profile):
    from app.db import utcnow
    from app.schemas.package import Answer, Question
    from app.services import answers, jobs, package
    from app.services.profile import get_candidate

    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client)
    with client.app.state.session_factory() as session:
        job = jobs.get(session, job_id)
        job.questions = [
            Question(id="q1", text="Email address and date of birth", category="factual",  # upgrade -> sensitive
                     detected_category="factual", factual_key="email", added_at=utcnow()),
            Question(id="q2", text="Describe your email marketing experience", category="unknown",
                     detected_category="open", added_at=utcnow()),
            Question(id="q3", text="Describe your email marketing work", category="unknown",
                     detected_category="open", added_at=utcnow()),
        ]
        job.application.answers = [Answer(question_id=q, text=f"Bank text {q}", origin="bank", at=utcnow())
                                   for q in ("q1", "q2", "q3")]
        session.commit()
        answers.upgrade_stored_questions(session)
        assert [a.question_id for a in job.application.answers] == ["q2", "q3"]
        assert any("date of birth" in b for b in package.check(job, job.application, get_candidate(session))[0])

    assert client.post(f"/jobs/{job_id}/questions/q2/category", data={"category": "sensitive"},
                       follow_redirects=False).status_code == 303
    assert client.post(f"/jobs/{job_id}/questions/q3/category", data={"category": "open"},
                       follow_redirects=False).status_code == 303
    with client.app.state.session_factory() as session:
        kept = {a.question_id: a.origin for a in jobs.get(session, job_id).application.answers}
    assert kept == {"q3": "bank"}  # kept for an open question, dropped for the sensitive one


def test_port_probe_uses_exclusive_binding_where_windows_offers_it(monkeypatch):
    import socket

    from app import __main__ as start

    options = []

    class Probe:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def setsockopt(self, level, option, value):
            options.append(option)

        def bind(self, address):
            pass

    monkeypatch.setattr(socket, "socket", Probe)
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)  # as on Windows
    start.check_port(8000)
    assert options == [-5]

    options.clear()
    monkeypatch.delattr(socket, "SO_EXCLUSIVEADDRUSE")  # as on macOS and Linux
    start.check_port(8000)
    assert options == [socket.SO_REUSEADDR]


def test_a_bad_value_in_a_link_is_not_called_a_damaged_form(client):
    job_id = create_job(client)
    response = client.get(f"/jobs/{job_id}/requirements/edit?proposal=abc")
    from html import unescape

    assert response.status_code == 400 and "This link has a value the page can't use." in unescape(response.text)
    assert "nothing was saved" not in response.text
    damaged = client.post(f"/jobs/{job_id}/requirements/extract", data={"force": "abc"})
    assert damaged.status_code == 422 and "incomplete or damaged" in damaged.text  # body errors keep their wording


def test_saving_unchanged_forms_over_legacy_control_characters_changes_nothing(client, sample_profile):
    from app.services import jobs
    from app.services.profile import get_candidate
    from tests.test_routes import hidden

    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client)
    # Text saved before forms were cleaned, as if by an older version.
    with client.app.state.session_factory() as session:
        job = jobs.get(session, job_id)
        job.description = "About the role\n\nWe need Python\x0band SQL."
        candidate = get_candidate(session)
        candidate.profile = candidate.profile.model_copy(update={"summary": "Builds backend\x07 services."})
        session.commit()
        job_revision, profile_revision = job.revision, candidate.revision

    # Job editor: the form re-sends the description, which the middleware cleans.
    page = client.get(f"/jobs/{job_id}/edit").text
    fields = {name: hidden(page, name) for name in ("base_revision", "base_token")}
    fields |= {"title": hidden(page, "title") or "Backend Engineering Intern", "company": "Example Corp",
               "location": "Austin, TX", "url": "https://jobs.example.com/backend-intern",
               "description": "About the role\n\nWe need Python\x0band SQL."}
    for name in ("title", "company", "location", "url"):
        found = re.search(rf'id="{name}" name="{name}" value="([^"]*)"', page)
        if found:
            fields[name] = found.group(1)
    response = client.post(f"/jobs/{job_id}/edit", data=fields, follow_redirects=False)
    assert response.headers["location"].endswith("msg=job_unchanged"), response.headers.get("location")

    # Profile editor: reviewing the stored profile lists no changes, and saving keeps the revision.
    from tests.test_routes import client_profile
    review = client.post("/profile/review", data=profile_form(client_profile(client)))
    assert review.status_code == 200 and "Nothing changed" in review.text

    with client.app.state.session_factory() as session:
        assert jobs.get(session, job_id).revision == job_revision
        assert get_candidate(session).revision == profile_revision


def test_requirements_resubmitted_over_legacy_control_characters_keep_their_evidence(client, sample_profile):
    from app.schemas.requirements import EvidenceLink, Requirement
    from app.services import jobs

    review_and_save(client, profile_form(sample_profile))
    job_id = create_job(client)
    with client.app.state.session_factory() as session:
        job = jobs.get(session, job_id)
        job.requirements = [Requirement(id="r1", text="Python\x07 and SQL", excerpt="We need Python and SQL")]
        job.requirement_counter = 1
        job.evidence = {"r1": EvidenceLink(sources=["exp-1-b1"], source_hashes={"exp-1-b1": "x"})}
        session.commit()
        revision = job.revision
    fields = editor_fields(client.get(f"/jobs/{job_id}/requirements/edit").text)
    response = client.post(f"/jobs/{job_id}/requirements", data=fields, follow_redirects=False)
    assert response.headers["location"].endswith("msg=requirements_unchanged#requirements")
    with client.app.state.session_factory() as session:
        job = jobs.get(session, job_id)
        assert job.revision == revision and "r1" in job.evidence
