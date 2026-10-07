"""Profile validation, stable IDs, revisions and diff (scenarios shared with ResuSkill's test_profile.py)."""

from __future__ import annotations

import copy

import pytest

from app.models import Candidate
from app.services import jobs as job_service
from app.services import profile as profile_service
from app.services.profile import ProfileInvalid, StaleReview, normalize


def save(session, data, **kwargs):
    return profile_service.save(session, copy.deepcopy(data), **kwargs)


def as_input(candidate: Candidate) -> dict:
    """The saved profile as the next edit would submit it (IDs included)."""
    return candidate.profile.model_dump()


# ---------------------------------------------------------------- validation

def test_valid_profile_is_normalized(sample_profile):
    sample_profile["skills"].append({"name": "postgres"})  # alias of PostgreSQL: duplicate
    sample_profile["experience"][0]["technologies"] = ["python", "Python", "k8s"]
    result = normalize(sample_profile)
    prof = result.profile
    assert prof.contact.name == "Jordan Example"
    assert [s.name for s in prof.skills] == ["Python", "SQL", "Flask", "PostgreSQL", "Git"]
    assert any("Duplicate skill" in w for w in result.warnings)
    assert prof.experience[0].technologies == ["Python", "Kubernetes"]


def test_invalid_profile_reports_every_problem_and_saves_nothing(session, sample_profile):
    sample_profile["contact"]["name"] = ""
    sample_profile["contact"]["email"] = "not-an-email"
    sample_profile["experience"][0]["start"] = "June 2024"
    sample_profile["projects"][0]["name"] = ""
    sample_profile["preferences"]["work_mode"] = "sometimes"
    with pytest.raises(ProfileInvalid) as exc:
        save(session, sample_profile)
    messages = " | ".join(e.message for e in exc.value.errors)
    assert "Name is required" in messages
    assert "Email looks invalid" in messages
    assert "start date must be" in messages and "June 2024" in messages
    assert "Project 1: name is required" in messages
    assert "Work mode" in messages
    assert profile_service.get_candidate(session) is None


def test_errors_point_at_form_fields(sample_profile):
    sample_profile["experience"][0]["_form"] = "experience-7"
    sample_profile["experience"][0]["end"] = "soon"
    with pytest.raises(ProfileInvalid) as exc:
        normalize(sample_profile)
    assert [e.field for e in exc.value.errors] == ["experience-7-end"]


def test_present_is_accepted_case_insensitively(sample_profile):
    sample_profile["experience"][0]["end"] = "Present"
    assert normalize(sample_profile).profile.experience[0].end == "present"


def test_skill_in_both_lists_is_rejected(sample_profile):
    sample_profile["skills_absent"] = ["python"]
    with pytest.raises(ProfileInvalid) as exc:
        normalize(sample_profile)
    assert exc.value.errors[0].field == "skills_absent"


def test_authorization_unknown_stays_unknown(sample_profile):
    sample_profile["authorization"] = [{"country": "ca"}]
    prof = normalize(sample_profile).profile
    assert [a.model_dump() for a in prof.authorization] == [
        {"country": "CA", "authorized": None, "requires_sponsorship": None}
    ]


def test_authorization_rejects_non_tristate_and_duplicates(sample_profile):
    sample_profile["authorization"] = [
        {"country": "US", "authorized": "maybe"},
        {"country": "us", "authorized": True},
    ]
    with pytest.raises(ProfileInvalid) as exc:
        normalize(sample_profile)
    messages = [e.message for e in exc.value.errors]
    assert any("yes, no or unknown" in m for m in messages)
    assert any("listed twice" in m for m in messages)


def test_string_where_a_list_is_expected_is_rejected():
    with pytest.raises(ProfileInvalid):
        normalize({"contact": {"name": "A"}, "preferences": {"roles": "Backend Engineer"}})


def test_certification_needs_a_name_and_a_real_date(sample_profile):
    sample_profile["certifications"] = [{"issuer": "Example Board", "date": "2024-13"}]
    with pytest.raises(ProfileInvalid) as exc:
        normalize(sample_profile)
    messages = " ".join(e.message for e in exc.value.errors)
    assert "name is required" in messages and "date must be" in messages


# ---------------------------------------------------------------- stable IDs and revisions

