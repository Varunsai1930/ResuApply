"""The four shared status meanings (services/status.py), their icons and the shared header."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from app.schemas.package import Answer, AnswerDraft, Question
from app.schemas.tracking import ReviewState
from app.services import answers, checklist
from app.services import profile as profile_service
from app.services import questions as q_rules
from app.services.status import Meaning, Status, for_answer, for_check, for_review
from app.templating import NAV_ITEMS, templates

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def test_every_status_has_its_label():
    assert [s.label for s in Status] == ["Done", "Needs you", "Blocked", "Info"]


def test_checklist_gaps_and_unknowns_are_info_never_blocked():
    assert for_check(checklist.MET) == Meaning(Status.DONE, "Met")
    assert for_check(checklist.UNMET) == Meaning(Status.INFO, "Gap")
    assert for_check(checklist.UNKNOWN) == Meaning(Status.INFO, "Unknown", "Link evidence")
    # A gap never blocks an application, so no checklist result may read Blocked.
    assert all(for_check(s).status is not Status.BLOCKED for s in checklist.STATUSES)


@pytest.mark.parametrize("state, status", [
    (ReviewState.APPROVED, Status.DONE),
    (ReviewState.DRAFT, Status.NEEDS_YOU),
    (ReviewState.STALE, Status.NEEDS_YOU),
    ("stale", Status.NEEDS_YOU),  # the stored column value
])
def test_review_states(state, status):
    assert for_review(state).status is status


def _question(category: str, required: bool = True, key: str | None = None, limit: int | None = None) -> Question:
    return Question(id="q1", text="A question", required=required, category=category, detected_category=category,
                    factual_key=key, limit=limit, added_at=NOW)


def _answer(**fields) -> Answer:
    return Answer(question_id="q1", at=NOW, **fields)


DRAFT = AnswerDraft(question_id="q1", text="Drafted", sources=["summary"], model="m", prompt_revision="1",
                    profile_revision=1, job_revision=1, created_at=NOW)


ANSWER_CASES = [
    ("profile", _question(q_rules.FACTUAL, key="email"), None, None, Status.DONE),
    ("ai_draft", _question(q_rules.OPEN), _answer(text="Mine", origin="ai_draft", sources=["summary"]), None, Status.DONE),
    ("user", _question(q_rules.OPEN), _answer(text="Mine", origin="user"), None, Status.DONE),
    ("missing", _question(q_rules.OPEN), None, None, Status.NEEDS_YOU),
    ("input", _question(q_rules.SENSITIVE), None, None, Status.NEEDS_YOU),
    ("pending", _question(q_rules.OPEN), None, DRAFT, Status.NEEDS_YOU),
    ("skipped", _question(q_rules.OPEN, required=False), _answer(origin="user", skipped=True), None, Status.INFO),
    ("category", _question(q_rules.UNKNOWN), None, None, Status.NEEDS_YOU),
    ("limit", _question(q_rules.FACTUAL, key="email", limit=3), None, None, Status.NEEDS_YOU),
]


@pytest.mark.parametrize("label, question, answer, draft, status", ANSWER_CASES)
def test_every_answer_label_has_a_meaning(sample_profile, label, question, answer, draft, status):
    item = answers.resolve_answer(question, answer, draft, profile_service.normalize(sample_profile).profile)
    assert item.label == answers.LABELS[label]
    assert for_answer(item) == Meaning(status, item.label)


def test_the_cases_above_cover_every_answer_label():
    covered = {case[0] for case in ANSWER_CASES}
    assert covered == set(answers.LABELS)


def _render(source: str, **context) -> str:
    return templates.env.from_string('{% import "macros.html" as m with context %}' + source).render(**context)


@pytest.mark.parametrize("status, shape", [
    (Status.DONE, "<path d=\"M5 12l5 5 9-10\"/>"),
    (Status.NEEDS_YOU, "<circle cx=\"12\" cy=\"12\" r=\"5\""),
    (Status.BLOCKED, "<rect"),
    (Status.INFO, "<path d=\"M12 11v6\"/>"),
])
def test_each_status_has_its_own_icon_and_says_its_meaning_in_words(status, shape):
    html = _render("{{ m.status_line(meaning) }}", meaning=Meaning(status, "a note"))
    assert f'class="st st-{status.value}"' in html
    assert 'aria-hidden="true"' in html and shape in html
    assert f"{status.label} · a note" in html
    others = {"<path d=\"M5 12l5 5 9-10\"/>", "<circle cx=\"12\" cy=\"12\" r=\"5\"", "<rect", "<path d=\"M12 11v6\"/>"}
    assert not any(other in html for other in others - {shape})


def _nav(html: str) -> str:
    return re.search(r'<nav aria-label="Main">(.*?)</nav>', html, re.S).group(1)


@pytest.mark.parametrize("path, current", [
    ("/jobs", "Jobs"), ("/jobs/new", "Jobs"), ("/answers", "Answers"), ("/profile", "Profile"),
])
def test_every_page_shares_the_same_header_with_its_section_current(client, path, current):
    html = client.get(path).text
    assert html.count('<header class="site-header">') == 1
    nav = _nav(html)
    assert re.findall(r'<a href="([^"]+)"', nav) == [href for _, href, _ in NAV_ITEMS]
    assert re.findall(r'aria-current="page">(\w+)<', nav) == [current]


def test_error_pages_mark_no_section_current(client):
    response = client.get("/jobs/999")
    assert response.status_code == 404
    assert 'aria-current' not in _nav(response.text)
