"""The cover letter: an optional part of the application package.

- The body is an accepted AI draft or the user's own text. An AI draft cites profile sources and
  passes the same claim checks as an AI answer (numbers, technologies and credentials must come
  from what it cites) before the user sees it, again when it is accepted, and again before the
  package is approved. Text the user writes is theirs and is not checked.
- Only the body is stored. The contact header, greeting and sign-off are added from the profile
  and the job whenever the letter is shown or exported, so they can't drift from the facts.
- The accepted letter is package content: saving, replacing or removing it clears approval. A
  draft is a proposal and leaves approval alone. Nothing requires a letter.
- The job's own title and company name are not claims about the candidate, so they are taken
  out before the claim checks: a role called "2026 Data Intern" doesn't trip the number check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Application, Candidate, Job
from ..schemas.package import CoverLetter, CoverLetterDraft
from ..schemas.profile import Profile
from ..schemas.tracking import ReviewState
from . import answers as answer_service
from . import profile as profile_service
from . import transactions
from .claims import detection_terms
from .text import content_hash

WORD_LIMIT = 400  # an AI draft longer than this is refused: with the header and sign-off it fills a page
TEXT_LIMIT = answer_service.ANSWER_LIMIT  # characters of a letter the user writes
CLOSING = "Sincerely,"


class CoverLetterError(Exception):
    """A user-facing refusal. Nothing was changed."""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = details or []


class ReviewChanged(CoverLetterError):
    """The draft is not the one the user reviewed. Nothing was changed."""


# ---------------------------------------------------------------- validation

def _without_role_names(text: str, job: Job | dict) -> str:
    names = [job.title, job.company] if isinstance(job, Job) else [job.get("title", ""), job.get("company", "")]
    for name in sorted((n.strip() for n in names if n and n.strip()), key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    return text


def claim_problems(text: str, sources: dict[str, profile_service.Source], cited: list[str], job: Job | dict,
                   extra_terms=()) -> list[str]:
    """``answers.cited_claim_problems`` for a letter: the job's title and company aren't candidate claims."""
    return answer_service.cited_claim_problems(_without_role_names(text, job), sources, cited, extra_terms)


def validate_letter(profile: Profile | dict, job: Job, text: str, cited: list[str]) -> list[str]:
    """Problems with an AI-drafted letter body against the canonical profile; empty when it passes."""
    prof = profile.model_dump() if isinstance(profile, Profile) else profile
    sources = profile_service.sources(prof)
    problems = []
    if not text.strip():
        problems.append("empty letter")
    if not cited:
        problems.append("a letter must cite at least one source")
    unknown = [s for s in cited if s not in sources]
    if unknown:
        problems.append(f"unknown sources {', '.join(unknown)}")
    else:
        problems.extend(claim_problems(text, sources, cited, job, detection_terms(prof, job)))
    length = answer_service.check_length(text, WORD_LIMIT, "words")
    if length:
        problems.append(length)
    return problems


def letter_problems(job: Job, application: Application, profile: Profile | dict | None) -> list[str]:
    """Why an accepted AI-drafted letter no longer holds against the current profile; empty when it does."""
    letter = application.cover_letter
    if letter is None or letter.origin != "ai_draft" or profile is None:
        return []
    return validate_letter(profile, job, letter.text, letter.sources)


def draft_token(job: Job, draft: CoverLetterDraft) -> str:
    """What an accept button vouches for: this job's details and the complete draft shown."""
    return content_hash({"job": job.id, "revision": job.revision, "title": job.title, "company": job.company,
                         "draft": draft.model_dump(mode="json")})


# ---------------------------------------------------------------- changes

def _content_changed(application: Application) -> None:
    """The letter is package content: approval no longer applies and the package is a Draft again."""
    application.approval = None
    application.review_state = ReviewState.DRAFT.value
    application.updated_at = utcnow()


def store_draft(application: Application, draft: CoverLetterDraft) -> None:
    """Keep a new draft in place of any earlier one. The caller commits; approval is untouched."""
    application.cover_letter_draft = draft
    application.updated_at = utcnow()


