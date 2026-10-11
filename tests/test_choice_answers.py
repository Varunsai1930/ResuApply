"""Answers to questions imported from an employer's choice field ("Choose one", "Choose any").

The answer must name the form's choices, or it can't be entered there: it stops approval, and
such a question is never drafted by the AI model. Fictional content only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.schemas.package import Answer, Question
from app.schemas.posting_import import PostingImport, QuestionField
from app.services import answers, package
from app.services import profile as profile_service
from app.services import questions as q_rules

NOW = datetime(2026, 10, 11, tzinfo=UTC)
TOOLS = QuestionField(field_type="multi_select", options=["Git", "Docker", "Python, SQL"])
SPONSORSHIP = QuestionField(field_type="single_select", options=["Yes", "No"])


@pytest.mark.parametrize("field, text, accepted", [
    (SPONSORSHIP, "No", True),
    (SPONSORSHIP, "  no ", True),  # case and spacing aside
    (SPONSORSHIP, "No, I won't need it.", False),
    (SPONSORSHIP, "Yes\nNo", False),  # "Choose one" takes one
    (TOOLS, "Git", True),
    (TOOLS, "Git\nDocker", True),
    (TOOLS, "Git; Docker", True),
    (TOOLS, "Git, Docker", True),
    (TOOLS, "Python, SQL", True),  # a choice that holds a comma
    (TOOLS, "Git, Kubernetes", False),
    (TOOLS, "I have used Git and Docker daily.", False),
    (QuestionField(field_type="long_text"), "Anything at all", True),  # not a choice field
    (QuestionField(field_type="single_select"), "Anything", True),  # no choices were listed
])
def test_a_choice_field_accepts_only_its_choices(field, text, accepted):
    assert field.accepts(text) is accepted


def _question(qid: str, text: str, category: str, required: bool = True, key: str | None = None) -> Question:
    return Question(id=qid, text=text, required=required, category=category, detected_category=category,
                    factual_key=key, added_at=NOW)


def _job(*questions: Question, fields: dict[str, QuestionField]) -> SimpleNamespace:
    return SimpleNamespace(questions=list(questions), posting_import=PostingImport(
        board="examplerobotics", job_id="4012345", fetched_at=NOW, fields=fields))


def _application(*answers_: Answer) -> SimpleNamespace:
    return SimpleNamespace(answers=list(answers_), answer_drafts=[])


def test_an_answer_that_is_not_a_choice_is_unresolved_and_blocks_approval_even_when_optional(sample_profile):
    profile = profile_service.normalize(sample_profile).profile
    tools = _question("q1", "Which of these tools have you used?", q_rules.OPEN, required=False)
    job = _job(tools, fields={"q1": TOOLS})
    prose = Answer(question_id="q1", text="I have used Git and Docker daily.", origin="ai_draft", sources=["summary"], at=NOW)
    [item] = answers.resolve_all(job, _application(prose), profile)
    assert not item.resolved and item.label == "Not one of the form's choices"
    assert "Git · Docker · Python, SQL" in item.not_a_choice and "or skip this optional question" in item.not_a_choice
    blocker = package.answer_blocker(item)
    assert blocker.startswith('"Which of these tools have you used?" (Not one of the form\'s choices): This answer')

    chosen = Answer(question_id="q1", text="Git\nDocker", origin="user", at=NOW)
    [item] = answers.resolve_all(job, _application(chosen), profile)
    assert item.resolved and item.not_a_choice is None and package.answer_blocker(item) is None


def test_a_profile_value_that_is_not_a_choice_is_left_to_the_user(sample_profile):
    profile = profile_service.normalize(sample_profile).profile
    year = QuestionField(field_type="single_select", options=["2024", "2025", "2026"])
    email = _question("q1", "Email", q_rules.FACTUAL, key="email")
    [item] = answers.resolve_all(_job(email, fields={"q1": year}), _application(), profile)
    assert item.value and not item.resolved and item.not_a_choice


def test_a_skipped_choice_question_and_a_job_without_an_import_are_unchanged(sample_profile):
    profile = profile_service.normalize(sample_profile).profile
    tools = _question("q1", "Which of these tools have you used?", q_rules.OPEN, required=False)
    skipped = Answer(question_id="q1", origin="user", skipped=True, at=NOW)
    [item] = answers.resolve_all(_job(tools, fields={"q1": TOOLS}), _application(skipped), profile)
    assert item.resolved and item.skipped
    typed = Answer(question_id="q1", text="Anything", origin="user", at=NOW)
    manual = SimpleNamespace(questions=[tools], posting_import=None)
    [item] = answers.resolve_all(manual, _application(typed), profile)
    assert item.resolved


def test_a_choice_question_is_never_drafted_by_the_ai_model(sample_profile):
    profile = profile_service.normalize(sample_profile).profile
    tools = _question("q1", "Which of these tools have you used?", q_rules.OPEN)
    why = _question("q2", "Why do you want to work here?", q_rules.OPEN)
    job = _job(tools, why, fields={"q1": TOOLS, "q2": QuestionField(field_type="long_text")})
    assert [q.id for q in answers.draft_targets(job, _application(), profile)] == ["q2"]
