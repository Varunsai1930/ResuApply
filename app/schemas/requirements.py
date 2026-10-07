"""Job requirements, confirmed evidence links and overrides, as stored on ``jobs``.

The shapes match ResuSkill's requirements JSON (references/schemas.md). Values are
validated by ``app.services.requirements.validate_requirements`` before they get here;
these models guard the stored shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["skill", "education", "experience", "location", "authorization", "availability", "other"]
Importance = Literal["required", "preferred", "unspecified"]
CheckStatus = Literal["met", "unmet", "unknown"]

CATEGORIES: tuple[str, ...] = Category.__args__
IMPORTANCE: tuple[str, ...] = Importance.__args__
DEGREE_LEVELS = ("associate", "bachelor", "master", "phd")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillCriterion(_Model):
    type: Literal["skill"] = "skill"
    skills: list[str]
    match: Literal["all", "any"] = "all"


class DegreeCriterion(_Model):
    type: Literal["degree"] = "degree"
    level: Literal["associate", "bachelor", "master", "phd"]
    fields: list[str] = Field(default_factory=list)
    status: Literal["any", "completed", "pursuing"] = "any"


class GraduationCriterion(_Model):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    type: Literal["graduation_window"] = "graduation_window"
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class LocationCriterion(_Model):
    type: Literal["location"] = "location"
    locations: list[str] = Field(default_factory=list)
    work_mode: Literal["remote", "hybrid", "onsite"] | None = None


class AuthorizationCriterion(_Model):
    type: Literal["authorization"] = "authorization"
    country: str
    sponsorship_available: bool | None = None


class AvailabilityCriterion(_Model):
    type: Literal["availability"] = "availability"
    start_by: str | None = None
    start_from: str | None = None


class YearsCriterion(_Model):
    type: Literal["years_experience"] = "years_experience"
    years: float
    area: str = ""


Criterion = Annotated[
    SkillCriterion | DegreeCriterion | GraduationCriterion | LocationCriterion
    | AuthorizationCriterion | AvailabilityCriterion | YearsCriterion,
    Field(discriminator="type"),
]
CRITERION_TYPES = (
    "skill", "degree", "graduation_window", "location", "authorization", "availability", "years_experience",
)


class Requirement(_Model):
    id: str
    text: str
    category: Category = "other"
    importance: Importance = "unspecified"
    excerpt: str  # verbatim from the job description
    criterion: Criterion | None = None

    def criterion_dict(self) -> dict | None:
        return self.criterion.model_dump(by_alias=True) if self.criterion else None


class EvidenceLink(_Model):
    """Profile sources the user confirmed as evidence for one requirement."""

    sources: list[str] = Field(default_factory=list)
    source_hashes: dict[str, str] = Field(default_factory=dict)  # content confirmed for each source ID
    rejected: list[str] = Field(default_factory=list)  # AI suggestions the user turned down
    confirmed_at: datetime | None = None
    profile_revision: int | None = None


class Override(_Model):
    """A user's manual status for a requirement. Always has a recorded reason."""

    status: CheckStatus
    reason: str
    at: datetime
