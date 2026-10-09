"""Application questions, answers, AI answer drafts and the answer bank.

Ported from ResuSkill's ``resuskill_core.package`` (questions, answers, resolution) and
``validate`` (answer checks), adapted to the database:

- Questions belong to the job and come from the employer's form. IDs (``q1``, ``q2`` ...) come
  from ``job.question_counter`` and are never reused.
- Answers and AI drafts belong to the application. A draft is not an answer until the user
  accepts it, and it is re-validated against the *current* profile at that moment.
- Any change to package content (questions, answers, categories) clears approval and returns
  the application to Draft. Storing drafts does not: they are proposals, not package content.
- An accepted AI answer the current profile no longer supports can be drafted again. The
  replacement waits beside the accepted answer until the user accepts or discards it.
- A factual value over the question's length limit is shown but never shortened: it stays
  unresolved until the user writes their own answer or skips an optional question.
- Sensitive answers are never saved to the answer bank. That is a hard rule, not a preference.

Every change runs in ``transactions.write``: it holds the database writer lock and works on the
reloaded rows, so simultaneous requests can't undo each other's questions or answers. Confirming
a value and accepting a draft also need the token of what the user reviewed (``confirmation_token``,
``draft_token``); a page that no longer matches is refused with ``ReviewChanged``.

JSON columns are treated as immutable: every mutation assigns a new list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Application, AnswerBankEntry, Candidate, Job
from ..schemas.package import Answer, AnswerDraft, Question
from ..schemas.profile import Profile
from ..schemas.tracking import ReviewState
from . import profile as profile_service
from . import questions as q_rules
from . import transactions
from .claims import claim_problems, detection_terms
from .text import content_hash, norm_text

QUESTION_LIMIT = 2000  # characters of question text
ANSWER_LIMIT = 10000  # characters of answer text, whatever the employer's own limit
LIMIT_UNITS = ("chars", "words")
USER_ORIGINS = ("user", "bank")

LABELS = {
    "profile": "From profile",
    "ai_draft": "AI draft",
    "user": "User answer",
    "missing": "Missing information",
    "input": "User input required",
    "pending": "AI draft (pending review)",
    "skipped": "Skipped (optional)",
    "category": "Confirm category",
    "limit": "Over the length limit",
}


class AnswerError(Exception):
    """A user-facing error. Nothing was changed."""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = details or []


class ReviewChanged(AnswerError):
    """What the user reviewed is no longer what is stored, or the page sent no token. Nothing was changed."""


# ---------------------------------------------------------------- review tokens

def _question_data(job: Job, question: Question) -> dict:
    return {"job": job.id, "question": question.model_dump(mode="json")}


def confirmation_token(job: Job, question: Question, value: str | None, candidate: Candidate) -> str:
    """What a confirm button vouches for: this question, the value shown and the profile revision it came from."""
    return content_hash(_question_data(job, question) | {
        "value": value, "candidate": candidate.id, "profile_revision": candidate.revision,
    })


def draft_token(job: Job, question: Question, draft: AnswerDraft) -> str:
    """What an accept button vouches for: this question and the complete draft shown."""
    return content_hash(_question_data(job, question) | {"draft": draft.model_dump(mode="json")})


# ---------------------------------------------------------------- length and validation

def check_length(text: str, limit: int | None, unit: str) -> str | None:
    """A message when the text is over the employer's limit, else None."""
    if not limit:
        return None
    size = len(text.split()) if unit == "words" else len(text)
    if size > limit:
        return f"{size} {unit} exceeds the limit of {limit}"
    return None


def answer_problems(job: Job, question: Question, answer: Answer | None, profile: Profile | dict) -> list[str]:
    """Why an accepted AI-drafted answer no longer holds against the current profile; empty when it does.

    Drafts are validated when accepted, but the profile can change afterwards (a cited bullet
    edited or removed). Like the accepted resume, such an answer must be checked again before
    the package is approved. User-written and profile-filled answers are the user's own words.
    """
    if answer is None or answer.skipped or answer.origin != "ai_draft":
        return []
    return validate_answer(profile, job, answer.text, answer.sources, question.limit, question.limit_unit)


