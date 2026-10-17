"""The hosted application forms autofill supports: Greenhouse, Lever and Ashby.

A job's link names one posting on one of these hosts. ``form_ref`` reads which one (the platform,
the employer's board and the posting ID), so autofill can tell the form it opened from any other
page it may be sent to; ``form_url`` is the page with the application form itself. Anything else
(another host, http, a port, a user name, another path) is not a supported form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from . import greenhouse

GREENHOUSE, LEVER, ASHBY = "greenhouse", "lever", "ashby"
LABELS = {GREENHOUSE: "Greenhouse", LEVER: "Lever", ASHBY: "Ashby"}
LEVER_HOSTS = ("jobs.lever.co", "jobs.eu.lever.co")
ASHBY_HOSTS = ("jobs.ashbyhq.com",)
HOSTS = (*greenhouse.HOSTS, *LEVER_HOSTS, *ASHBY_HOSTS)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_LEVER_PATH = re.compile(rf"/([A-Za-z0-9._-]{{1,100}})/({_UUID})(/apply)?/?")
_ASHBY_PATH = re.compile(rf"/([A-Za-z0-9._%-]{{1,100}})/({_UUID})(/application)?/?")


@dataclass(frozen=True)
class FormRef:
    platform: str
    board: str
    job_id: str

    @property
    def label(self) -> str:
        return LABELS[self.platform]


def _parts(url: str):
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None or port not in (None, 443):
        return None
    return parts


def form_ref(url: str) -> FormRef | None:
    """The supported posting a link names, or None for any other link."""
    if (ref := greenhouse.posting_ref(url)) is not None:
        return FormRef(GREENHOUSE, ref.board, ref.job_id)
    parts = _parts(url)
    if parts is None:
        return None
    host = (parts.hostname or "").lower()
    if host in LEVER_HOSTS and (match := _LEVER_PATH.fullmatch(parts.path)):
        return FormRef(LEVER, match.group(1).lower(), match.group(2).lower())
    if host in ASHBY_HOSTS and (match := _ASHBY_PATH.fullmatch(parts.path)):
        return FormRef(ASHBY, match.group(1).lower(), match.group(2).lower())
    return None


def form_url(url: str) -> str:
    """The page with the application form for a supported posting link (``form_ref(url)`` is not None).

    A Greenhouse posting page carries its form; Lever's form is at ``…/apply`` and Ashby's at
    ``…/application``, so a link to the posting itself is sent there.
    """
    ref = form_ref(url)
    if ref is None or ref.platform == GREENHOUSE:
        return url.strip()
    parts = urlsplit(url.strip())
    suffix = "/apply" if ref.platform == LEVER else "/application"
    path = parts.path.rstrip("/")
    if not path.endswith(suffix):
        path += suffix
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
