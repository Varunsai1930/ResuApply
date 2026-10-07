"""Parsing the guided profile form into profile data."""

from __future__ import annotations

from app.services.profile_form import parse_form, parse_skills, skills_text


def test_indexed_fields_become_entries_in_form_order():
    data = parse_form([
        ("contact-name", "Jordan Example"),
        ("experience-n2-organization", "Second Example"),
        ("experience-0-id", "exp-1"),
        ("experience-0-organization", "First Example"),
        ("experience-0-technologies", "Python,  Flask ,"),
        ("experience-0-bullets-0-id", "exp-1-b1"),
        ("experience-0-bullets-0-text", "Built an API"),
        ("experience-0-bullets-x9-id", ""),
        ("experience-0-bullets-x9-text", "Wrote tests"),
    ])
    new_row, saved_row = data["experience"]  # rows keep the order they first appear in
    assert new_row["organization"] == "Second Example" and new_row["id"] == ""
    assert saved_row["_form"] == "experience-0" and saved_row["id"] == "exp-1"
    assert saved_row["technologies"] == ["Python", "Flask"]
    assert saved_row["bullets"] == [
        {"_form": "experience-0-bullets-0", "id": "exp-1-b1", "text": "Built an API"},
        {"_form": "experience-0-bullets-x9", "id": "", "text": "Wrote tests"},
    ]


def test_blank_rows_and_cleared_bullets_are_dropped():
    data = parse_form([
        ("education-0-institution", ""),
        ("education-0-degree", "  "),
        ("projects-0-name", "TaskBot"),
        ("projects-0-bullets-0-id", "proj-1-b1"),
        ("projects-0-bullets-0-text", "   "),
        ("certifications-0-name", ""),
        ("authorization-0-country", ""),
        ("authorization-0-authorized", ""),
    ])
    assert data["education"] == []
    assert data["projects"][0]["bullets"] == []
    assert data["certifications"] == []
    assert data["authorization"] == []


def test_authorization_values_map_to_yes_no_unknown():
    data = parse_form([
        ("authorization-0-country", "US"),
        ("authorization-0-authorized", "yes"),
        ("authorization-0-requires_sponsorship", "no"),
        ("authorization-1-country", "CA"),
        ("authorization-1-authorized", ""),
        ("authorization-1-requires_sponsorship", "perhaps"),
    ])
    assert [(a["country"], a["authorized"], a["requires_sponsorship"]) for a in data["authorization"]] == [
        ("US", True, False),
        ("CA", None, "perhaps"),  # left for validation to reject
    ]


def test_lists_and_preferences():
    data = parse_form([
        ("skills_absent", "Rust, Go\nScala"),
        ("preferences-roles", "Backend Engineer\n\nData Engineer"),
        ("preferences-locations", "Austin, TX\nRemote"),
        ("preferences-work_mode", "remote"),
        ("availability-start_date", "2099-06"),
    ])
    assert data["skills_absent"] == ["Rust", "Go", "Scala"]
    assert data["preferences"] == {"roles": ["Backend Engineer", "Data Engineer"], "locations": ["Austin, TX", "Remote"], "work_mode": "remote"}
    assert data["availability"]["start_date"] == "2099-06"


def test_skills_text_round_trip():
    skills = parse_skills("Languages: Python, SQL\nGit, Docker\n\nFrameworks: Flask")
    assert skills == [
        {"name": "Python", "category": "Languages"},
        {"name": "SQL", "category": "Languages"},
        {"name": "Git", "category": "Skills"},
        {"name": "Docker", "category": "Skills"},
        {"name": "Flask", "category": "Frameworks"},
    ]
    assert parse_skills(skills_text(skills)) == skills
