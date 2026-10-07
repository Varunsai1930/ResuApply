"""Requirements validation and IDs, and the deterministic checklist.

Scenarios shared with ResuSkill's test_checklist.py and test_review_fixes.py.
"""

from __future__ import annotations

import copy
import re

import pytest

from app.services import checklist
from app.services import jobs as job_service
from app.services import profile as profile_service
from app.services.requirements import RequirementsInvalid, excerpt_found, set_requirements
from app.schemas.requirements import EvidenceLink
from app.services.text import date_key, is_valid_date
from app.templating import templates
from tests.conftest import DEMO_JOB, DEMO_REQUIREMENTS, SAMPLE_PROFILE


def seed(session, profile: dict | None = SAMPLE_PROFILE, requirements: list | None = DEMO_REQUIREMENTS):
    if profile is not None:
        profile_service.save(session, copy.deepcopy(profile))
    job = job_service.create(session, job_service.clean_input(**DEMO_JOB))
    if requirements is not None:
        set_requirements(session, job, copy.deepcopy(requirements))
    return job


def evaluate(session, job) -> dict[str, checklist.CheckResult]:
    candidate = profile_service.get_candidate(session)
    return {r.id: r for r in checklist.evaluate(job, candidate.profile if candidate else None)}


def statuses(session, job) -> dict[str, str]:
    return {k: v.status for k, v in evaluate(session, job).items()}


# ---------------------------------------------------------------- requirements

def test_requirements_get_sequential_ids_and_bump_the_job_revision(session):
    job = seed(session)
    assert [r.id for r in job.requirements] == [f"r{i}" for i in range(1, 8)]
    assert job.revision == 2 and job.requirement_counter == 7


def test_unsupported_excerpt_rejects_the_whole_set(session):
    job = seed(session, requirements=None)
    reqs = copy.deepcopy(DEMO_REQUIREMENTS)
    reqs[0]["excerpt"] = "Ten years of Java required."
    with pytest.raises(RequirementsInvalid) as exc:
        set_requirements(session, job, reqs)
    assert "not found in the job description" in exc.value.errors[0].message
    session.refresh(job)
    assert job.requirements == [] and job.revision == 1


def test_excerpt_matching_ignores_quote_style_and_spacing(session):
    job = seed(session, requirements=None)
    set_requirements(session, job, [{"text": "Bachelor's", "excerpt": "Pursuing a  Bachelor’s degree"}])
    assert len(job.requirements) == 1


@pytest.mark.parametrize("criterion", [
    {"type": "degree", "level": "wizard"},
    {"type": "skill", "skills": "Python"},
    {"type": "degree", "level": "bachelor", "fields": "Computer Science"},
    {"type": "location", "locations": "Austin"},
    {"type": "authorization", "country": ""},
    {"type": "years_experience", "years": -1},
    {"type": "graduation_window"},
    {"type": "telepathy"},
])
def test_invalid_criteria_are_rejected(session, criterion):
    job = seed(session, requirements=None)
    with pytest.raises(RequirementsInvalid):
        set_requirements(session, job, [{"text": "x", "excerpt": "Experience with Rust.", "criterion": criterion}])


def test_resave_keeps_ids_and_drops_links_for_changed_requirements(session):
    profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE))
    job = job_service.create(session, job_service.clean_input(title="Eng", company="Co", description="Need Python. Need Docker. Need Kubernetes."))
    set_requirements(session, job, [{"text": "Python", "excerpt": "Need Python"}, {"text": "Docker", "excerpt": "Need Docker"}])
    checklist.set_override(session, job, "r2", "met", "has Docker")
    set_requirements(session, job, [{"text": "Docker", "excerpt": "Need Docker"}, {"text": "Kubernetes", "excerpt": "Need Kubernetes"}])
    assert {r.text: r.id for r in job.requirements} == {"Docker": "r2", "Kubernetes": "r3"}
    assert "r2" in job.overrides
    set_requirements(session, job, [{"id": "r2", "text": "Docker 3+ years", "excerpt": "Need Docker"}])
    assert job.overrides == {}
    set_requirements(session, job, [{"text": "Python", "excerpt": "Need Python"}])
    assert job.requirements[0].id == "r4"  # r1 was deleted and is never reused


def test_unknown_supplied_ids_are_treated_as_new(session):
    job = seed(session, requirements=None)
    set_requirements(session, job, [{"id": "r99", "text": "Rust", "excerpt": "Experience with Rust."}])
    assert job.requirements[0].id == "r1"


