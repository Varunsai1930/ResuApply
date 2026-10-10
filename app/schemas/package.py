"""Application questions, answers, AI answer drafts, package approval and submitted snapshots.

Shapes follow ResuSkill's package model (resuskill_core.package and .questions):

- Questions belong to the job (they come from the employer's form). IDs are ``q1``, ``q2`` ...
  and are never reused (``jobs.question_counter``).
- Answers and drafts belong to the application. A draft is an AI proposal for an *open*
  question; it is not an answer until the user accepts it.
- An approval records what was approved (a hash of the resolved package) and the profile and
  job revisions it was built from. Changed content clears approval (Draft); changed inputs make
  it Stale. Approval never sets Applied.
- A submitted snapshot freezes the approved package when the user records Applied. Later
  profile or job edits never alter it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .profile import Profile

QuestionCategory = Literal["factual", "sensitive_factual", "sensitive", "open", "unknown"]
QUESTION_CATEGORIES: tuple[str, ...] = QuestionCategory.__args__
LimitUnit = Literal["chars", "words"]
# How an accepted answer came to be. "profile": a factual value from the profile (confirmed by the
# user for sensitive-factual questions); "ai_draft": an accepted AI draft; "user": typed by the user;
# "bank": started from an answer-bank entry and accepted by the user.
AnswerOrigin = Literal["profile", "ai_draft", "user", "bank"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Question(_Model):
    id: str
    text: str
    required: bool = True
    limit: int | None = Field(default=None, ge=1)
    limit_unit: LimitUnit = "chars"
    category: QuestionCategory
    detected_category: QuestionCategory  # what the rules said; a detected sensitive question stays sensitive
    factual_key: str | None = None  # profile field for factual and sensitive-factual questions
    added_at: datetime


class Answer(_Model):
    question_id: str
    text: str = ""
    origin: AnswerOrigin
    sources: list[str] = Field(default_factory=list)  # profile source IDs backing an AI draft
    skipped: bool = False  # an optional question the user explicitly skipped
    confirmed: bool = False  # the user confirmed a profile value (required for sensitive-factual)
    bank_id: int | None = None
    at: datetime


class AnswerDraft(_Model):
    question_id: str
    text: str
    sources: list[str] = Field(min_length=1)
    model: str
    prompt_revision: str
    outbound_hash: str | None = None
    profile_revision: int
    job_revision: int
    created_at: datetime


class Approval(_Model):
    content_hash: str  # hash of the resolved package (resume + questions + answers)
    approved_at: datetime
    profile_revision: int
    job_revision: int
    warnings: list[str] = Field(default_factory=list)
    # The approval rules version it passed (package.APPROVAL_RULES). Approvals saved before
    # this was recorded read as 1, so they are checked against the current rules.
    rules: int = 1


class SubmittedSnapshot(_Model):
    """Everything the user submitted, frozen at the moment they recorded Applied."""

    id: int
    submitted_on: date
    recorded_at: datetime
    approval: Approval
    profile_revision: int
    job_revision: int
    job: dict[str, Any]  # title, company, location, url, description, revision
    resume: dict[str, Any]  # rendered resume context (profile facts + accepted claims)
    resume_html: str  # the rendered A4 resume document, exactly as it printed
    answers: list[dict[str, Any]]  # question, required, category, label, text
    profile: Profile
