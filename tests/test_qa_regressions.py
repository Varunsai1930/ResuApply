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
