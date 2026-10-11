"""How a proposed resume sentence differs from the profile lines it cites, word by word.

The review shows this so the user can see exactly what was reworded. It is a reading aid: it
says which words changed, never whether the new wording is still true; that stays the user's call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# A word (letters or digits, with inner apostrophes, dots, hyphens and a trailing + or #, so
# "don't", "Node.js", "C++" and "C#" stay whole), a single punctuation mark, or a run of spaces.
_TOKEN = re.compile(r"\w+(?:['’.\-]\w+)*[+#]*|[^\w\s]|\s+")
# Words too common to say where an added word came from.
_COMMON = frozenset("a an and as at by for from in into of on or the to with".split())


@dataclass(frozen=True)
class Segment:
    text: str
    marked: bool  # added (proposed side), removed (base side) or supporting (other sources)


@dataclass(frozen=True)
class Source:
    id: str
    segments: list[Segment]
    base: bool  # the line the proposal was compared with


@dataclass(frozen=True)
class Comparison:
    proposed: list[Segment]
    sources: list[Source]
    added: int  # words (punctuation is marked but not counted)
    removed: int
    unchanged: bool  # nothing marked: the same words, capitals and punctuation as the closest line


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text or "")


def _key(token: str) -> str:
    return token.casefold()


def _segments(tokens: list[str], marked: set[int]) -> list[Segment]:
    """Runs of marked and unmarked tokens; a space between two marked words joins their run."""
    out: list[Segment] = []
    for i, token in enumerate(tokens):
        flag = i in marked
        if token.isspace():
            before = i > 0 and (i - 1) in marked
            after = i + 1 < len(tokens) and (i + 1) in marked
            flag = before and after
        if out and out[-1].marked == flag:
            out[-1] = Segment(out[-1].text + token, flag)
        else:
            out.append(Segment(token, flag))
    return out


def _words(tokens: list[str]) -> list[int]:
    return [i for i, t in enumerate(tokens) if not t.isspace()]


def _is_word(token: str) -> bool:
    return token[0].isalnum() or token[0] == "_"


def _diff(old: list[str], new: list[str]) -> tuple[set[int], set[int]]:
    """Token positions removed from ``old`` and added in ``new``.

    Words are lined up regardless of case, so "Built" and "built" match up; a word whose
    capitals changed is then still marked on both sides, so "unchanged" means the same text.
    """
    old_words, new_words = _words(old), _words(new)
    matcher = SequenceMatcher(None, [_key(old[i]) for i in old_words], [_key(new[i]) for i in new_words],
                              autojunk=False)
    removed, added = set(), set()
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            for a, b in zip(old_words[a1:a2], new_words[b1:b2]):
                if old[a] != new[b]:
                    removed.add(a)
                    added.add(b)
        if op in ("replace", "delete"):
            removed.update(old_words[a1:a2])
        if op in ("replace", "insert"):
            added.update(new_words[b1:b2])
    return removed, added


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, [_key(t) for t in _tokens(a) if not t.isspace()],
                           [_key(t) for t in _tokens(b) if not t.isspace()], autojunk=False).ratio()


def compare(proposed: str, originals: list[dict]) -> Comparison:
    """Compare a proposed sentence with the profile lines it cites (``{"id", "text"}`` each).

    The closest line is the base: its removed words are marked, and the words the proposal adds
    to it are marked on the proposed side. In the other cited lines, the added words they contain
    are marked, showing where those words came from.
    """
    new = _tokens(proposed)
    if not originals:
        return Comparison([Segment(proposed, False)], [], 0, 0, False)
    base = max(originals, key=lambda o: _similarity(o["text"], proposed))
    old = _tokens(base["text"])
    removed, added = _diff(old, new)
    added_keys = {_key(new[i]) for i in added} - _COMMON
    sources = []
    for original in originals:
        if original is base:
            sources.append(Source(original["id"], _segments(old, removed), True))
            continue
        tokens = _tokens(original["text"])
        support = {i for i in _words(tokens) if _key(tokens[i]) in added_keys and _is_word(tokens[i])}
        sources.append(Source(original["id"], _segments(tokens, support), False))
    return Comparison(_segments(new, added), sources, sum(_is_word(new[i]) for i in added),
                      sum(_is_word(old[i]) for i in removed), not added and not removed)
