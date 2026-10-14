"""Detect damaged Unicode in JSON and make legacy text safe to review."""

from __future__ import annotations

import re

from pydantic import BaseModel

_SURROGATE = re.compile(r"[\ud800-\udfff]")


def has_invalid_unicode(value: object) -> bool:
    """JSON may decode an isolated UTF-16 surrogate that cannot be rendered as UTF-8."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and _SURROGATE.search(item):
            return True
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, BaseModel):
            pending.extend(getattr(item, name) for name in type(item).model_fields)
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return False


def repair_unicode(value):
    """Return a reviewable copy, replacing isolated surrogates with the visible U+FFFD marker.

    This never writes stored data. A valid surrogate pair from a legacy Python string is
    recovered as its original character; ordinary Unicode, including emoji, is unchanged.
    """
    if isinstance(value, str):
        return (value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
                if _SURROGATE.search(value) else value)
    if isinstance(value, dict):
        return {repair_unicode(k): repair_unicode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [repair_unicode(v) for v in value]
    return value
