"""A job imported from a posting (Greenhouse): its application questions as read, and what the import kept.

``ImportedQuestion``, ``LeftOut`` and ``ImportPreview`` describe an import the user is reviewing on
the Add a job page; the preview travels with that form, so it is validated again when the job is
saved. ``PostingImport`` is what a saved job keeps, in its own column (``jobs.posting_import``),
so older versions of the app read the database as before.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.dataclasses import dataclass

FieldType = Literal["short_text", "long_text", "single_select", "multi_select", "unsupported"]
LABEL_LIMIT = 2000  # answers.QUESTION_LIMIT: a longer label is never a question
Label = Annotated[str, Field(min_length=1, max_length=LABEL_LIMIT)]
Option = Annotated[str, Field(min_length=1, max_length=200)]
Reason = Annotated[str, Field(min_length=1, max_length=300)]

_CONFIG = ConfigDict(extra="forbid")


@dataclass(frozen=True, config=_CONFIG)
class ImportedQuestion:
    """One question of the posting's application form, as it will be added to the job."""

    label: Label
    required: bool
    field_type: FieldType
    options: tuple[Option, ...] = Field(default=(), max_length=100)
    # From the voluntary self-identification (EEOC) or demographic sections: always the user's own
    # answer, whatever the wording, so it is added as Sensitive.
    sensitive: bool = False
    greenhouse_type: str = Field(default="", max_length=100)  # the posting's name for an unsupported field


@dataclass(frozen=True, config=_CONFIG)
class LeftOut:
    """A form field that isn't added as a question, and what the user does instead."""

    label: Annotated[str, Field(min_length=1, max_length=300)]
    reason: Reason


@dataclass(frozen=True, config=_CONFIG)
class ImportPreview:
    """An import the user is reviewing before saving the job."""

    board: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    job_id: Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]
    fetched_at: datetime
    questions: tuple[ImportedQuestion, ...] = Field(default=(), max_length=100)
    left_out: tuple[LeftOut, ...] = Field(default=(), max_length=300)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


CHOICE_TYPES = ("single_select", "multi_select")


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


class QuestionField(_Model):
    """The form field an imported question was read from."""

    field_type: FieldType
    options: list[Option] = Field(default_factory=list, max_length=100)
    greenhouse_type: str = Field(default="", max_length=100)

    @property
    def is_choice(self) -> bool:
        """A "Choose one" or "Choose any" field with its choices listed."""
        return self.field_type in CHOICE_TYPES and bool(self.options)

    def accepts(self, text: str) -> bool:
        """Whether an answer names this field's choices as the employer's form writes them (case and spacing aside).

        A field without choices accepts any answer. "Choose one" takes one choice; "Choose any" takes
        one or more, one per line or separated by semicolons or commas.
        """
        if not self.is_choice:
            return True
        options = {_fold(option) for option in self.options}
        if _fold(text) in options:
            return True
        if self.field_type == "single_select":
            return False
        for separator in ("\n", ";", ","):
            parts = [_fold(part) for part in text.split(separator) if part.strip()]
            if len(parts) > 1 and all(part in options for part in parts):
                return True
        return False


class PostingImport(_Model):
    """What a job imported from a posting keeps: where from, when, and each question's form field."""

    source: Literal["greenhouse"] = "greenhouse"
    board: str
    job_id: str
    fetched_at: datetime
    fields: dict[str, QuestionField] = Field(default_factory=dict)  # question ID -> its form field
    left_out: list[LeftOut] = Field(default_factory=list)
