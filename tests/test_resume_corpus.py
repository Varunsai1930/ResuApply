"""A corpus of fictional, synthetic resumes in the layouts real resumes use, read end to end.

Each case builds its file here (PDF through tests.resume_files, Word through python-docx), reads
it with the import (services/resume_import.py) and drafts the profile (services/resume_draft.py).
Nothing is sent anywhere; every name, company and school is made up.
"""

from __future__ import annotations

from app.services.resume_draft import draft_profile
from app.services.resume_import import extract_text
from tests.resume_files import column, layout_pdf_bytes
from tests.test_profile_sections import form_fields


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


def test_entries_without_bullets_stay_apart():
    draft = draft_profile("""Jordan Example

Education
Example State University | Austin, TX
B.S. in Computer Science | Aug 2023 – May 2027
Example Community College | Dallas, TX
Associate of Science | 2021 – 2023

Experience
Sample Analytics | Software Engineering Intern | Jun 2024 – Aug 2024
Example Labs | Research Assistant | Jan 2025 – Present
Software Engineering Intern | Jun 2023 – Aug 2023
Example Robotics | Remote
""")
    assert [(e["institution"], e["degree"], e["start"], e["end"]) for e in draft["education"]] == [
        ("Example State University", "B.S.", "2023-08", "2027-05"),
        ("Example Community College", "Associate", "2021", "2023"),
    ]
    assert [(e["organization"], e["title"], e["start"]) for e in draft["experience"]] == [
        ("Sample Analytics", "Software Engineering Intern", "2024-06"),
        ("Example Labs", "Research Assistant", "2025-01"),
        ("Example Robotics", "Software Engineering Intern", "2023-06"),  # a two-line heading stays one entry
    ]


# A LaTeX resume in the widely used one-column template: dates and places are set flush right on
# the entry's own lines.
LATEX = [(200, 760, "Jordan Example"), (150, 745, "+1 555 0100 | jordan@example.com | linkedin.com/in/jordan-example"),
         (50, 720, "EDUCATION"),
         (50, 705, "Example State University"), (500, 705, "Austin, TX"),
         (50, 692, "Bachelor of Science in Computer Science"), (470, 692, "Aug. 2023 – May 2027"),
         (50, 670, "EXPERIENCE"),
         (50, 655, "Software Engineering Intern"), (470, 655, "Jun 2024 – Aug 2024"),
         (50, 642, "Sample Analytics"), (500, 642, "Remote"),
         (60, 628, "• Built a Flask REST API that served reporting data to 1,200 internal users"),
         (60, 615, "• Added PostgreSQL indexes that cut report time by 35%"),
         (50, 595, "PROJECTS"),
         (50, 580, "TaskBot | Python, SQLite"), (470, 580, "Nov 2023 – Feb 2024"),
         (60, 566, "• Created a chat bot that tracks team tasks"),
         (50, 545, "TECHNICAL SKILLS"),
         (50, 530, "Languages: Python, SQL, JavaScript"),
         (50, 517, "Developer Tools: Git, Docker")]


def test_dates_and_places_set_flush_right_are_read_apart():
    draft = read(layout_pdf_bytes([LATEX]))
    (school,) = draft["education"]
    assert (school["institution"], school["degree"], school["field"], school["start"], school["end"]) == (
        "Example State University", "Bachelor of Science", "Computer Science", "2023-08", "2027-05")
    (job,) = draft["experience"]
    assert (job["organization"], job["title"], job["location"], job["start"], job["end"]) == (
        "Sample Analytics", "Software Engineering Intern", "Remote", "2024-06", "2024-08")
    (project,) = draft["projects"]
    assert (project["name"], project["start"], project["end"]) == ("TaskBot", "2023-11", "2024-02")


def justified(words: list[str], y: float, gap: float = 6.0) -> list[tuple[float, float, str]]:
    """Words drawn one by one with widened spaces, as a justified line is (Helvetica 10 pt widths)."""
    widths = {"S": 667, "t": 278, "u": 556, "d": 556, "e": 556, "n": 556, "w": 722, "h": 556, "o": 556, "b": 556,
              "i": 222, "l": 222, "s": 500, "a": 556, "c": 500, "k": 500, "r": 333, "v": 500, ".": 278}
    x, items = 50.0, []
    for word in words:
        items.append((x, y, word))
        x += sum(widths[ch] for ch in word) / 100 + gap  # a normal space is 2.78 pt
    return items


