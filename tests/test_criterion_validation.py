"""Malformed AI criteria must be rejected as editable errors, without saving partial data."""

import pytest

from app.services import jobs
from app.services.requirements import RequirementsInvalid, set_requirements


@pytest.mark.parametrize("criterion, field", [
    ({"type": "graduation_window", "from": 2026}, "from"),
    ({"type": "graduation_window", "to": ["2026"]}, "to"),
    ({"type": "availability", "start_by": 2026}, "start_by"),
    ({"type": "availability", "start_from": {"date": "2026"}}, "start_from"),
    ({"type": "skill", "skills": ["Python", 1]}, "skills"),
    ({"type": "degree", "level": "bachelor", "fields": ["Computer Science", False]}, "fields"),
    ({"type": "location", "locations": ["Austin", {}]}, "locations"),
    ({"type": "authorization", "country": "US", "sponsorship_available": 1}, "sponsorship"),
    ({"type": "authorization", "country": "Atlantis"}, "country"),
    ({"type": "authorization", "country": ["US"]}, "country"),
])
def test_malformed_criterion_fields_report_locations_and_leave_saved_requirements(session, criterion, field):
    job = jobs.create(session, jobs.clean_input(title="Intern", company="Example", description="Need Python."))
    set_requirements(session, job, [{"text": "Python", "excerpt": "Need Python."}])
    before = list(job.requirements), job.revision
    with pytest.raises(RequirementsInvalid) as exc:
        set_requirements(session, job, [{
            "_form": "req-0", "text": "Python", "excerpt": "Need Python.", "criterion": criterion,
        }])
    assert f"req-0-{field}" in {e.field for e in exc.value.errors}
    session.refresh(job)
    assert (job.requirements, job.revision) == before


@pytest.mark.parametrize("country, expected", [
    ("United States", "US"), ("USA", "US"), ("u.s.", "US"),
    ("United Kingdom", "GB"), ("uk", "GB"), ("India", "IN"),
    ("Côte d'Ivoire", "CI"), ("Türkiye", "TR"),
])
def test_authorization_criterion_uses_canonical_country_code(session, country, expected):
    job = jobs.create(session, jobs.clean_input(title="Intern", company="Example", description="Work authorization required."))
    set_requirements(session, job, [{
        "text": "Work authorization", "excerpt": "Work authorization required.",
        "criterion": {"type": "authorization", "country": country},
    }])
    assert job.requirements[0].criterion.country == expected
