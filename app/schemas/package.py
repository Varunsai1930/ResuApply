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

from copy import deepcopy
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_serializer, model_validator

from .profile import Profile, stored_profile

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


def stored_approval(value: Any) -> Any:
    """Read hook for a stored approval: drop the "rules" key one release wrote into it.

    The rules version lives in applications.approval_rules, outside this JSON, so earlier
    versions of the app can read stored approvals. Only stored data is adapted; an Approval
    built in code still rejects unknown fields.
    """
    if isinstance(value, dict) and "rules" in value:
        return {k: v for k, v in value.items() if k != "rules"}
    return value


def stored_snapshots(value: Any) -> Any:
    """Read legacy snapshots safely while retaining their frozen profile facts for storage."""
    if not isinstance(value, list):
        return value
    snapshots = []
    for snapshot in value:
        if not isinstance(snapshot, dict):
            snapshots.append(snapshot)
            continue
        adapted = dict(snapshot)
        if "approval" in adapted:
            adapted["approval"] = stored_approval(adapted["approval"])
        if "profile" in adapted:
            recovered = stored_profile(adapted["profile"])
            if isinstance(recovered, Profile) and recovered.unicode_recovered:
                adapted["profile"] = recovered
                loaded = SubmittedSnapshot.model_validate(adapted)
                loaded._stored_profile = deepcopy(snapshot["profile"])
                snapshots.append(loaded)
                continue
        snapshots.append(adapted)
    return snapshots


class SubmittedSnapshot(_Model):
    """Everything the user submitted, frozen at the moment they recorded Applied."""

    _stored_profile: dict[str, Any] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def readable_profile(self):
        # Revalidate nested text even when the database receives an existing snapshot
        # instance whose mutable Profile was changed after construction.
        Profile.model_validate(self.profile)
        return self

    @field_serializer("profile", mode="wrap")
    def serialize_profile(self, value, serialize, info):
        # Only the database asks for the original legacy payload. Display/export dumps
        # use readable text; approval upgrades and appending snapshots preserve history.
        if info.context and info.context.get("preserve_stored_snapshot_profile") and self._stored_profile is not None:
            return deepcopy(self._stored_profile)
        return serialize(value)

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