def validate_answer(profile: Profile | dict, job: Job | dict, text: str, cited: list[str],
                    limit: int | None, unit: str) -> list[str]:
    """Problems with an AI-drafted answer against the canonical profile; empty when it passes."""
    prof = profile.model_dump() if isinstance(profile, Profile) else profile
    sources = profile_service.sources(prof)
    problems = []
    if not text.strip():
        problems.append("empty answer")
    if not cited:
        problems.append("an answer must cite at least one source")
    bad = [s for s in cited if s not in sources]
    if bad:
        problems.append(f"unknown sources {', '.join(bad)}")
    else:
        problems.extend(claim_problems(
            text,
            [sources[s].text for s in cited] + [prof.get("summary") or ""],
            profile_service.skill_keys(prof),
            detection_terms(prof, job),
        ))
    length = check_length(text, limit, unit)
    if length:
        problems.append(length)
    return problems


# ---------------------------------------------------------------- helpers

def _find(job: Job, qid: str) -> Question:
    for question in job.questions:
        if question.id == qid:
            return question
    raise AnswerError(f"This job has no question {qid!r}.")


def _answer_for(application: Application, qid: str) -> Answer | None:
    return next((a for a in application.answers if a.question_id == qid), None)


def _draft_for(application: Application, qid: str) -> AnswerDraft | None:
    return next((d for d in application.answer_drafts if d.question_id == qid), None)


def missing_value_message(question: Question) -> str:
    """Why a factual question has no profile value, and what to do instead."""
    if q_rules.name_part(question.factual_key or "", question.text):
        return ("Your profile stores your full name only, and ResuApply never splits it. "
                "Type this part of your name yourself.")
    return "Your profile has no value for this question. Update the profile or answer it yourself."


def too_long_message(question: Question, problem: str) -> str:
    """Why a factual value can't be used as it is, and what to do instead. It is never shortened for the user."""
    instead = "write a shorter answer yourself" + ("" if question.required else ", or skip this optional question")
    return f"The profile value is too long for this question: {problem}. Nothing is shortened for you; {instead}."


def _content_changed(application: Application) -> None:
    """Package content changed: approval no longer applies and the package is a Draft again."""
    application.approval = None
    application.review_state = ReviewState.DRAFT.value
    application.updated_at = utcnow()


def _drop(application: Application, qid: str) -> None:
    application.answers = [a for a in application.answers if a.question_id != qid]
    application.answer_drafts = [d for d in application.answer_drafts if d.question_id != qid]


def _replace_answer(application: Application, answer: Answer) -> None:
    application.answers = [*(a for a in application.answers if a.question_id != answer.question_id), answer]
    application.answer_drafts = [d for d in application.answer_drafts if d.question_id != answer.question_id]
    _content_changed(application)


def _check_category_change(detected: str, key: str | None, category: str) -> None:
    """Refuse category changes that would let the app answer a question it must not."""
    if category not in q_rules.CATEGORIES:
        raise AnswerError(f"Category must be one of {', '.join(q_rules.CATEGORIES)}.")
    if detected in q_rules.SENSITIVE_CATEGORIES and category not in (q_rules.SENSITIVE, detected):
        raise AnswerError(
            f"This question was detected as {q_rules.CATEGORY_LABELS[detected].lower()}; it cannot become "
            f"{q_rules.CATEGORY_LABELS[category].lower()}. You answer it yourself, or skip it if it is optional.",
        )
    if category in (q_rules.FACTUAL, q_rules.SENSITIVE_FACTUAL) and not key:
        raise AnswerError("No profile field matches this question; choose Open-ended or Sensitive instead.")


# ---------------------------------------------------------------- questions

def add_question(session: Session, job: Job, text: str, required: bool = True, limit: int | None = None,
                 limit_unit: str = "chars", category: str | None = None) -> Question:
    """Add one of the employer's questions. Its category is detected unless the user overrides it."""
    text = (text or "").strip()
    if not text:
        raise AnswerError("Enter the question text.")
    if len(text) > QUESTION_LIMIT:
        raise AnswerError(f"Keep the question under {QUESTION_LIMIT:,} characters.")
    if limit_unit not in LIMIT_UNITS:
        raise AnswerError("The limit unit must be characters or words.")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise AnswerError("The length limit must be a whole number of at least 1, or empty for no limit.")
    detected, key = q_rules.classify(text)
    chosen = detected
    if category and category != detected:
        _check_category_change(detected, key, category)
        chosen = category
    with transactions.write(session, job):
        # The number comes from the reloaded counter and list, so simultaneous additions get distinct IDs.
        existing = [int(q.id[1:]) for q in job.questions if q.id[1:].isdigit()]
        number = max(job.question_counter, *existing, 0) + 1
        question = Question(
            id=f"q{number}", text=text, required=bool(required), limit=limit, limit_unit=limit_unit,
            category=chosen, detected_category=detected,
            factual_key=key if chosen in (q_rules.FACTUAL, q_rules.SENSITIVE_FACTUAL) else None,
            added_at=utcnow(),
        )
        job.question_counter = number
        job.questions = [*job.questions, question]
        _content_changed(job.application)
    return question


