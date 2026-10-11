"""The plain search the jobs board and the answer bank share: every word, in any case."""

from __future__ import annotations

SEARCH_LIMIT = 200  # characters of a search; anything longer is cut, never an error


def clean_query(raw: str | None) -> str:
    """A search as typed, with runs of spaces made one and cut to the limit."""
    return " ".join((raw or "").split())[:SEARCH_LIMIT]


def matches(text: str, query: str) -> bool:
    """Whether every word of the search appears in ``text``, ignoring case."""
    text = text.casefold()
    return all(word in text for word in query.casefold().split())