def test_a_justified_paragraph_is_not_split():
    lines = [(50, 750, "Jordan Example"), (50, 730, "SUMMARY"),
             *justified("Student who builds backend services.".split(), 715),
             (50, 700, "SKILLS"), (50, 685, "Python")]
    assert read(layout_pdf_bytes([lines]))["summary"] == "Student who builds backend services."


def test_bullet_glyphs_from_word_and_design_tools_are_bullets():
    bullets = ["▪ Built an API", "● Wrote tests", "➢ Added indexes", " Reviewed code", "✓ Shipped a feature",
               "■ Planned sprints", "► Led stand-ups", "o Mentored a student", "> Fixed bugs", "– Wrote docs"]
    draft = draft_profile("Jordan Example\n\nExperience\nSample Analytics | Intern | 2024\n" + "\n".join(bullets))
    (job,) = draft["experience"]
    assert [b["text"] for b in job["bullets"]] == [line.split(" ", 1)[1] for line in bullets]
    pdf = read(layout_pdf_bytes([[(50, 750, "Jordan Example"), (50, 730, "EXPERIENCE"),
                                  (50, 715, "Sample Analytics | Intern | 2024"),
                                  (60, 700, " Reviewed code"), (60, 686, "➢ Added indexes")]]))
    assert [b["text"] for b in pdf["experience"][0]["bullets"]] == ["Reviewed code", "Added indexes"]


def test_a_word_that_starts_like_a_bullet_is_not_one():
    draft = draft_profile("Jordan Example\n\nExperience\nSample Analytics | Intern | 2024\n• Built an API\n"
                          "Oracle Labs | Analyst | 2025\n• Wrote reports\n")
    assert [e["organization"] for e in draft["experience"]] == ["Sample Analytics", "Oracle Labs"]


def test_ligatures_read_as_letters():
    draft = read(layout_pdf_bytes([[(50, 750, "Jordan Example"), (50, 730, "Certiﬁcations"),
                                    (50, 715, "Example Cloud Practitioner"), (50, 695, "Experience"),
                                    (50, 680, "Sample Analytics | Intern | 2024"),
                                    (60, 665, "• Proﬁled slow queries and streamlined ﬂaky tests"),
                                    (60, 651, "• Wrote eﬃcient Python"), (50, 630, "Skills"),
                                    (50, 615, "Conﬂuence, Python")]]))
    assert [c["name"] for c in draft["certifications"]] == ["Example Cloud Practitioner"]
    assert [b["text"] for b in draft["experience"][0]["bullets"]] == [
        "Profiled slow queries and streamlined flaky tests", "Wrote efficient Python"]
    assert [s["name"] for s in draft["skills"]] == ["Confluence", "Python"]


def test_page_numbers_and_running_headers_are_left_out():
    running = (50, 770, "Jordan Example · jordan@example.com · +1 555 0100")
    first = [running, *column(["EXPERIENCE", "Sample Analytics | Software Engineering Intern | Jun 2024 – Aug 2024",
                               "• Built a Flask REST API", "• Added PostgreSQL indexes"], top=740),
             (280, 30, "Page 1 of 2")]
    second = [running, *column(["Example Labs | Research Assistant | Jan 2025 – Present", "• Cleaned survey data",
                                "SKILLS", "Python, SQL"], top=740),
              (300, 30, "2")]
    draft = read(layout_pdf_bytes([first, second]))
    assert (draft["contact"]["name"], draft["contact"]["email"]) == ("Jordan Example", "jordan@example.com")
    assert [(e["organization"], [b["text"] for b in e["bullets"]]) for e in draft["experience"]] == [
        ("Sample Analytics", ["Built a Flask REST API", "Added PostgreSQL indexes"]),
        ("Example Labs", ["Cleaned survey data"]),
    ]
    assert [s["name"] for s in draft["skills"]] == ["Python", "SQL"]
    assert "Page 1 of 2" not in extract_text(layout_pdf_bytes([first, second]))