def set_category(session: Session, job: Job, application: Application, qid: str, category: str) -> Question:
    """Change a question's category. Its answer and draft are dropped: they were made for the old one."""
    if category == q_rules.UNKNOWN:
        raise AnswerError("Choose a category; Unrecognized is only what the rules detect.")
    with transactions.write(session, job):
        question = _find(job, qid)
        _, key = q_rules.classify(question.text)
        _check_category_change(question.detected_category, key, category)
        if category == question.category:
            return question
        updated = question.model_copy(update={
            "category": category,
            "factual_key": key if category in (q_rules.FACTUAL, q_rules.SENSITIVE_FACTUAL) else None,
        })
        job.questions = [updated if q.id == qid else q for q in job.questions]
        _drop(application, qid)
        _content_changed(application)
    return updated


def remove_question(session: Session, job: Job, application: Application, qid: str) -> None:
    """Remove a question with its answer and draft. Its ID is never issued again."""
    with transactions.write(session, job):
        _find(job, qid)
        job.questions = [q for q in job.questions if q.id != qid]
        _drop(application, qid)
        _content_changed(application)


# ---------------------------------------------------------------- answers

def set_answer(session: Session, job: Job, application: Application, qid: str, text: str,
               origin: str = "user", bank_id: int | None = None) -> Answer:
    """Store the user's own answer. ``origin="bank"`` marks one that started from an answer-bank entry."""
    with transactions.write(session, job):
        question = _find(job, qid)
        if origin not in USER_ORIGINS:
            raise AnswerError("An answer you write is stored as yours or as one started from the answer bank.")
        if question.category == q_rules.UNKNOWN:
            raise AnswerError("Confirm the question's category before answering it.")
        text = (text or "").strip()
        if not text:
            raise AnswerError("The answer is empty. Skip the question instead if it is optional.")
        if len(text) > ANSWER_LIMIT:
            raise AnswerError(f"Keep the answer under {ANSWER_LIMIT:,} characters.")
        problem = check_length(text, question.limit, question.limit_unit)
        if problem:
            raise AnswerError(f"{qid}: {problem}.")
        if origin == "bank":
            if question.category in q_rules.SENSITIVE_CATEGORIES:
                raise AnswerError("Sensitive questions are never answered from the answer bank.")
            if bank_id is None or session.get(AnswerBankEntry, bank_id) is None:
                raise AnswerError("That answer bank entry no longer exists.")
        else:
            bank_id = None
        answer = Answer(question_id=qid, text=text, origin=origin, bank_id=bank_id, at=utcnow())
        _replace_answer(application, answer)
    return answer


def skip_answer(session: Session, job: Job, application: Application, qid: str) -> Answer:
    """Explicitly skip an optional question."""
    with transactions.write(session, job):
        question = _find(job, qid)
        if question.required:
            raise AnswerError(f"{qid} is required and can't be skipped.")
        answer = Answer(question_id=qid, text="", origin="user", skipped=True, at=utcnow())
        _replace_answer(application, answer)
    return answer


def confirm_answer(session: Session, job: Job, application: Application, qid: str, candidate: Candidate,
                   token: str) -> Answer:
    """Confirm the profile value the user was shown for a factual or sensitive-factual question.

    ``token`` is ``confirmation_token`` as the page showed it. The value is read again from the
    reloaded profile; if it, the question or the profile revision changed, nothing is confirmed.
    """
    with transactions.write(session, job, candidate):
        question = _find(job, qid)
        if question.category not in (q_rules.FACTUAL, q_rules.SENSITIVE_FACTUAL):
            raise AnswerError(f"{qid} isn't answered from the profile, so there is nothing to confirm.")
        value = q_rules.factual_value(question.factual_key or "", question.text, candidate.profile)
        if value is None:
            raise AnswerError(missing_value_message(question))
        problem = check_length(value, question.limit, question.limit_unit)
        if problem:
            raise AnswerError(too_long_message(question, problem))
        if token != confirmation_token(job, question, value, candidate):
            raise ReviewChanged("This question or your profile changed since you reviewed it, so nothing was "
                                "confirmed. Check the value shown now and confirm it again.")
        answer = Answer(question_id=qid, text=value, origin="profile", confirmed=True, at=utcnow())
        _replace_answer(application, answer)
    return answer


