"""How a proposed resume sentence differs from the profile lines it cites (services/wording.py)."""

from __future__ import annotations

from app.services.wording import compare
from tests.conftest import tool_response
from tests.test_resume_routes import approval, draft, manual_proposal, setup

BULLET = "Built a Flask REST API in Python that served reporting data to 1,200 internal users"
ENTRY = "Software Engineering Intern at Sample Analytics. Technologies: Python, Flask, PostgreSQL"


def marked(segments):
    return [s.text for s in segments if s.marked]


def test_added_and_removed_words_are_marked_against_the_closest_line():
    c = compare("Designed and built a Flask REST API in Python serving 1,200 internal users",
                [{"id": "exp-1", "text": ENTRY}, {"id": "exp-1-b1", "text": BULLET}])
    base = next(s for s in c.sources if s.base)
    assert base.id == "exp-1-b1"  # the bullet is closer than the entry, whatever the order cited
    assert marked(c.proposed) == ["Designed and", "serving"]
    assert marked(base.segments) == ["that served reporting data to"]
    assert (c.added, c.removed, c.unchanged) == (3, 5, False)
    # Every character is kept, in order, on both sides.
    assert "".join(s.text for s in c.proposed) == "Designed and built a Flask REST API in Python serving 1,200 internal users"
    assert "".join(s.text for s in base.segments) == BULLET


def test_other_cited_lines_highlight_where_added_words_come_from():
    c = compare(BULLET + ", backed by PostgreSQL", [{"id": "exp-1-b1", "text": BULLET}, {"id": "exp-1", "text": ENTRY}])
    assert marked(c.proposed) == [", backed by PostgreSQL"]
    entry = next(s for s in c.sources if s.id == "exp-1")
    assert marked(entry.segments) == ["PostgreSQL"]  # "by" is too common to say where it came from


def test_an_unchanged_sentence_says_so():
    c = compare(BULLET, [{"id": "exp-1-b1", "text": BULLET}])
    assert c.unchanged and marked(c.proposed) == [] and marked(c.sources[0].segments) == []


def test_case_alone_is_not_a_change_but_numbers_and_punctuation_are():
    c = compare("built a flask REST API", [{"id": "b", "text": "Built a Flask REST API"}])
    assert c.unchanged
    c = compare("Served 1,500 users.", [{"id": "b", "text": "Served 1,200 users"}])
    assert marked(c.proposed) == ["500", "."] and marked(c.sources[0].segments) == ["200"]


def test_words_with_inner_marks_stay_whole():
    c = compare("Wrote C++ and Node.js services", [{"id": "b", "text": "Wrote C# and Node.js services"}])
    assert marked(c.proposed) == ["C++"] and marked(c.sources[0].segments) == ["C#"]


def test_no_cited_text_shows_the_sentence_unmarked():
    c = compare("A sentence", [])
    assert c.sources == [] and marked(c.proposed) == [] and not c.unchanged


# ---------------------------------------------------------------- on the review page

def test_the_review_page_shows_the_reworded_words(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = setup(client, sample_profile)
    approval(client)
    proposal = draft(client)
    bullet = proposal["experience"][0]["bullets"][0]
    bullet["text"] = "Designed and built a Flask REST API in Python that served reporting data to 1,200 internal users"
    fake_ai.push(tool_response("return_resume", proposal))
    response = client.post(f"/jobs/{job_id}/resume/generate", follow_redirects=False)
    assert response.headers["location"].endswith("/resume/review"), response.text
    page = client.get(response.headers["location"]).text
    assert "<ins>Designed and</ins> built a Flask REST API" in page
    assert "2 words added, 0 removed." in page
    assert "Unchanged from your profile." in page  # the other bullets kept their wording
    assert "<ins>underlined words</ins> were added" in page


def test_profile_text_in_the_comparison_is_escaped(client, sample_profile):
    sample_profile["experience"][0]["bullets"][0] = "Built <script>alert(1)</script> dashboards"
    job_id = setup(client, sample_profile)
    page = manual_proposal(client, job_id).text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt; dashboards" in page