def test_a_one_page_resume_keeps_every_line():
    lines = column(["Jordan Example", "jordan@example.com", "SKILLS", "Python", "3"])
    assert extract_text(layout_pdf_bytes([lines])).splitlines()[-1] == "3"


def test_skills_lists_keep_brackets_table_categories_and_several_categories_per_line():
    draft = draft_profile("Jordan Example\n\nSkills\nPython (Pandas, NumPy), SQL, JavaScript\n• Docker\n"
                          "Languages | Java, Go\nFrameworks: React, Flask; Tools: Git, Jira\nRust | Kotlin\n")
    assert [(s["name"], s["category"]) for s in draft["skills"]] == [
        ("Python (Pandas, NumPy)", "Skills"), ("SQL", "Skills"), ("JavaScript", "Skills"), ("Docker", "Skills"),
        ("Java", "Languages"), ("Go", "Languages"), ("React", "Frameworks"), ("Flask", "Frameworks"),
        ("Git", "Tools"), ("Jira", "Tools"), ("Rust", "Skills"), ("Kotlin", "Skills"),
    ]


def test_combined_qualified_and_letter_spaced_headings_are_recognized():
    draft = draft_profile("""Jordan Example

W O R K  E X P E R I E N C E
Sample Analytics | Intern | 2024
- Built an API

Skills & Interests
Python, SQL

Education & Certifications
Example State University | B.S. Computer Science | 2027

Research Experience
Example Labs | Research Assistant | 2025
- Cleaned survey data

Projects & Leadership
TaskBot | Creator
- Made a chat bot

Programming Skills
Docker

Leadership & Activities
Captain, Example Robotics Club
""")
    assert [e["organization"] for e in draft["experience"]] == ["Sample Analytics", "Example Labs"]
    assert [s["name"] for s in draft["skills"]] == ["Python", "SQL", "Docker"]
    assert [e["institution"] for e in draft["education"]] == ["Example State University"]
    assert [p["name"] for p in draft["projects"]] == ["TaskBot"]
    assert "Captain" not in str(draft)  # a section the profile doesn't hold stays out


def test_a_list_of_soft_skills_is_not_a_heading():
    draft = draft_profile("Jordan Example\n\nSkills\nPython\nLeadership, Research\nTeamwork and Communication\n")
    assert [s["name"] for s in draft["skills"]] == ["Python", "Leadership", "Research", "Teamwork and Communication"]


def test_word_tables_are_read_where_they_are():
    def build(document):
        contact = document.add_table(rows=1, cols=2)
        contact.cell(0, 0).text = "Jordan Example"
        contact.cell(0, 1).text = "jordan@example.com | Austin, TX"
        document.add_paragraph("Experience")
        row = document.add_table(rows=1, cols=2)
        row.cell(0, 0).text = "Sample Analytics | Software Engineering Intern"
        row.cell(0, 1).text = "Jun 2024 – Aug 2024"
        document.add_paragraph("Built a Flask REST API", style="List Bullet")
        document.add_paragraph("Skills")
        document.add_paragraph("Python, SQL")

    draft = read(word(build))
    assert (draft["contact"]["name"], draft["contact"]["email"]) == ("Jordan Example", "jordan@example.com")
    (job,) = draft["experience"]
    assert (job["organization"], job["start"], [b["text"] for b in job["bullets"]]) == (
        "Sample Analytics", "2024-06", ["Built a Flask REST API"])
    assert [s["name"] for s in draft["skills"]] == ["Python", "SQL"]


def test_a_two_column_word_layout_table_is_read_column_by_column():
    def build(document):
        layout = document.add_table(rows=1, cols=2)
        left, right = layout.cell(0, 0), layout.cell(0, 1)
        left.paragraphs[0].text = "Jordan Example"
        for line in ("jordan@example.com", "Skills", "Python, SQL"):
            left.add_paragraph(line)
        right.paragraphs[0].text = "Experience"
        right.add_paragraph("Sample Analytics | Software Engineering Intern | Jun 2024 – Aug 2024")
        right.add_paragraph("Built a Flask REST API", style="List Bullet")

    draft = read(word(build))
    assert draft["contact"]["email"] == "jordan@example.com"
    assert [s["name"] for s in draft["skills"]] == ["Python", "SQL"]
    assert [(e["organization"], [b["text"] for b in e["bullets"]]) for e in draft["experience"]] == [
        ("Sample Analytics", ["Built a Flask REST API"])]