def accept_draft(session: Session, job: Job, application: Application, qid: str, candidate: Candidate,
                 token: str) -> Answer:
    """Turn the AI draft the user reviewed into the answer, after checking it against the current profile and limit.

    ``token`` is ``draft_token`` as the page showed it. The draft may be older than the latest
    profile edit; it is accepted only if its claims still hold against the reloaded profile.
    Accepting a replacement for an unsupported AI answer swaps the answer.
    """
    with transactions.write(session, job, candidate):
        question = _find(job, qid)
        draft = _draft_for(application, qid)
        if draft is None:
            raise ReviewChanged(f"{qid} has no draft to accept. Review the question as it is now.")
        if question.category != q_rules.OPEN:
            raise AnswerError("Only open-ended questions take AI drafts.")
        if token != draft_token(job, question, draft):
            raise ReviewChanged("This draft or its question changed since you reviewed it, so it was not accepted. "
                                "Read the draft shown now and accept it again.")
        problems = validate_answer(candidate.profile, job, draft.text, draft.sources, question.limit,
                                   question.limit_unit)
        if problems:
            raise AnswerError(
                "This draft no longer passes validation against your current profile, so it was not accepted.",
                problems,
            )
        answer = Answer(question_id=qid, text=draft.text, origin="ai_draft", sources=list(draft.sources), at=utcnow())
        _replace_answer(application, answer)
    return answer


def discard_draft(session: Session, job: Job, application: Application, qid: str) -> None:
    """Throw away a pending draft. Drafts are not package content, so approval and the answer are untouched."""
    with transactions.write(session, job):
        _find(job, qid)
        if _draft_for(application, qid) is None:
            raise AnswerError(f"{qid} has no draft to discard.")
        application.answer_drafts = [d for d in application.answer_drafts if d.question_id != qid]
        application.updated_at = utcnow()


def replace_drafts(application: Application, drafts: list[AnswerDraft]) -> None:
    """Store new drafts, replacing only those for the same questions. The caller commits.

    Drafts are proposals, so this deliberately leaves approval alone.
    """
    replaced = {d.question_id for d in drafts}
    application.answer_drafts = [*(d for d in application.answer_drafts if d.question_id not in replaced), *drafts]
    application.updated_at = utcnow()


# ---------------------------------------------------------------- resolution

@dataclass(frozen=True)
class ResolvedAnswer:
    """What a question currently shows and whether it counts as resolved for approval."""

    question: Question
    text: str
    label: str
    resolved: bool
    origin: str | None  # "profile", "ai_draft", "user", "bank"; None while nothing is accepted
    skipped: bool = False
    value: str | None = None  # the current profile value, for factual and sensitive-factual questions
    answer: Answer | None = None
    draft: AnswerDraft | None = None  # a pending AI draft, or a replacement beside an accepted answer
    problem: str | None = None  # why the shown profile value can't be used as it is (over the length limit)


def resolve_answer(question: Question, answer: Answer | None, draft: AnswerDraft | None,
                   profile: Profile) -> ResolvedAnswer:
    """The current text and label for a question, and whether it is resolved."""
    category = question.category

    def result(text: str, label: str, resolved: bool, origin: str | None = None, **extra) -> ResolvedAnswer:
        return ResolvedAnswer(question, text, label, resolved, origin, answer=answer, draft=draft, **extra)

    if answer and answer.skipped:
        return result("", LABELS["skipped"], True, answer.origin, skipped=True)
    if category == q_rules.UNKNOWN:
        return result("", LABELS["category"], False)
    if category in (q_rules.FACTUAL, q_rules.SENSITIVE_FACTUAL):
        value = q_rules.factual_value(question.factual_key or "", question.text, profile)
        if answer and answer.origin in USER_ORIGINS:
            return result(answer.text, LABELS["user"], True, answer.origin, value=value)
        problem = check_length(value, question.limit, question.limit_unit) if value else None
        if problem:  # shown as it is, never shortened; even a confirmation of it doesn't resolve it
            return result(value, LABELS["limit"], False, value=value, problem=problem)
        if category == q_rules.SENSITIVE_FACTUAL:
            # Confirmation covers the value that was shown; a changed profile value needs a new one.
            if answer and answer.confirmed and answer.text == value:
                return result(value, LABELS["profile"] + " (confirmed)", True, "profile", value=value)
            return result(value or "", LABELS["input"], False, value=value)
        if value is None:
            return result("", LABELS["missing"], False)
        return result(value, LABELS["profile"], True, "profile", value=value)
    if answer:
        return result(answer.text, LABELS["user" if answer.origin == "bank" else answer.origin], True, answer.origin)
    if category == q_rules.SENSITIVE:
        return result("", LABELS["input"], False)
    if draft:
        return result(draft.text, LABELS["pending"], False)
    return result("", LABELS["missing"], False)