def test_unchanged_save_keeps_the_revision(session):
    job = seed(session)
    saved = [r.model_dump(by_alias=True) | {"criterion": r.criterion_dict()} for r in job.requirements]
    assert set_requirements(session, job, saved) is False
    assert job.revision == 2


def test_editing_the_description_flags_missing_excerpts(session):
    job = seed(session)
    edited = job_service.clean_input(**(DEMO_JOB | {"description": DEMO_JOB["description"].replace("Experience with Rust.", "")}))
    job_service.update(session, job, edited)
    results = evaluate(session, job)
    assert results["r6"].excerpt_found is False
    assert all(r.excerpt_found for k, r in results.items() if k != "r6")
    assert excerpt_found(job, job.requirements[0])


# ---------------------------------------------------------------- checklist

def test_expected_statuses(session):
    assert statuses(session, seed(session)) == {
        "r1": "met",      # Python + SQL listed
        "r2": "met",      # pursuing B.S. Computer Science
        "r3": "met",      # authorized, no sponsorship needed
        "r4": "unknown",  # experience needs evidence
        "r5": "unknown",  # Java/Kafka missing, not denied
        "r6": "unmet",    # Rust confirmed absent
        "r7": "met",      # Austin, work mode any
    }


def test_every_result_shows_its_basis_and_excerpt(session):
    results = evaluate(session, seed(session))
    for result in results.values():
        assert result.basis and result.excerpt
    assert results["r1"].basis == "Profile lists Python, SQL"
    assert "edu-1" in results["r2"].basis
    assert results["r5"].basis == "Not in profile: Java, Kafka"
    assert results["r6"].basis == "You confirmed you don't have Rust"


def test_missing_information_stays_unknown(session):
    data = copy.deepcopy(SAMPLE_PROFILE)
    data["authorization"] = []
    data["education"] = []
    data["preferences"]["work_mode"] = None
    data["preferences"]["locations"] = []
    data["contact"]["location"] = ""
    result = statuses(session, seed(session, data))
    assert result["r2"] == result["r3"] == result["r7"] == "unknown"


def test_without_a_profile_everything_is_unknown(session):
    job = seed(session, profile=None)
    assert set(statuses(session, job).values()) == {"unknown"}


def test_explicit_conflicts_are_unmet(session):
    data = copy.deepcopy(SAMPLE_PROFILE)
    data["authorization"] = [{"country": "US", "authorized": True, "requires_sponsorship": True}]
    data["preferences"]["work_mode"] = "remote"
    result = statuses(session, seed(session, data))
    assert result["r3"] == "unmet" and result["r7"] == "unmet"


def test_lower_degree_is_unmet(session):
    data = copy.deepcopy(SAMPLE_PROFILE)
    data["education"][0]["degree"] = "Associate of Science"
    assert statuses(session, seed(session, data))["r2"] == "unmet"


def test_skill_aliases_match(session):
    data = copy.deepcopy(SAMPLE_PROFILE)
    data["skills"].append({"name": "JS"})
    profile_service.save(session, data)
    job = job_service.create(session, job_service.clean_input(title="Intern", company="Demo", description="We use JavaScript daily."))
    set_requirements(session, job, [{"text": "JS", "excerpt": "JavaScript", "criterion": {"type": "skill", "skills": ["JavaScript"]}}])
    assert statuses(session, job)["r1"] == "met"


def test_degree_field_and_location_need_whole_words():
    assert not checklist._field_matches("Economics", ["CS"])
    assert not checklist._field_matches("Art", ["Computer Science and Artificial Intelligence"])
    assert checklist._field_matches("Computer Science and Engineering", ["Computer Science"])
    prof = {"preferences": {"locations": ["US"]}, "contact": {"location": "New York, NY"}}
    assert checklist._location({"locations": ["Australia"]}, prof)[0] == checklist.UNKNOWN
    assert checklist._location({"locations": ["New York"]}, prof)[0] == checklist.MET


def test_partial_dates_are_unknown_when_ambiguous():
    def grad(end, **crit):
        return checklist._graduation(crit, {"education": [{"end": end}]})[0]

    assert grad("2026", **{"from": "2026-03"}) == checklist.UNKNOWN
    assert grad("2026", to="2026-06") == checklist.UNKNOWN
    assert grad("2025", **{"from": "2026-03"}) == checklist.UNMET
    assert grad("2026-05", **{"from": "2026-03", "to": "2026-06"}) == checklist.MET

    def avail(start):
        return checklist._availability({"start_by": "2026-03"}, {"availability": {"start_date": start}})[0]

    assert (avail("2026"), avail("2026-02"), avail("2027")) == (checklist.UNKNOWN, checklist.MET, checklist.UNMET)