def test_contact_details_in_a_word_page_header_or_footer_are_read():
    def build(document):
        section = document.sections[0]
        section.header.paragraphs[0].text = "Jordan Example"
        section.header.add_paragraph("jordan@example.com | +1 555 0100")
        section.footer.paragraphs[0].text = "Austin, TX | linkedin.com/in/jordan-example"
        for line in ("Summary", "Student who builds backend services.", "Skills", "Python, SQL"):
            document.add_paragraph(line)

    draft = read(word(build))
    contact = draft["contact"]
    assert (contact["name"], contact["email"], contact["phone"], contact["location"], contact["links"]["linkedin"]) == (
        "Jordan Example", "jordan@example.com", "+1 555 0100", "Austin, TX", "linkedin.com/in/jordan-example")
    assert draft["summary"] == "Student who builds backend services."


def test_a_first_page_header_is_read():
    def build(document):
        section = document.sections[0]
        section.different_first_page_header_footer = True
        section.first_page_header.paragraphs[0].text = "Jordan Example · jordan@example.com"
        section.header.paragraphs[0].text = "Jordan Example · page 2"
        for line in ("Skills", "Python"):
            document.add_paragraph(line)

    assert read(word(build))["contact"]["email"] == "jordan@example.com"


def test_only_a_long_bullet_wraps_and_a_blank_line_ends_it():
    draft = draft_profile("""Jordan Example

Projects
TaskBot | Creator
- Made a chat bot
Budget Tracker
- Tracked monthly spending for a student club in SQLite and
Python with charts
- Wrote the README for new members of the club so they could

Example Game
- Built a small browser game

Hidden Heading Nobody Knows
""")
    assert [(p["name"], [b["text"] for b in p["bullets"]]) for p in draft["projects"]] == [
        ("TaskBot", ["Made a chat bot"]),
        ("Budget Tracker", ["Tracked monthly spending for a student club in SQLite and Python with charts",
                            "Wrote the README for new members of the club so they could"]),
        ("Example Game", ["Built a small browser game"]),
        ("Hidden Heading Nobody Knows", []),
    ]


def test_short_years_seasons_and_apostrophe_years_are_dates():
    draft = draft_profile("""Jordan Example

Experience
Sample Analytics | Intern | Summer 2024
- Built an API
Example Labs | Research Assistant | 2021–22
- Cleaned data
Example Corp | Tutor | Fall 2022 – Spring 2023
- Tutored
Example Inc | Engineer | May '24 – Aug ’24
- Engineered
Example Org | Analyst | Jan. 2023 – Ongoing
- Analysed
""")
    assert [(e["organization"], e["title"], e["start"], e["end"]) for e in draft["experience"]] == [
        ("Sample Analytics", "Intern", "2024", ""),  # a season is no month: only the year is kept
        ("Example Labs", "Research Assistant", "2021", "2022"),
        ("Example Corp", "Tutor", "2022", "2023"),
        ("Example Inc", "Engineer", "2024-05", "2024-08"),
        ("Example Org", "Analyst", "2023-01", "present"),
    ]


def test_honours_coursework_and_a_minor_are_kept_with_their_school():
    draft = draft_profile("""Jordan Example

Education
Example State University — Austin, TX
B.S. in Computer Science, Minor in Mathematics | May 2027
GPA: 3.85/4.00 · Magna Cum Laude
Dean's List (6 semesters)
Relevant Coursework: Data Structures, Algorithms, Databases
Example Community College
Associate of Science, 2023
""")
    first, second = draft["education"]
    assert (first["institution"], first["degree"], first["field"], first["end"], first["gpa"]) == (
        "Example State University", "B.S.", "Computer Science", "2027-05", "3.85")
    assert [b["text"] for b in first["bullets"]] == [
        "Minor in Mathematics", "Magna Cum Laude", "Dean's List (6 semesters)",
        "Relevant Coursework: Data Structures, Algorithms, Databases"]
    assert (second["institution"], second["degree"], second["end"], second["bullets"]) == (
        "Example Community College", "Associate", "2023", [])