def resolve_all(job: Job, application: Application, profile: Profile) -> list[ResolvedAnswer]:
    """Every question of the job, resolved against the current profile, in order."""
    answers = {a.question_id: a for a in application.answers}
    drafts = {d.question_id: d for d in application.answer_drafts}
    return [resolve_answer(q, answers.get(q.id), drafts.get(q.id), profile) for q in job.questions]


def draft_targets(job: Job, application: Application, profile: Profile | dict) -> list[Question]:
    """The open questions worth drafting: those without an answer, and those whose accepted AI
    answer the current profile no longer supports.

    Skipped questions, the user's own answers (typed or from the bank) and AI answers the
    profile still supports are never drafted. The draft button, the request and the freshness
    checks all use this one rule.
    """
    answers = {a.question_id: a for a in application.answers}
    return [
        q for q in job.questions
        if q.category == q_rules.OPEN
        and (q.id not in answers or answer_problems(job, q, answers[q.id], profile))
    ]


# ---------------------------------------------------------------- answer bank

STOPWORDS = frozenset("""
a an and are as at be by can could do does for from have has how i if in is it its me my of on or our
please so than that the their them then there these they this those to us was we were what when where
which who whom why will with would you your
""".split())
SIMILARITY_THRESHOLD = 0.3


def bank_block(question: Question, answer: Answer | None) -> str | None:
    """Why this question's answer can't be saved to the answer bank, or None when it can."""
    if (question.category in q_rules.SENSITIVE_CATEGORIES
            or question.detected_category in q_rules.SENSITIVE_CATEGORIES):
        return "Answers to sensitive questions are never saved to the answer bank."
    if answer is None or answer.skipped or not answer.text.strip():
        return f"{question.id} has no accepted answer to save."
    if answer.origin == "profile":
        return "Profile values are filled in from your profile every time; they aren't saved to the bank."
    return None


def save_to_bank(session: Session, job: Job, application: Application, qid: str) -> AnswerBankEntry:
    """Save an accepted answer to a non-sensitive question for reuse. Sensitive answers are refused."""
    with transactions.write(session, job):
        question = _find(job, qid)
        answer = _answer_for(application, qid)
        blocked = bank_block(question, answer)
        if blocked:
            raise AnswerError(blocked)
        now = utcnow()
        for entry in bank_entries(session):
            if (norm_text(entry.question) == norm_text(question.text)
                    and norm_text(entry.answer) == norm_text(answer.text)):
                entry.updated_at = now
                return entry
        entry = AnswerBankEntry(
            question=question.text, answer=answer.text, category=question.category, sources=list(answer.sources),
            source_job_id=job.id, created_at=now, updated_at=now,
        )
        session.add(entry)
    return entry


def bank_entries(session: Session) -> list[AnswerBankEntry]:
    """Every saved entry, most recently saved or reused first."""
    return list(session.scalars(
        select(AnswerBankEntry).order_by(AnswerBankEntry.updated_at.desc(), AnswerBankEntry.id.desc())
    ))


def delete_bank_entry(session: Session, entry_id: int) -> None:
    entry = session.get(AnswerBankEntry, entry_id)
    if entry is None:
        raise AnswerError("That answer bank entry no longer exists.")
    session.delete(entry)
    session.commit()


@dataclass(frozen=True)
class BankSuggestion:
    entry: AnswerBankEntry
    score: float


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", norm_text(text)) if w not in STOPWORDS and len(w) > 1}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of the lowercase content words of two questions (0 to 1)."""
    words_a, words_b = _content_words(a), _content_words(b)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def bank_suggestions(session: Session, question_text: str, limit: int = 3) -> list[BankSuggestion]:
    """Saved answers to similar questions, best first. Never offers anything for a sensitive question."""
    if q_rules.classify(question_text)[0] in q_rules.SENSITIVE_CATEGORIES:
        return []
    ranked = []
    for entry in bank_entries(session):
        if entry.category in q_rules.SENSITIVE_CATEGORIES or q_rules.classify(entry.question)[0] in q_rules.SENSITIVE_CATEGORIES:
            continue
        score = similarity(question_text, entry.question)
        if score >= SIMILARITY_THRESHOLD:
            ranked.append(BankSuggestion(entry, score))
    ranked.sort(key=lambda s: s.score, reverse=True)  # stable: equal scores keep newest first
    return ranked[:max(limit, 0)]
