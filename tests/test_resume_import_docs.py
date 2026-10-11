"""The README says which resume layouts the import reads and which it can't."""

from __future__ import annotations

from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text()


def test_the_readme_lists_what_the_resume_import_reads_and_cant_read():
    assert "**What it reads:** one- and two-column PDFs" in README
    assert "**What it can't read (fill these in yourself" in README
    for layout in ("without recognizable section headings", "text in images", "languages other than English"):
        assert layout in README