def two_columns(left: list[str], right: list[str], interleaved: bool) -> bytes:
    """A two-column page: a sidebar at the left, the main column at the right, drawn column after column or
    line by line across both columns, as different resume builders do."""
    items = column(left, x=40) + column(right, x=260)
    if interleaved:
        items.sort(key=lambda item: (-item[1], item[0]))
    return layout_pdf_bytes([items])


SIDEBAR = ["Jordan Example", "jordan@example.com", "Austin, TX", "SKILLS", "Python, SQL", "Docker",
           "EDUCATION", "Example State University", "B.S. in Computer Science", "May 2027"]
MAIN = ["EXPERIENCE", "Sample Analytics | Software Engineering Intern", "Jun 2024 – Aug 2024",
        "• Built a Flask REST API", "• Added PostgreSQL indexes", "PROJECTS", "TaskBot | Creator",
        "• Created a chat bot"]


def test_a_two_column_pdf_is_read_column_by_column_however_it_is_drawn():
    for interleaved in (False, True):
        draft = read(two_columns(SIDEBAR, MAIN, interleaved))
        assert (draft["contact"]["name"], draft["contact"]["email"], draft["contact"]["location"]) == (
            "Jordan Example", "jordan@example.com", "Austin, TX"), interleaved
        assert [s["name"] for s in draft["skills"]] == ["Python", "SQL", "Docker"]
        assert [(e["institution"], e["end"]) for e in draft["education"]] == [("Example State University", "2027-05")]
        assert [(e["organization"], e["start"], len(e["bullets"])) for e in draft["experience"]] == [
            ("Sample Analytics", "2024-06", 2)]
        assert [p["name"] for p in draft["projects"]] == ["TaskBot"]


def text_box(document, lines: list[str]) -> None:
    """A paragraph anchoring a text box, as Word saves one: the box's text in a DrawingML choice and again
    in a VML fallback for older readers."""
    from lxml import etree

    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    content = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    xml = (
        f'<w:r xmlns:w="{w}" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
        'xmlns:v="urn:schemas-microsoft-com:vml">'
        '<mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:anchor><a:graphic><a:graphicData>'
        f"<wps:wsp><wps:txbx><w:txbxContent>{content}</w:txbxContent></wps:txbx></wps:wsp>"
        "</a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>"
        f"<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>{content}</w:txbxContent></v:textbox>"
        "</v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>"
    )
    document.add_paragraph()._p.append(etree.fromstring(xml))


def test_word_text_boxes_are_read_once():
    def build(document):
        text_box(document, ["Jordan Example", "jordan@example.com | Austin, TX", "Skills", "Python, SQL"])
        for line in ("Experience", "Sample Analytics | Software Engineering Intern | Jun 2024 – Aug 2024"):
            document.add_paragraph(line)
        document.add_paragraph("Built a Flask REST API", style="List Bullet")

    data = word(build)
    assert extract_text(data).count("jordan@example.com") == 1  # the fallback copy is not read again
    draft = read(data)
    assert (draft["contact"]["name"], draft["contact"]["email"]) == ("Jordan Example", "jordan@example.com")
    assert [s["name"] for s in draft["skills"]] == ["Python", "SQL"]
    assert [e["organization"] for e in draft["experience"]] == ["Sample Analytics"]


# ---------------------------------------------------------------- the import's rules, over the whole corpus

def corpus() -> dict[str, bytes]:
    """Every layout above as a file, for the rules every import must keep."""
    def word_resume(document):
        document.sections[0].header.paragraphs[0].text = "Jordan Example · jordan@example.com"
        text_box(document, ["Skills", "Python, SQL"])
        contact = document.add_table(rows=1, cols=2)
        contact.cell(0, 0).text = "Work authorization: US citizen"
        contact.cell(0, 1).text = "Austin, TX"
        for line in ("Experience", "Sample Analytics | Software Engineering Intern | Jun 2024 – Aug 2024"):
            document.add_paragraph(line)
        document.add_paragraph("Built a Flask REST API", style="List Bullet")

    return {
        "latex.pdf": layout_pdf_bytes([LATEX]),
        "two-columns.pdf": two_columns(SIDEBAR, MAIN, interleaved=False),
        "two-columns-interleaved.pdf": two_columns(SIDEBAR, MAIN, interleaved=True),
        "two-pages.pdf": layout_pdf_bytes([
            [(50, 770, "Jordan Example · jordan@example.com"), *column(MAIN[:5], top=740), (280, 30, "Page 1 of 2")],
            [(50, 770, "Jordan Example · jordan@example.com"), *column(MAIN[5:], top=740), (280, 30, "Page 2 of 2")]]),
        "word.docx": word(word_resume),
    }


