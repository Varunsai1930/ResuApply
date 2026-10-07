"""The canonical candidate profile, as stored in ``candidates.profile``.

The shape matches ResuSkill's profile JSON (references/schemas.md), minus ``_meta``:
the revision and ID counters live in their own columns. Values here are already
normalized by ``app.services.profile.normalize``; these models guard the stored shape.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WorkMode = Literal["remote", "hybrid", "onsite", "any"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Bullet(_Model):
    id: str
    text: str


class Links(_Model):
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Contact(_Model):
    name: str
    email: str = ""
    phone: str = ""
    location: str = ""
    links: Links = Field(default_factory=Links)


class Education(_Model):
    id: str
    institution: str
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    gpa: str = ""
    bullets: list[Bullet] = Field(default_factory=list)


class Experience(_Model):
    id: str
    organization: str
    title: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    technologies: list[str] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)


class Project(_Model):
    id: str
    name: str
    role: str = ""
    link: str = ""
    start: str = ""
    end: str = ""
    technologies: list[str] = Field(default_factory=list)
    bullets: list[Bullet] = Field(default_factory=list)


class Skill(_Model):
    name: str
    category: str = "Skills"


class Certification(_Model):
    id: str
    name: str
    issuer: str = ""
    date: str = ""


class Preferences(_Model):
    roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    work_mode: WorkMode | None = None


class Availability(_Model):
    start_date: str = ""
    notes: str = ""


class Authorization(_Model):
    """Per-country facts supplied by the user. ``None`` means unknown."""

    country: str
    authorized: bool | None = None
    requires_sponsorship: bool | None = None


class Profile(_Model):
    contact: Contact
    summary: str = ""
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    skills_absent: list[str] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    availability: Availability = Field(default_factory=Availability)
    authorization: list[Authorization] = Field(default_factory=list)