@pytest.mark.parametrize("value,last_day", [("2026-02", 28), ("2028-02", 29), ("2026-04", 30)])
def test_month_dates_use_the_calendar_end_in_checklist_comparisons(value, last_day):
    year, month = map(int, value.split("-"))
    deadline = f"{value}-{last_day}"
    assert date_key(value, end_of_period=True) == (year, month, last_day)
    assert checklist._graduation({"to": deadline}, {"education": [{"end": value}]})[0] == checklist.MET
    assert checklist._availability({"start_by": deadline}, {"availability": {"start_date": value}})[0] == checklist.MET


@pytest.mark.parametrize("value", ["2026-02-30", "2026-01-00", "2026-04-99", "2026-02-29", "0000", "2026-13"])
def test_impossible_calendar_dates_are_rejected_and_cannot_be_compared(value):
    assert is_valid_date(value) is False
    assert date_key(value) is None
    assert date_key(value, end_of_period=True) is None


@pytest.mark.parametrize("value", [None, "", "present", "2026", "2026-02", "2028-02-29", "2026-04-30"])
def test_valid_partial_and_complete_dates_are_supported(value):
    assert is_valid_date(value) is True


def test_invalid_persisted_dates_remain_unknown_instead_of_crashing():
    assert checklist._graduation({"to": "2026-12"}, {"education": [{"end": "2026-02-30"}]})[0] == checklist.UNKNOWN
    assert checklist._graduation({"to": "2026-02-30"}, {"education": [{"end": "2026-01"}]})[0] == checklist.UNKNOWN
    assert checklist._availability({"start_by": "2026-12"}, {"availability": {"start_date": "2026-02-30"}})[0] == checklist.UNKNOWN


@pytest.mark.parametrize("start,end,status,expected", [
    ("2099-01", "2100-01", "pursuing", checklist.UNKNOWN),
    ("2099-01", "2100-01", "any", checklist.UNKNOWN),
    ("2026", "2027", "pursuing", checklist.UNKNOWN),
    ("2026-10", "2027", "pursuing", checklist.UNKNOWN),
    ("2025", "2026", "pursuing", checklist.UNKNOWN),
    ("2025", "2026-10", "pursuing", checklist.UNKNOWN),
    ("", "2027", "pursuing", checklist.UNKNOWN),
    ("2025", "2027", "pursuing", checklist.MET),
    ("2025", "present", "pursuing", checklist.MET),
    ("", "present", "pursuing", checklist.MET),
    ("2099", "present", "pursuing", checklist.UNKNOWN),
    ("2025", "2026-09", "completed", checklist.MET),
    ("2025", "2026-10", "completed", checklist.UNKNOWN),
])
def test_degree_enrollment_requires_unambiguous_current_dates(monkeypatch, start, end, status, expected):
    monkeypatch.setattr(checklist, "today_key", lambda: (2026, 10, 7))
    prof = {"education": [{"id": "edu-1", "degree": "Bachelor of Science", "institution": "Example",
                            "start": start, "end": end}]}
    crit = {"level": "bachelor", "fields": [], "status": status}
    assert checklist._degree(crit, prof)[0] == expected


@pytest.mark.parametrize("profile_country,requirement_country", [("UNITED STATES", "US"), ("US", "United States"), ("UK", "GB")])
def test_authorization_compares_existing_legacy_names_and_codes(profile_country, requirement_country):
    prof = {"authorization": [{"country": profile_country, "authorized": True, "requires_sponsorship": False}]}
    crit = {"country": requirement_country, "sponsorship_available": False}
    assert checklist._authorization(crit, prof)[0] == checklist.MET


def test_unsupported_legacy_country_does_not_establish_authorization():
    prof = {"authorization": [{"country": "Atlantis", "authorized": True, "requires_sponsorship": False}]}
    assert checklist._authorization({"country": "US"}, prof)[0] == checklist.UNKNOWN
    assert checklist._authorization({"country": "Atlantis"}, prof)[0] == checklist.UNKNOWN


def test_years_of_experience_stays_unknown_until_evidence(session):
    job = seed(session, requirements=None)
    set_requirements(session, job, [{"text": "2 years", "excerpt": "Experience building REST APIs.",
                                     "criterion": {"type": "years_experience", "years": 2, "area": "APIs"}}])
    assert evaluate(session, job)["r1"].basis == "Years of experience need confirmed evidence"


# ---------------------------------------------------------------- evidence and overrides

