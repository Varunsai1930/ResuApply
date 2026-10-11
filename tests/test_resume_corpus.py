"""A corpus of fictional, synthetic resumes in the layouts real resumes use, read end to end.

Each case builds its file here (PDF through tests.resume_files, Word through python-docx), reads
it with the import (services/resume_import.py) and drafts the profile (services/resume_draft.py).
Nothing is sent anywhere; every name, company and school is made up.
"""

from __future__ import annotations

from app.services.resume_draft import draft_profile
from app.services.resume_import import extract_text
from tests.resume_files import layout_pdf_bytes


def read(data: bytes) -> dict:
    return draft_profile(extract_text(data))


def test_a_project_without_a_link_keeps_its_name():
    draft = draft_profile("Jordan Example\n\nProjects\nTaskBot | Creator | Nov 2023 – Feb 2024\n"
                          "- Made a chat bot that tracks team tasks.\nBudget Tracker\n- Tracked spending in SQLite\n")
    assert [(p["name"], p["role"], p["link"]) for p in draft["projects"]] == [
        ("TaskBot", "Creator", ""), ("Budget Tracker", "", "")]
    pdf = read(layout_pdf_bytes([[(50, 750, "Jordan Example"), (50, 730, "PROJECTS"),
                                  (50, 715, "TaskBot | Python, SQLite"), (60, 700, "• Created a chat bot")]]))
    assert pdf["projects"][0]["name"] == "TaskBot"
