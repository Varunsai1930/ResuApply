"""Source guards shared with ResuSkill, including strict shape/privacy boundaries."""

from __future__ import annotations

import copy

import pytest

from app.services import outbound, profile, resume
from app.services.claims import claim_problems, credentials, numbers
from app.services.skills import find_terms
from tests.conftest import SAMPLE_PROFILE


@pytest.fixture
def prof():
    return profile.normalize(copy.deepcopy(SAMPLE_PROFILE)).profile


def one_bullet(text="Wrote unit tests for the billing module", sources=None, entry="exp-1"):
    return {"experience": [{"entry": entry, "bullets": [{"text": text, "sources": ["exp-1-b3"] if sources is None else sources}]}]}


def test_good_proposal_keeps_order_and_defaults(prof):
    data = one_bullet("Built a Flask REST API serving 1200 internal users", ["exp-1-b1"])
    data["projects"] = [{"entry": "proj-1", "bullets": [{"text": "Used by three student clubs", "sources": ["proj-1-b2"]}]}]
    clean, warnings = resume.validate_resume(prof, {}, data)
    assert clean.experience[0].entry == "exp-1"
    assert clean.education == ["edu-1"]
    assert clean.skills == [s.name for s in prof.skills]
    assert warnings == []


@pytest.mark.parametrize("text,sources,fragment", [
    ("Built a Java REST API serving 1200 users", ["exp-1-b1"], "Java"),
    ("Reduced report time by 60% using PostgreSQL indexes", ["exp-1-b2"], "60"),
    ("Wrote unit tests in 2021", ["exp-1-b3"], "2021"),
    ("Wrote tests for twelve billing modules", ["exp-1-b3"], "12"),
    ("Doubled billing test coverage", ["exp-1-b3"], "2"),
    ("Certified engineer who wrote unit tests", ["exp-1-b3"], "certification"),
    ("Master’s degree in testing", ["exp-1-b3"], "master's degree"),
    ("Used by 3 student clubs", ["proj-1-b2"], "different entry"),
    ("Wrote tests", ["exp-1-b99"], "unknown sources"),
    ("Wrote tests", ["exp-1"], "source bullets"),
    ("Built Flask services", ["summary"], "different entry"),
])
def test_fabricated_or_inappropriate_bullet_sources_are_rejected(prof, text, sources, fragment):
    with pytest.raises(resume.ResumeInvalid) as exc:
        resume.validate_resume(prof, {}, one_bullet(text, sources))
    assert fragment in " ".join(exc.value.problems)


@pytest.mark.parametrize("data", [
    None, [], "resume", {"experience": "exp-1"}, {"experience": [None]},
    {"experience": [{"entry": "exp-1", "bullets": "Wrote tests"}]},
    one_bullet(sources=[]), one_bullet(sources="exp-1-b3"), one_bullet(sources=[1]),
    one_bullet(text=12), one_bullet(text=" "),
    {"summary": "Backend engineer"}, {"summary": {"text": "Backend engineer", "sources": []}},
    {"education": "edu-1"}, {"education": [1]}, {"certifications": [{}]}, {"skills": [False]},
    {"contact": {"name": "Invented person"}},
    {"experience": [{"entry": "exp-1", "organization": "Invented employer", "bullets": []}]},
])
def test_malformed_ai_payloads_fail_with_resume_invalid(prof, data):
    with pytest.raises(resume.ResumeInvalid):
        resume.validate_resume(prof, {}, data)


def test_new_skill_unknown_selected_entry_and_duplicates_are_rejected(prof):
    for data, fragment in [({"skills": ["Python", "Kubernetes"]}, "Kubernetes"),
                           ({"education": ["edu-99"]}, "unknown entries"),
                           ({"certifications": ["cert-99"]}, "unknown entries"),
                           ({"projects": [{"entry": "exp-1"}]}, "not a projects entry")]:
        with pytest.raises(resume.ResumeInvalid, match=fragment):
            resume.validate_resume(prof, {}, data)
    data = one_bullet()
    data["experience"] *= 2
    with pytest.raises(resume.ResumeInvalid, match="appears twice"):
        resume.validate_resume(prof, {}, data)


def test_entry_technologies_and_numeric_formatting_are_supported(prof):
    resume.validate_resume(prof, {}, one_bullet("Wrote Flask unit tests for billing"))
    resume.validate_resume(prof, {}, one_bullet("Served 1200 users via a Flask REST API", ["exp-1-b1"]))
    clean, _ = resume.validate_resume(prof, {}, {"skills": ["Postgres", "PostgreSQL", "python3"]})
    assert clean.skills == ["PostgreSQL", "Python"]


def test_merging_sources_within_one_entry_is_supported(prof):
    clean, _ = resume.validate_resume(prof, {}, one_bullet("Built a Flask API serving 1200 users and reduced report time by 35%",
                                                         ["exp-1-b1", "exp-1-b2"]))
    assert clean.experience[0].bullets[0].sources == ["exp-1-b1", "exp-1-b2"]


def test_summary_uses_source_credentials_and_profile_skills(prof):
    resume.validate_resume(prof, {}, {"summary": {"text": "B.S. student building Python services", "sources": ["edu-1", "summary"]}})
    with pytest.raises(resume.ResumeInvalid, match="master's degree"):
        resume.validate_resume(prof, {}, {"summary": {"text": "Master’s graduate building Python services", "sources": ["edu-1", "summary"]}})


