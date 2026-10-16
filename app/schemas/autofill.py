"""The resume PDF the user attached for Greenhouse autofill, as stored in ``applications.resume_attachment``.

Its own nullable column, so older versions of the app read the database as before. The file
itself lives in the data folder; this record says which approved package it belongs to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ResumeAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: Annotated[str, Field(min_length=5, max_length=120)]  # the name the employer's form receives
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size: int = Field(ge=1)
    pages: int = Field(ge=1)
    # The approved package it was attached to (``Approval.content_hash``). Once the package
    # changes, the PDF no longer counts as the package's resume and is asked for again.
    package_hash: str
    attached_at: datetime