def test_every_corpus_resume_is_read_locally_and_saved_only_after_review(ai_app_client, fake_ai):
    from app.models import Candidate

    for name, data in corpus().items():
        page = ai_app_client.post("/profile/import", files={"file": (name, data, "application/octet-stream")})
        assert page.status_code == 200, name
        assert f"Read from {name}. Check every field before reviewing." in page.text
        authorization = {k: v for k, v in form_fields(page.text).items() if k.startswith("authorization-")}
        assert not any(authorization.values()), name  # never read from a resume, even "US citizen"
    assert fake_ai.requests == []  # nothing is sent to the AI model
    with ai_app_client.app.state.session_factory() as session:
        assert session.query(Candidate).count() == 0  # nothing is saved before the review step


def test_with_a_saved_profile_no_corpus_resume_changes_a_saved_fact(client, sample_profile):
    import copy

    from tests.test_routes import client_profile, profile_form, review_and_save

    review_and_save(client, profile_form(sample_profile))
    saved = client_profile(client)
    for name, data in corpus().items():
        page = client.post("/profile/import", files={"file": (name, data, "application/octet-stream")})
        assert page.status_code == 200, name
        if "Nothing new was found" in page.text:
            continue
        fields = form_fields(page.text)
        assert (fields["contact-name"], fields["contact-email"]) == (
            saved["contact"]["name"], saved["contact"]["email"]), name
        for index, entry in enumerate(saved["experience"]):
            assert fields[f"experience-{index}-id"] == entry["id"], name
            assert fields[f"experience-{index}-organization"] == entry["organization"], name
        assert client_profile(client) == copy.deepcopy(saved)  # still unchanged until reviewed and saved


def test_a_projects_tech_stack_is_read_as_its_technologies():
    draft = draft_profile("Jordan Example\n\nProjects\nTaskBot | Python, SQLite, Flask | Nov 2023 – Feb 2024\n"
                          "- Created a chat bot\nBudget Tracker | Team Lead | 2024\n- Led a team of three\n")
    assert [(p["name"], p["role"], p["technologies"]) for p in draft["projects"]] == [
        ("TaskBot", "", ["Python", "SQLite", "Flask"]), ("Budget Tracker", "Team Lead", [])]


def test_a_word_file_without_section_settings_is_read():
    def build(document):
        for line in ("Jordan Example", "jordan@example.com", "Skills", "Python, SQL"):
            document.add_paragraph(line)
        body = document.element.body
        body.remove(body.sectPr)  # optional in a Word file, and some tools leave it out

    draft = read(word(build))
    assert draft["contact"]["email"] == "jordan@example.com" and [s["name"] for s in draft["skills"]] == ["Python", "SQL"]


def test_a_short_bullet_that_wraps_onto_a_capitalized_line_stays_one_bullet():
    draft = draft_profile("""Jordan Example

Experience
Acme Corp | Software Engineer | 2020 - 2022
• Built a REST API using
Flask and PostgreSQL for orders
• Cut costs by 20%
• Led the migration of reports to
Snowflake
University Lab | Research Assistant | 2019
• Assisted with experiments
Example Robotics Club
""")
    first, second, third = draft["experience"]
    assert (first["organization"], [b["text"] for b in first["bullets"]]) == ("Acme Corp", [
        "Built a REST API using Flask and PostgreSQL for orders", "Cut costs by 20%",
        "Led the migration of reports to Snowflake"])
    assert second["organization"] == "University Lab" and len(second["bullets"]) == 1
    assert third["organization"] == "Example Robotics Club"  # a title after a short bullet is a new entry
