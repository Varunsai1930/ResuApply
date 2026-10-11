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


def word(build) -> bytes:
    """A Word file made by ``build(document)``."""
    import io

    from docx import Document

    document = Document()
    build(document)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def bulleted(document, text: str) -> None:
    """A paragraph bulleted as Word does it by hand: List Paragraph with numbering on the paragraph itself."""
    paragraph = document.add_paragraph(text, style="List Paragraph")
    num_pr = paragraph._p.get_or_add_pPr().get_or_add_numPr()
    num_pr.get_or_add_ilvl().val = 0
    num_pr.get_or_add_numId().val = 1


def test_word_list_bullets_stay_bullets():
    def build(document):
        for line in ("Jordan Example", "jordan@example.com | Austin, TX", "Experience",
                     "Sample Analytics | Software Engineering Intern | Jun 2024 – Aug 2024"):
            document.add_paragraph(line)
        document.add_paragraph("Built a Flask REST API in Python", style="List Bullet")  # bulleted by its style
        document.add_paragraph("Reduced report time by 35%", style="List Bullet")
        document.add_paragraph("Example Labs | Research Assistant | Jan 2025 – Present")
        bulleted(document, "Cleaned survey data in Python")  # bulleted on the paragraph
        unnumbered = document.add_paragraph("Skills", style="List Paragraph")  # indented, not a list item
        unnumbered.paragraph_format.left_indent = 0
        document.add_paragraph("Python, SQL")

    draft = read(word(build))
    assert [(e["organization"], [b["text"] for b in e["bullets"]]) for e in draft["experience"]] == [
        ("Sample Analytics", ["Built a Flask REST API in Python", "Reduced report time by 35%"]),
        ("Example Labs", ["Cleaned survey data in Python"]),
    ]
    assert [s["name"] for s in draft["skills"]] == ["Python", "SQL"]