def test_save_assigns_stable_ids_and_revision(session, sample_profile):
    result = save(session, sample_profile)
    candidate = result.candidate
    assert result.saved and candidate.revision == 1
    prof = candidate.profile
    assert prof.education[0].id == "edu-1"
    assert prof.experience[0].id == "exp-1"
    assert prof.projects[0].id == "proj-1"
    assert [b.id for b in prof.experience[0].bullets] == ["exp-1-b1", "exp-1-b2", "exp-1-b3"]
    assert [b.id for b in prof.projects[0].bullets] == ["proj-1-b1", "proj-1-b2"]
    assert result.changes


def test_edit_preserves_ids_and_never_reuses_deleted_ones(session, sample_profile):
    first = save(session, sample_profile).candidate
    edited = as_input(first)
    bullets = edited["experience"][0]["bullets"]
    edited["experience"][0]["bullets"] = [{"text": bullets[1]["text"]}, {"text": "Mentored a new intern"}]
    result = save(session, edited)
    ids = [b.id for b in result.candidate.profile.experience[0].bullets]
    assert ids == ["exp-1-b2", "exp-1-b4"]
    assert result.candidate.revision == 2
    assert any(c.line.startswith("- exp-1-b1") for c in result.changes)
    assert any(c.line.startswith("- exp-1-b3") for c in result.changes)

    # Deleting the newest bullet and adding another still never reuses b4.
    again = as_input(result.candidate)
    again["experience"][0]["bullets"] = [again["experience"][0]["bullets"][0], {"text": "Paired on code reviews"}]
    third = save(session, again).candidate
    assert [b.id for b in third.profile.experience[0].bullets] == ["exp-1-b2", "exp-1-b5"]


def test_editing_bullet_text_keeps_its_id(session, sample_profile):
    first = save(session, sample_profile).candidate
    edited = as_input(first)
    edited["experience"][0]["bullets"][0]["text"] = "Built a Flask REST API for internal reporting"
    result = save(session, edited)
    bullet = result.candidate.profile.experience[0].bullets[0]
    assert (bullet.id, bullet.text) == ("exp-1-b1", "Built a Flask REST API for internal reporting")
    assert [c.kind for c in result.changes] == ["changed"]
    assert result.changes[0].path == "exp-1-b1"


def test_editing_entry_fields_keeps_entry_and_bullet_ids(session, sample_profile):
    first = save(session, sample_profile).candidate
    edited = as_input(first)
    edited["experience"][0]["organization"] = "Sample Analytics Inc."
    edited["experience"][0]["title"] = "Backend Intern"
    edited["experience"][0]["start"] = "2024-05"
    result = save(session, edited)
    entry = result.candidate.profile.experience[0]
    assert entry.id == "exp-1"
    assert [b.id for b in entry.bullets] == ["exp-1-b1", "exp-1-b2", "exp-1-b3"]


def test_deleted_entry_id_is_never_reused(session, sample_profile):
    first = save(session, sample_profile).candidate
    edited = as_input(first)
    edited["experience"] = []
    save(session, edited)
    readded = as_input(profile_service.get_candidate(session))
    readded["experience"] = [{"id": "exp-1", "organization": "Another Example Ltd", "bullets": ["Shipped a feature"]}]
    result = save(session, readded)
    entry = result.candidate.profile.experience[0]
    assert entry.id == "exp-2"  # the stale "exp-1" from the form is not resurrected
    assert entry.bullets[0].id == "exp-2-b1"


def test_unknown_ids_are_treated_as_new(session, sample_profile):
    sample_profile["experience"][0]["id"] = "exp-99"
    sample_profile["experience"][0]["bullets"] = [{"id": "proj-1-b1", "text": "Wrote unit tests"}]
    result = save(session, sample_profile)
    entry = result.candidate.profile.experience[0]
    assert entry.id == "exp-1"
    assert entry.bullets[0].id == "exp-1-b1"


def test_bullet_ids_do_not_move_between_entries(session, sample_profile):
    first = save(session, sample_profile).candidate
    edited = as_input(first)
    moved = edited["experience"][0]["bullets"].pop()
    edited["projects"][0]["bullets"].append(moved)  # keeps "id": "exp-1-b3"
    result = save(session, edited)
    assert [b.id for b in result.candidate.profile.projects[0].bullets] == ["proj-1-b1", "proj-1-b2", "proj-1-b3"]


def test_entries_match_by_key_when_ids_are_missing(session, sample_profile):
    save(session, sample_profile)
    result = save(session, sample_profile)  # same content, no IDs supplied
    assert not result.saved
    assert result.candidate.profile.experience[0].id == "exp-1"


