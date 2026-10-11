"""A draft profile from a resume's text, read locally (services/resume_draft.py). Synthetic text only."""

from __future__ import annotations

import copy

from app.services import profile as profile_service
from app.services.resume_draft import draft_profile, merge, sections
from tests.synthetic import SAMPLE_PROFILE

RESUME = """Jordan Example
Austin, TX | jordan@example.com | +1 555 0100 | linkedin.com/in/jordan-example | github.com/jordan-example

SUMMARY
Computer science student who builds backend services in Python.

EDUCATION
Example State University | B.S. in Computer Science | Aug 2023 - May 2027 | GPA: 3.7
• Coursework: Data Structures, Operating Systems, Databases

EXPERIENCE
Software Engineering Intern | Sample Analytics | Remote | Jun 2024 – Aug 2024
• Built a Flask REST API in Python that served reporting data
to 1,200 internal users
• Reduced report generation time by 35% by adding PostgreSQL indexes

PROJECTS
TaskBot | Creator | github.com/jordan-example/taskbot | Nov 2023 - Feb 2024
- Created a Python chat bot that tracks team tasks in SQLite

SKILLS
Languages: Python, SQL, JavaScript
Tools: Git, Docker

CERTIFICATIONS
• Example Cloud Practitioner

AWARDS
Dean's List 2024
"""


def test_contact_details_come_from_the_lines_before_the_first_heading():
    contact = draft_profile(RESUME)["contact"]
    assert contact == {"name": "Jordan Example", "email": "jordan@example.com", "phone": "+1 555 0100",
                       "location": "Austin, TX",
                       "links": {"linkedin": "linkedin.com/in/jordan-example", "github": "github.com/jordan-example",
                                 "portfolio": ""}}


def test_entries_keep_their_parts_dates_and_bullets():
    draft = draft_profile(RESUME)
    assert draft["summary"] == "Computer science student who builds backend services in Python."
    (school,) = draft["education"]
    assert (school["institution"], school["degree"], school["field"], school["start"], school["end"], school["gpa"]) == (
        "Example State University", "B.S.", "Computer Science", "2023-08", "2027-05", "3.7")
    (job,) = draft["experience"]
    assert (job["organization"], job["title"], job["location"], job["start"], job["end"]) == (
        "Sample Analytics", "Software Engineering Intern", "Remote", "2024-06", "2024-08")
    assert [b["text"] for b in job["bullets"]] == [
        "Built a Flask REST API in Python that served reporting data to 1,200 internal users",  # a wrapped bullet
        "Reduced report generation time by 35% by adding PostgreSQL indexes",
    ]
    (project,) = draft["projects"]
    assert (project["name"], project["role"], project["link"], project["start"], project["end"]) == (
        "TaskBot", "Creator", "github.com/jordan-example/taskbot", "2023-11", "2024-02")
    assert [(s["name"], s["category"]) for s in draft["skills"]] == [
        ("Python", "Languages"), ("SQL", "Languages"), ("JavaScript", "Languages"), ("Git", "Tools"), ("Docker", "Tools")]
    assert [c["name"] for c in draft["certifications"]] == ["Example Cloud Practitioner"]
    assert draft["authorization"] == []  # always the user's own answer, never read from a resume


def test_an_unknown_heading_ends_the_section_before_it():
    assert "Dean's List 2024" not in str(draft_profile(RESUME))
    assert "other" not in sections(RESUME)


def test_two_line_headings_present_and_expected_dates():
    draft = draft_profile("""Jordan Example
jordan@example.com

Experience
Sample Analytics — Austin, TX
Software Engineering Intern, Jun 2024 - Present
- Wrote unit tests for the billing module

Education
Example State University
Bachelor of Science in Computer Science, Expected May 2027
""")
    (job,) = draft["experience"]
    assert (job["organization"], job["title"], job["location"], job["start"], job["end"]) == (
        "Sample Analytics", "Software Engineering Intern", "Austin, TX", "2024-06", "present")
    (school,) = draft["education"]
    assert (school["institution"], school["degree"], school["field"], school["end"]) == (
        "Example State University", "Bachelor of Science", "Computer Science", "2027-05")