def accept_draft(session: Session, job: Job, candidate: Candidate, token: str) -> CoverLetter:
    """Make the reviewed draft the letter, after checking it against the profile as it is now."""
    with transactions.write(session, job, candidate):
        application = job.application
        draft = application.cover_letter_draft
        if draft is None:
            raise ReviewChanged("There is no cover letter draft to accept. Review the letter as it is now.")
        if token != draft_token(job, draft):
            raise ReviewChanged("The draft or the job changed since you reviewed it, so it was not accepted. "
                                "Read the draft shown now and accept it again.")
        problems = validate_letter(candidate.profile, job, draft.text, draft.sources)
        if problems:
            raise CoverLetterError("This draft no longer passes validation against your current profile, "
                                   "so it was not accepted.", problems)
        letter = CoverLetter(text=draft.text, origin="ai_draft", sources=list(draft.sources), at=utcnow())
        application.cover_letter = letter
        application.cover_letter_draft = None
        _content_changed(application)
    return letter


def discard_draft(session: Session, job: Job) -> None:
    """Throw away the pending draft. It isn't package content, so approval is untouched."""
    with transactions.write(session, job):
        application = job.application
        if application.cover_letter_draft is None:
            raise CoverLetterError("There is no cover letter draft to discard.")
        application.cover_letter_draft = None
        application.updated_at = utcnow()


def save_letter(session: Session, job: Job, text: str) -> CoverLetter:
    """Store the user's own letter body, replacing an accepted one. A pending draft is kept for comparison."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        raise CoverLetterError("The letter is empty. Remove the cover letter instead if you don't want one.")
    if len(text) > TEXT_LIMIT:
        raise CoverLetterError(f"Keep the letter under {TEXT_LIMIT:,} characters.")
    with transactions.write(session, job):
        application = job.application
        current = application.cover_letter
        if current is not None and current.origin == "user" and current.text == text:
            return current  # unchanged: approval stays
        letter = CoverLetter(text=text, origin="user", at=utcnow())
        application.cover_letter = letter
        _content_changed(application)
    return letter


def remove_letter(session: Session, job: Job) -> None:
    """Leave the cover letter out of the package. A pending draft stays."""
    with transactions.write(session, job):
        application = job.application
        if application.cover_letter is None:
            raise CoverLetterError("There is no cover letter to remove.")
        application.cover_letter = None
        _content_changed(application)


# ---------------------------------------------------------------- display

def paragraphs(text: str) -> list[str]:
    """The body's paragraphs: blocks separated by a blank line, each with its own line breaks kept."""
    return [block.strip() for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if block.strip()]


def document(contact: dict, company: str, text: str) -> dict:
    """Everything a printed or exported letter shows. ``contact`` is the profile's (or a snapshot's) contact."""
    company = (company or "").strip()
    return {
        "contact": contact,
        "greeting": f"Dear {company} hiring team," if company else "Dear hiring team,",
        "paragraphs": paragraphs(text),
        "closing": CLOSING,
        "name": contact.get("name", ""),
    }


@dataclass(frozen=True)
class LetterState:
    """The Cover letter section of the workspace."""

    letter: CoverLetter | None
    draft: CoverLetterDraft | None
    draft_token: str = ""
    draft_cited: list[tuple[str, str]] = field(default_factory=list)
    draft_outdated: bool = False  # drafted before the latest profile or job edit
    unsupported: list[str] = field(default_factory=list)  # why the accepted AI letter no longer holds
    letter_cited: list[tuple[str, str]] = field(default_factory=list)


def state(job: Job, application: Application, candidate: Candidate | None) -> LetterState:
    letter, draft = application.cover_letter, application.cover_letter_draft
    if candidate is None:
        return LetterState(letter, draft)
    sources = profile_service.sources(candidate.profile)

    def cited(ids: list[str]) -> list[tuple[str, str]]:
        return [(i, sources[i].text) for i in ids if i in sources]

    return LetterState(
        letter=letter,
        draft=draft,
        draft_token=draft_token(job, draft) if draft else "",
        draft_cited=cited(draft.sources) if draft else [],
        draft_outdated=bool(draft and (draft.profile_revision != candidate.revision
                                       or draft.job_revision != job.revision)),
        unsupported=letter_problems(job, application, candidate.profile),
        letter_cited=cited(letter.sources) if letter else [],
    )