def test_reordering_bullets_is_a_change_but_keeps_ids(session, sample_profile):
    first = save(session, sample_profile).candidate
    edited = as_input(first)
    edited["experience"][0]["bullets"].reverse()
    result = save(session, edited)
    assert result.saved and result.candidate.revision == 2
    assert [b.id for b in result.candidate.profile.experience[0].bullets] == ["exp-1-b3", "exp-1-b2", "exp-1-b1"]
    assert any("order" in c.path for c in result.changes)


def test_unchanged_save_keeps_revision(session, sample_profile):
    first = save(session, sample_profile).candidate
    updated_at = first.updated_at
    result = save(session, as_input(first))
    assert not result.saved
    assert result.changes == []
    assert result.candidate.revision == 1
    assert result.candidate.updated_at == updated_at


def test_whitespace_only_edits_are_not_changes(session, sample_profile):
    first = save(session, sample_profile).candidate
    edited = as_input(first)
    edited["summary"] = "  " + edited["summary"] + "  "
    assert not save(session, edited).saved


def test_certification_ids_are_stable(session, sample_profile):
    sample_profile["certifications"] = [{"name": "Example Cloud Practitioner", "issuer": "Example", "date": "2024-03"}]
    first = save(session, sample_profile).candidate
    assert first.profile.certifications[0].id == "cert-1"
    edited = as_input(first)
    edited["certifications"][0]["date"] = "2024-04"
    edited["certifications"].append({"name": "Example Data Fundamentals"})
    result = save(session, edited)
    assert [c.id for c in result.candidate.profile.certifications] == ["cert-1", "cert-2"]
    edited = as_input(result.candidate)
    edited["certifications"] = [{"name": "Another Cert"}]
    assert [c.id for c in save(session, edited).candidate.profile.certifications] == ["cert-3"]


def test_review_does_not_save(session, sample_profile):
    review = profile_service.review(session, sample_profile)
    assert review.is_new and review.has_changes and review.base_revision == 0
    assert profile_service.get_candidate(session) is None


def test_save_refuses_a_stale_review(session, sample_profile):
    first = save(session, sample_profile).candidate
    reviewed = as_input(first)
    reviewed["summary"] = "Reviewed against revision 1."
    other = as_input(first)
    other["summary"] = "Saved from another tab."
    save(session, other, base_revision=1)
    with pytest.raises(StaleReview):
        save(session, reviewed, base_revision=1)
    assert profile_service.get_candidate(session).profile.summary == "Saved from another tab."


def test_first_profile_claims_jobs_saved_earlier(session, sample_profile):
    data = job_service.clean_input(title="Intern", company="Example Corp", description="Python.")
    job = job_service.create(session, data)
    assert job.application.candidate_id is None
    candidate = save(session, sample_profile).candidate
    session.refresh(job.application)
    assert job.application.candidate_id == candidate.id


# ---------------------------------------------------------------- diff

def test_diff_for_a_new_profile_lists_additions_only(sample_profile):
    prof = normalize(sample_profile).profile
    changes = profile_service.diff(None, prof)
    assert {c.kind for c in changes} == {"added"}
    paths = [c.path for c in changes]
    assert "contact.name" in paths and "experience exp-1" in paths and "exp-1-b1" in paths
    assert "contact.links.portfolio" not in paths  # empty fields are not listed
    entry = next(c for c in changes if c.path == "experience exp-1")
    assert entry.after == (
        "Sample Analytics · Software Engineering Intern · Remote · 2024-06 – 2024-08"
        " · Python, Flask, PostgreSQL (3 bullets)"
    )


def test_diff_reports_field_level_changes(session, sample_profile):
    first = save(session, sample_profile).candidate
    edited = as_input(first)
    edited["contact"]["email"] = "jordan.example@example.org"
    edited["skills"].append({"name": "Docker", "category": "Tools"})
    edited["authorization"][0]["requires_sponsorship"] = None
    edited["projects"] = []
    lines = [c.line for c in profile_service.diff(first.profile, normalize(edited, first.profile, first.id_counters).profile)]
    assert "~ contact.email: jordan@example.com -> jordan.example@example.org" in lines
    assert "+ skill: Docker (Tools)" in lines
    assert "~ authorization.US.requires_sponsorship: no -> unknown" in lines
    assert "- projects proj-1: TaskBot" in lines
