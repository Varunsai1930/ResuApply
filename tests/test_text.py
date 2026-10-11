"""The shared text helpers (services/text.py)."""

from __future__ import annotations

from app.services import greenhouse, resume_import
from app.services.text import tidy_lines


def test_tidy_lines_makes_spaces_single_and_blank_runs_one_line():
    assert tidy_lines("  Python  and\tSQL  \r\n\n\n\nTeam\x00 lead\r") == "Python and SQL\n\nTeam lead"


def test_the_resume_import_and_the_greenhouse_import_tidy_text_the_same_way():
    messy = "First line   here\r\n\n\n\nSecond\tline"
    assert resume_import._tidy(messy) == tidy_lines(messy) == "First line here\n\nSecond line"
    # A carriage return inside a posting's text is a line break there too.
    assert greenhouse.html_text("<p>First line   here\rSecond\tline</p>") == "First line here\nSecond line"
