"""The README describes how graduation questions are answered."""

from __future__ import annotations

from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()


def test_the_readme_describes_graduation_answers():
    assert "A **Graduation year** question is yours to answer" not in README
    assert "a **graduation year** question gets the year" in README
    assert "several degrees with different dates and the question doesn't name one" in README
    assert "now asks for a profile field (such as a graduation year) becomes Factual" in README