def test_common_words_do_not_count_as_technologies():
    assert find_terms("We go to the rest area and react quickly; the spring term ended") == set()
    assert {"c++", "c#", "kubernetes", "postgresql"} <= find_terms("Shipped C++ and C# services on k8s with Postgres")
    assert "java" not in find_terms("Wrote JavaScript")
    assert {"c", "c++", "python", "django"} <= find_terms("C/C++ and Python/Django")


def test_number_words_and_credential_classes():
    assert numbers("$1,200.00 users in 2021, twice, halved and dozens") == {"1200", "2021", "2", "0.5", "12"}
    assert {"master's degree", "doctorate", "PMP credential", "certification", "award"} <= credentials("Master’s, Ph.D., PMP certified award-winning engineer")
    assert "master's degree" in " ".join(claim_problems("Master’s degree", ["Bachelor’s degree"], set()))
    assert numbers("0.50 and .5") == numbers("halved") == {"0.5"}


@pytest.mark.parametrize("text,expected", [
    ("zero incidents and one year", {"0", "1"}),
    ("1,200k users", {"1200000"}),
    ("1.2m users and 2 billion events", {"1200000", "2000000000"}),
    ("1.2 million users", {"1200000"}),
    ("twelve hundred users", {"1200"}),
    ("two thousand users", {"2000"}),
])
def test_numbers_preserve_scale_and_number_words(text, expected):
    assert numbers(text) == expected


@pytest.mark.parametrize("text,sources,fragment", [
    ("One year of experience writing unit tests", ["exp-1-b3"], "1"),
    ("Supported 1,200k users", ["exp-1-b1"], "1200000"),
    ("Zero billing incidents", ["exp-1-b3"], "0"),
])
def test_scaled_and_small_fabricated_metrics_are_rejected(prof, text, sources, fragment):
    with pytest.raises(resume.ResumeInvalid, match=fragment):
        resume.validate_resume(prof, {}, one_bullet(text, sources))


@pytest.mark.parametrize("text,credential", [
    ("MS graduate", "master's degree"),
    ("BS graduate", "bachelor's degree"),
    ("Earned an MS", "master's degree"),
    ("AA degree", "associate's degree"),
    ("Honoured graduate", "honor"),
])
def test_uncited_degrees_and_honors_are_rejected(prof, text, credential):
    with pytest.raises(resume.ResumeInvalid, match=credential):
        resume.validate_resume(prof, {}, {"summary": {"text": text, "sources": ["summary"]}})


def test_professional_credentials_cannot_substitute_for_each_other():
    assert credentials("PMP") == {"PMP credential"}
    assert "CPA credential" in " ".join(claim_problems("CPA certified", ["PMP certified"], set()))
    assert credentials("Reported request times in ms") == set()


@pytest.mark.parametrize("degree,text", [
    ("MS", "MS graduate"),
    ("BS", "Bachelor’s degree in Computer Science"),
    ("AA", "Associate’s degree"),
])
def test_summary_degree_abbreviations_are_supported_by_cited_education(prof, degree, text):
    data = prof.model_dump()
    data["education"][0]["degree"] = degree
    prof = profile.normalize(data).profile
    resume.validate_resume(prof, {}, {"summary": {"text": text, "sources": ["edu-1"]}})


def test_shared_context_defaults_do_not_restore_excluded_selection(prof):
    context = outbound.apply_choices(outbound.reduced_context(prof), {"edu-1", "skills", "exp-1", "summary"}, {})
    clean, _ = resume.validate_resume(prof, {}, {}, context)
    assert clean.education == [] and clean.skills == [] and clean.summary is None
    for data in [one_bullet(), {"education": ["edu-1"]}, {"summary": {"text": "Student", "sources": ["summary"]}}]:
        with pytest.raises(resume.ResumeInvalid, match="shared context"):
            resume.validate_resume(prof, {}, data, context)


def test_excluded_bullet_cannot_be_cited_even_when_entry_is_shared(prof):
    context = outbound.apply_choices(outbound.reduced_context(prof), {"exp-1-b3"}, {})
    with pytest.raises(resume.ResumeInvalid, match="shared context"):
        resume.validate_resume(prof, {}, one_bullet(), context)


def test_reworded_outbound_text_does_not_authorize_fabricated_metrics(prof):
    context = outbound.apply_choices(outbound.reduced_context(prof), set(), {"exp-1-b3": "Improved billing tests by 99% using Java"})
    with pytest.raises(resume.ResumeInvalid) as exc:
        resume.validate_resume(prof, {}, one_bullet("Improved billing tests by 99% using Java"), context)
    assert "99" in " ".join(exc.value.problems) and "Java" in " ".join(exc.value.problems)


def test_ai_claim_must_also_match_the_reworded_shared_source(prof):
    context = outbound.apply_choices(outbound.reduced_context(prof), set(), {"exp-1-b1": "Built a REST API"})
    with pytest.raises(resume.ResumeInvalid, match="shared context numbers"):
        resume.validate_resume(prof, {}, one_bullet("Served 1200 users via a Flask REST API", ["exp-1-b1"]), context)
