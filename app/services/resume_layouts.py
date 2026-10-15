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
    Layout("classic", "Classic", "Traditional Arial type with clear section rules."),
    Layout("modern", "Modern", "A navy accent and generous spacing for a clear hierarchy."),
    Layout("compact", "Compact", "Closer spacing to fit more detail while staying readable."),
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