def test_confirmed_evidence_makes_experience_met_and_is_shown(session):
    job = seed(session)
    candidate = profile_service.get_candidate(session)
    checklist.link(session, job, candidate, "r4", ["exp-1-b1"])
    result = evaluate(session, job)["r4"]
    assert result.status == "met" and result.basis == "You confirmed supporting evidence"
    assert result.evidence[0].id == "exp-1-b1"
    assert result.evidence[0].text.startswith("Built a Flask REST API")
    link = job.evidence["r4"]
    assert link.profile_revision == candidate.revision and link.confirmed_at is not None


def test_evidence_never_turns_unmet_into_met(session):
    job = seed(session)
    checklist.link(session, job, profile_service.get_candidate(session), "r6", ["proj-1-b1"])
    assert evaluate(session, job)["r6"].status == "unmet"


def test_evidence_link_rejects_unknown_or_empty_sources(session):
    job = seed(session)
    candidate = profile_service.get_candidate(session)
    with pytest.raises(checklist.ChecklistError, match="exp-9-b9"):
        checklist.link(session, job, candidate, "r4", ["exp-9-b9"])
    with pytest.raises(checklist.ChecklistError):
        checklist.link(session, job, candidate, "r4", [])
    with pytest.raises(checklist.ChecklistError):
        checklist.link(session, job, candidate, "r404", ["exp-1-b1"])
    assert job.evidence == {}


def test_unlink_and_reject(session):
    job = seed(session)
    candidate = profile_service.get_candidate(session)
    checklist.link(session, job, candidate, "r4", ["exp-1-b1", "exp-1-b2"])
    checklist.unlink(session, job, "r4", "exp-1-b1")
    assert job.evidence["r4"].sources == ["exp-1-b2"]
    checklist.unlink(session, job, "r4", "exp-1-b2")
    assert "r4" not in job.evidence and evaluate(session, job)["r4"].status == "unknown"
    checklist.reject_suggestion(session, job, "r4", "proj-1-b1")
    assert job.evidence["r4"].rejected == ["proj-1-b1"]
    assert evaluate(session, job)["r4"].status == "unknown"
    # Linking a rejected item later is the user's choice and clears the rejection.
    checklist.link(session, job, candidate, "r4", ["proj-1-b1"])
    assert job.evidence["r4"].rejected == []


def test_evidence_removed_from_the_profile_is_reported(session):
    job = seed(session)
    candidate = profile_service.get_candidate(session)
    checklist.link(session, job, candidate, "r4", ["exp-1-b1"])
    edited = candidate.profile.model_dump()
    edited["experience"][0]["bullets"].pop(0)
    profile_service.save(session, edited)
    result = evaluate(session, job)["r4"]
    assert result.stale_links == ["exp-1-b1"] and result.status == "unknown"


@pytest.mark.parametrize("change", ["bullet", "technologies", "dates", "title"])
def test_changed_evidence_requires_reconfirmation(session, change):
    job = seed(session)
    candidate = profile_service.get_candidate(session)
    checklist.link(session, job, candidate, "r4", ["exp-1-b1"])
    old_hash = job.evidence["r4"].source_hashes["exp-1-b1"]
    edited = candidate.profile.model_dump()
    entry = edited["experience"][0]
    if change == "bullet":
        entry["bullets"][0]["text"] = "Answered customer support phone calls"
    elif change == "technologies":
        entry["technologies"] = ["Excel"]
    elif change == "dates":
        entry["end"] = "2024-07"
    else:
        entry["title"] = "Customer Support Intern"
    profile_service.save(session, edited)
    result = evaluate(session, job)["r4"]
    assert result.status == checklist.UNKNOWN and not result.evidence
    assert [e.id for e in result.changed_evidence] == ["exp-1-b1"]
    assert "needs confirmation" in result.basis
    assert checklist.confirmed_source_ids(job.evidence["r4"], candidate.profile) == []
    checklist.link(session, job, candidate, "r4", ["exp-1-b1"])
    assert job.evidence["r4"].source_hashes["exp-1-b1"] != old_hash
    assert evaluate(session, job)["r4"].status == checklist.MET
    assert not evaluate(session, job)["r4"].changed_evidence


def test_confirmation_survives_unrelated_contact_and_sibling_bullet_edits(session):
    job = seed(session)
    candidate = profile_service.get_candidate(session)
    checklist.link(session, job, candidate, "r4", ["exp-1-b1"])
    edited = candidate.profile.model_dump()
    edited["contact"]["phone"] = "+1 555 0199"
    edited["experience"][0]["bullets"][1]["text"] = "Automated customer reports"
    profile_service.save(session, edited)
    result = evaluate(session, job)["r4"]
    assert result.status == checklist.MET and not result.changed_evidence
    assert checklist.confirmed_source_ids(job.evidence["r4"], candidate.profile) == ["exp-1-b1"]


