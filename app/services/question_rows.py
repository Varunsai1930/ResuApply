"""What the Questions section shows for each question: the resolved answer plus its helpers.

Every rule (resolution, bank eligibility, suggestions) lives in ``services.answers``; this only
gathers the results for one page so the template stays free of logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..models import Application, Candidate, Job
from . import answers as answer_service
from . import profile as profile_service
from . import questions as q_rules

MAX_SUGGESTIONS = 3


@dataclass(frozen=True)
class QuestionRow:
    item: answer_service.ResolvedAnswer
    category_label: str
    suggestions: list[answer_service.BankSuggestion] = field(default_factory=list)
    cited: list[tuple[str, str]] = field(default_factory=list)  # (source ID, text) behind an AI draft or answer
    can_bank: bool = False
    draft_outdated: bool = False  # the pending draft was made before the latest profile edit
    confirmation_outdated: bool = False  # the profile value changed after the user confirmed it
    unsupported: list[str] = field(default_factory=list)  # an accepted AI answer the profile no longer backs
    manual_name_part: bool = False  # a first- or last-name question: typed by the user, never from the profile
    confirmation_token: str = ""  # sent with Confirm / Use the profile value
    draft_token: str = ""  # sent with Accept draft


def _cited(profile_sources: dict, ids: list[str]) -> list[tuple[str, str]]:
    return [(i, profile_sources[i].text) for i in ids if i in profile_sources]


def rows(session: Session, job: Job, application: Application, candidate: Candidate) -> list[QuestionRow]:
    """One row per question, in order, resolved against the current profile."""
    profile_sources = profile_service.sources(candidate.profile)
    result = []
    for item in answer_service.resolve_all(job, application, candidate.profile):
        question, answer, draft = item.question, item.answer, item.draft
        accepted = answer is not None and not answer.skipped
        suggestions = []
        if question.category == q_rules.OPEN and not (answer and answer.skipped):
            suggestions = answer_service.bank_suggestions(session, question.text, MAX_SUGGESTIONS)
            if answer:  # don't offer what is already the answer
                suggestions = [s for s in suggestions if s.entry.answer != answer.text]
        cited = []
        if draft is not None and item.label == answer_service.LABELS["pending"]:
            cited = _cited(profile_sources, draft.sources)
        elif accepted and answer.origin == "ai_draft":
            cited = _cited(profile_sources, answer.sources)
        result.append(QuestionRow(
            item=item,
            category_label=q_rules.CATEGORY_LABELS[question.category],
            suggestions=suggestions,
            cited=cited,
            can_bank=answer_service.bank_block(question, answer) is None,
            draft_outdated=draft is not None and draft.profile_revision != candidate.revision,
            confirmation_outdated=bool(
                answer and answer.confirmed and item.value is not None and answer.text != item.value
            ),
            unsupported=answer_service.answer_problems(job, question, answer, candidate.profile),
            manual_name_part=q_rules.name_part(question.factual_key or "", question.text) is not None,
            confirmation_token=answer_service.confirmation_token(job, question, item.value, candidate),
            draft_token=answer_service.draft_token(job, question, draft) if draft is not None else "",
        ))
    return result


def draft_count(job: Job, application: Application) -> int:
    """How many open questions could be drafted."""
    return len(answer_service.draft_targets(job, application))
