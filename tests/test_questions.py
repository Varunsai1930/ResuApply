"""Question classification and profile-backed factual values (synthetic data only)."""

from __future__ import annotations

import copy

import pytest

from app.schemas.profile import Authorization, Profile
from app.services.questions import classify, factual_value
from tests.synthetic import SAMPLE_PROFILE


@pytest.fixture
def profile(session) -> Profile:
    from app.services import profile as profile_service
    return profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate.profile


@pytest.mark.parametrize("text,expected", [
    ("What is your email address?", ("factual", "email")),
    ("Phone number", ("factual", "phone")),
    ("LinkedIn profile URL", ("factual", "linkedin")),
    ("GitHub", ("factual", "github")),
    ("Personal website or portfolio", ("factual", "portfolio")),
    ("When do you expect to graduate?", ("factual", "graduation_date")),
    ("What is your earliest start date?", ("factual", "start_date")),
    ("Which university do you attend?", ("factual", "school")),
    ("What is your major?", ("factual", "major")),
    ("What degree are you pursuing?", ("factual", "degree")),
    ("What is your GPA?", ("factual", "gpa")),
    ("Where are you currently located?", ("factual", "location")),
    ("Full name", ("factual", "name")),
    ("What is your gender?", ("sensitive", None)),
    ("Do you have a disability?", ("sensitive", None)),
    ("Are you a protected veteran?", ("sensitive", None)),
    ("Have you ever been convicted of a felony?", ("sensitive", None)),
    ("What are your salary expectations?", ("sensitive", None)),
    ("I agree to the privacy policy and consent to processing", ("sensitive", None)),
    ("I certify that the above is true", ("sensitive", None)),
    ("Are you legally authorized to work in the United States?", ("sensitive_factual", "authorization")),
    ("Are you eligible to work in Canada?", ("sensitive_factual", "authorization")),
    ("Will you now or in the future require visa sponsorship?", ("sensitive_factual", "sponsorship")),
    ("Do you need H-1B sponsorship?", ("sensitive_factual", "sponsorship")),
    ("Why do you want to work here?", ("open", None)),
    ("Describe a project you are proud of", ("open", None)),
    ("Tell us about yourself", ("open", None)),
    ("Anything else you'd like to add?", ("open", None)),
    ("Are you willing to relocate?", ("unknown", None)),
    ("Preferred pronouns for the team", ("sensitive", None)),
    ("Favourite colour", ("unknown", None)),
    ("", ("unknown", None)),
])
def test_classification_rules(text, expected):
    assert classify(text) == expected


def test_sensitive_wins_over_other_rules():
    # "age" and "consent" match before any factual or open rule
    assert classify("Describe your age and why you want this job")[0] == "sensitive"
    assert classify("Please provide your email and consent to contact")[0] == "sensitive"


def test_profile_values(profile):
    assert factual_value("name", "Name", profile) == "Jordan Example"
    assert factual_value("email", "Email", profile) == "jordan@example.com"
    assert factual_value("phone", "Phone", profile) == "+1 555 0100"
    assert factual_value("location", "Location", profile) == "Austin, TX"
    assert factual_value("github", "GitHub", profile) == "github.com/jordan-example"
    assert factual_value("portfolio", "Portfolio", profile) is None  # not in the profile
    assert factual_value("graduation_date", "Graduation", profile) == "2099-05"
    assert factual_value("start_date", "Start", profile) == "2099-06"
    assert factual_value("school", "School", profile) == "Example State University"
    assert factual_value("degree", "Degree", profile) == "B.S."
    assert factual_value("major", "Major", profile) == "Computer Science"
    assert factual_value("gpa", "GPA", profile) == "3.7"
    assert factual_value("unheard-of", "?", profile) is None


def test_latest_education_entry_supplies_school_values(profile):
    earlier = profile.education[0].model_copy(update={"id": "edu-0", "institution": "Earlier College", "end": "2020-05"})
    both = profile.model_copy(update={"education": [earlier, *profile.education]})
    assert factual_value("school", "School", both) == "Example State University"
    assert factual_value("graduation_date", "Graduation", both) == "2099-05"


def test_accepts_a_profile_dict_as_well(profile):
    assert factual_value("email", "Email", profile.model_dump()) == "jordan@example.com"


def with_authorization(profile: Profile, *records: Authorization) -> Profile:
    return profile.model_copy(update={"authorization": list(records)})


def test_country_mapping_never_answers_one_country_from_anothers_record(profile):
    us_only = with_authorization(profile, Authorization(country="US", authorized=True, requires_sponsorship=False))
    assert factual_value("authorization", "Authorized to work in the United States?", us_only) == "Yes"
    assert factual_value("authorization", "Are you authorized to work in the U.S.?", us_only) == "Yes"
    assert factual_value("sponsorship", "Do you require sponsorship in the USA?", us_only) == "No"
    # A named country without a record is unknown, not the US answer
    assert factual_value("authorization", "Are you authorized to work in Canada?", us_only) is None
    assert factual_value("authorization", "Are you authorized to work in the UK?", us_only) is None
    # A place the mapping doesn't know is never answered from the only record
    assert factual_value("authorization", "Are you authorized to work in Narnia?", us_only) is None
    # No country named: the single record answers
    assert factual_value("authorization", "Are you authorized to work here?", us_only) == "Yes"


def test_several_records_need_the_country_named(profile):
    two = with_authorization(
        profile,
        Authorization(country="US", authorized=True, requires_sponsorship=False),
        Authorization(country="CA", authorized=False, requires_sponsorship=True),
    )
    assert factual_value("authorization", "Authorized to work in Canada?", two) == "No"
    assert factual_value("sponsorship", "Sponsorship needed in Canada?", two) == "Yes"
    assert factual_value("authorization", "Authorized to work in the United States?", two) == "Yes"
    assert factual_value("authorization", "Are you authorized to work here?", two) is None


def test_unknown_authorization_stays_unknown(profile):
    unknown = with_authorization(profile, Authorization(country="US"))
    assert factual_value("authorization", "Authorized to work in the US?", unknown) is None
    assert factual_value("sponsorship", "Sponsorship?", unknown) is None
    assert factual_value("authorization", "Authorized to work?", with_authorization(profile)) is None