def test_what_isnt_contact_information_stays_out_of_it():
    contact = draft_profile("Jordan Example\nPython, SQL | 2023 - 2027 | jordan@example.com\n\nSkills\nPython")["contact"]
    assert contact["phone"] == "" and contact["location"] == "" and contact["name"] == "Jordan Example"


def test_the_draft_passes_the_profiles_own_checks():
    result = profile_service.normalize(draft_profile(RESUME))
    assert result.profile.contact.name == "Jordan Example"
    assert [b.text for b in result.profile.experience[0].bullets][0].startswith("Built a Flask REST API")


def test_merge_never_changes_what_the_profile_holds():
    current = profile_service.normalize(copy.deepcopy(SAMPLE_PROFILE)).profile.model_dump()
    current["contact"]["phone"] = ""
    draft = draft_profile(RESUME.replace("+1 555 0100", "+1 555 0199").replace("Jordan Example\n", "Someone Else\n"))
    draft["experience"].append({"id": "", "organization": "Example Labs", "title": "Research Assistant", "location": "",
                                "start": "2025-01", "end": "", "technologies": [], "bullets": []})
    merged = merge(current, draft)
    assert merged.data["contact"]["name"] == "Jordan Example"  # kept
    assert merged.data["contact"]["phone"] == "+1 555 0199"  # was empty: filled
    assert [e["organization"] for e in merged.data["experience"]] == ["Sample Analytics", "Example Labs"]  # no duplicate
    assert merged.data["experience"][0] == current["experience"][0]
    assert [s["name"] for s in merged.data["skills"]][-2:] == ["JavaScript", "Docker"]  # only the new skills
    assert "Phone" in merged.added and "1 experience entry" in merged.added and "2 skills" in merged.added
    assert "Name" not in merged.added and merged.data["authorization"] == current["authorization"]


def test_merge_with_nothing_new_adds_nothing():
    current = profile_service.normalize(copy.deepcopy(SAMPLE_PROFILE)).profile.model_dump()
    merged = merge(current, draft_profile("Jordan Example\njordan@example.com\n\nSkills\nPython, SQL"))
    assert merged.added == [] and merged.data == current


def test_all_caps_names_and_companies_are_not_taken_for_headings():
    draft = draft_profile("""JORDAN EXAMPLE
jordan@example.com | Austin, TX

EXPERIENCE
SAMPLE ANALYTICS
Software Engineering Intern | Jun 2024 - Aug 2024
- Built a Flask REST API in Python

VOLUNTEERING
Food bank driver
""")
    assert draft["contact"]["name"] == "JORDAN EXAMPLE" and draft["contact"]["email"] == "jordan@example.com"
    (job,) = draft["experience"]
    assert (job["organization"], job["title"]) == ("SAMPLE ANALYTICS", "Software Engineering Intern")
    assert "Food bank" not in str(draft)  # a known heading outside the profile still ends the section


def test_a_long_line_without_an_email_address_is_read_quickly():
    import time

    started = time.perf_counter()
    draft_profile("a" * 100_000 + "\n" + "b." * 50_000 + "@\n\nSkills\nPython")
    assert time.perf_counter() - started < 1


def test_an_email_address_is_still_found_next_to_long_words():
    assert draft_profile("x" * 300 + " jordan.example+cv@mail.example.co.uk")["contact"]["email"] == (
        "jordan.example+cv@mail.example.co.uk")


def test_a_comma_separated_education_line_is_split_into_its_parts():
    def school(line):
        (entry,) = draft_profile(f"Jordan Example\n\nEducation\n{line}\n")["education"]
        return entry["institution"], entry["degree"], entry["field"], entry["end"]

    assert school("Example State University, B.S. in Computer Science, May 2027") == (
        "Example State University", "B.S.", "Computer Science", "2027-05")
    assert school("Bachelor of Science, Computer Science, Example State University, 2027") == (
        "Example State University", "Bachelor of Science", "Computer Science", "2027")
