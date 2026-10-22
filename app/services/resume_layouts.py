"""The supported, presentation-only layouts for an accepted resume.

Layout selection never changes the resume's content. Request values are validated
strictly; old database rows can be displayed using the original Classic layout.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Layout:
    key: str
    label: str
    description: str


LAYOUTS = (
    Layout("classic", "Classic", "Centered header, traditional section rules and a balanced reading column."),
    Layout("modern", "Modern", "Split header, navy accents and section labels beside your experience."),
    Layout("compact", "Compact", "Condensed header, inline dates and tighter spacing for more detail."),
)


def get_layout(value: str) -> Layout:
    """Resolve a supported request value, refusing arbitrary layout names."""
    for layout in LAYOUTS:
        if layout.key == value:
            return layout
    raise ValueError("Choose Classic, Modern or Compact for the resume layout.")


def layout_key(application: object) -> str:
    """Display missing or invalid legacy choices using the original layout."""
    try:
        return get_layout(getattr(application, "resume_layout", "classic")).key
    except ValueError:
        return "classic"