def test_entry_evidence_is_invalidated_when_its_bullets_change(session):
    job = seed(session)
    candidate = profile_service.get_candidate(session)
    checklist.link(session, job, candidate, "r4", ["exp-1"])
    edited = candidate.profile.model_dump()
    edited["experience"][0]["bullets"][0]["text"] = "Answered customer support phone calls"
    profile_service.save(session, edited)
    assert evaluate(session, job)["r4"].status == checklist.UNKNOWN


def test_linking_another_source_does_not_reconfirm_changed_existing_evidence(session):
    job = seed(session)
    candidate = profile_service.get_candidate(session)
    checklist.link(session, job, candidate, "r4", ["exp-1-b1"])
    old_hash = job.evidence["r4"].source_hashes["exp-1-b1"]
    edited = candidate.profile.model_dump()
    edited["experience"][0]["bullets"][0]["text"] = "Answered customer support phone calls"
    profile_service.save(session, edited)
    checklist.link(session, job, candidate, "r4", ["proj-1-b1"])
    assert job.evidence["r4"].source_hashes["exp-1-b1"] == old_hash
    result = evaluate(session, job)["r4"]
    assert [e.id for e in result.evidence] == ["proj-1-b1"]
    assert [e.id for e in result.changed_evidence] == ["exp-1-b1"]


def test_legacy_links_load_conservatively_and_can_be_reconfirmed(session):
    job = seed(session)
    candidate = profile_service.get_candidate(session)
    job.evidence = {"r4": EvidenceLink(sources=["exp-1-b1"], profile_revision=candidate.revision)}
    session.commit()
    session.refresh(job)
    assert evaluate(session, job)["r4"].status == checklist.UNKNOWN
    assert [e.id for e in evaluate(session, job)["r4"].changed_evidence] == ["exp-1-b1"]
    checklist.link(session, job, candidate, "r4", ["exp-1-b1"])
    session.refresh(job)
    assert evaluate(session, job)["r4"].status == checklist.MET
    assert job.evidence["r4"].source_hashes["exp-1-b1"]


def test_changed_evidence_is_visible_with_an_enabled_reconfirmation_control(session, settings):
    job = seed(session)
    candidate = profile_service.get_candidate(session)
    job.evidence = {"r4": EvidenceLink(sources=["exp-1-b1"])}
    result = evaluate(session, job)["r4"]
    html = templates.env.get_template("checklist.html").render(
        job=job, settings=settings, candidate=candidate,
        counts=checklist.summary([result]), result_groups={checklist.MET: [], checklist.UNMET: [], checklist.UNKNOWN: [result]},
        source_groups=[{"label": "Experience", "items": [("exp-1-b1", result.changed_evidence[0].text)]}],
        suggestions={"by_requirement": {}, "run": None, "out_of_date": False},
    )
    assert "Linked evidence changed or needs confirmation" in html
    assert 'aria-label="Reconfirm evidence exp-1-b1"' in html
    checkbox = re.search(r'<input type="checkbox"[^>]*value="exp-1-b1"[^>]*>', html).group(0)
    assert "disabled" not in checkbox


def test_override_requires_a_reason_and_is_reported(session):
    job = seed(session)
    with pytest.raises(checklist.ChecklistError):
        checklist.set_override(session, job, "r5", "met", "  ")
    with pytest.raises(checklist.ChecklistError):
        checklist.set_override(session, job, "r5", "maybe", "because")
    checklist.set_override(session, job, "r5", "met", "Used Kafka in a course project not yet in profile")
    result = evaluate(session, job)["r5"]
    assert result.status == "met" and result.computed_status == "unknown"
    assert result.basis == "Your override: Used Kafka in a course project not yet in profile (computed: Unknown)"
    checklist.set_override(session, job, "r5", "clear", "")
    assert evaluate(session, job)["r5"].status == "unknown"


def test_summary_counts_required_gaps(session):
    data = copy.deepcopy(SAMPLE_PROFILE)
    data["authorization"] = [{"country": "US", "authorized": False, "requires_sponsorship": True}]
    job = seed(session, data)
    candidate = profile_service.get_candidate(session)
    counts = checklist.summary(checklist.evaluate(job, candidate.profile))
    # r3 (required) and r6 (preferred) are unmet; only r3 counts as a required gap.
    assert counts == {"met": 3, "unmet": 2, "unknown": 2, "required_unmet": 1}
