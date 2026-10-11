"""Importing a job from Greenhouse's public Job Board API. The app's only outbound fetch besides the AI provider.

A fetch happens only when the user asks for it on the Add a job page. The rules are strict:

- only a posting link on ``boards.greenhouse.io`` or ``job-boards.greenhouse.io`` over https is
  accepted, and only its board name and job number are read from it;
- the request always goes to ``boards-api.greenhouse.io``, built here, never to the link itself;
- redirects are refused, the whole exchange has a deadline, and the response is read up to a size limit;
- every failure is a ``GreenhouseError`` with a plain message, raised before anything is saved.

Everything fetched is untrusted data: the description is stored as the job's text and the
question labels as question text, shown escaped and never obeyed.
"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx

from .text import clean_text

HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io")
API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true"
TIMEOUT_SECONDS = 10.0  # for the whole exchange, not per read
MAX_BYTES = 2 * 1024 * 1024  # a posting with its questions is tens of kilobytes

_BOARD = re.compile(r"[A-Za-z0-9_-]{1,100}")
_JOB_ID = re.compile(r"[0-9]{1,20}")
_JOB_PATH = re.compile(r"/([A-Za-z0-9_-]{1,100})/jobs/([0-9]{1,20})/?")


class GreenhouseError(Exception):
    """The posting couldn't be imported. Nothing was saved or changed."""


@dataclass(frozen=True)
class PostingRef:
    board: str
    job_id: str

    @property
    def api_url(self) -> str:
        return API.format(board=self.board, job_id=self.job_id)


@dataclass(frozen=True)
class Posting:
    title: str
    company: str
    location: str
    description: str  # plain text, from the posting's HTML


def posting_ref(url: str) -> PostingRef | None:
    """The board and job a Greenhouse posting link names, or None for any other link.

    Accepts ``https://boards.greenhouse.io/{board}/jobs/{id}`` (and the same on
    ``job-boards.greenhouse.io``), with or without a query, and the embedded form
    ``/embed/job_app?for={board}&token={id}``. Anything else (another host, http, a port,
    a user name, another path) is not a Greenhouse posting.
    """
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or (parts.hostname or "") not in HOSTS or parts.username is not None
            or parts.password is not None or port not in (None, 443)):
        return None
    if match := _JOB_PATH.fullmatch(parts.path):
        return PostingRef(match.group(1), match.group(2))
    if parts.path.rstrip("/") == "/embed/job_app":
        query = parse_qs(parts.query)
        board, job_id = (query.get("for") or [""])[0], (query.get("token") or [""])[0]
        if _BOARD.fullmatch(board) and _JOB_ID.fullmatch(job_id):
            return PostingRef(board, job_id)
    return None


# ---------------------------------------------------------------- fetching

def fetch(ref: PostingRef, transport: httpx.BaseTransport | None = None) -> dict:
    """The posting's JSON, read under the deadline and size limit. ``transport`` replaces the network in tests."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    body = bytearray()
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
            with client.stream("GET", ref.api_url, headers={"Accept": "application/json"}) as response:
                if response.status_code == 404:
                    raise GreenhouseError("Greenhouse has no such job. The posting may have closed; check the link.")
                if response.is_redirect:
                    raise GreenhouseError("Greenhouse answered with a redirect, which isn't followed. Nothing was "
                                          "imported; paste the posting instead.")
                if response.status_code != 200:
                    raise GreenhouseError(f"Greenhouse answered with an error ({response.status_code}). Nothing was "
                                          "imported; try again later or paste the posting instead.")
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > MAX_BYTES:
                    raise _too_large()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise _too_large()
                    if time.monotonic() > deadline:
                        raise httpx.ReadTimeout("deadline passed")
    except httpx.TimeoutException:
        raise GreenhouseError(f"Greenhouse took longer than {TIMEOUT_SECONDS:.0f} seconds. Nothing was imported; "
                              "try again, or paste the posting instead.") from None
    except httpx.HTTPError:
        raise GreenhouseError("Greenhouse couldn't be reached. Check your connection, or paste the posting "
                              "instead.") from None
    try:
        data = json.loads(bytes(body))
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        raise _malformed()
    return data


def _too_large() -> GreenhouseError:
    return GreenhouseError(f"Greenhouse's answer was over {MAX_BYTES // (1024 * 1024)} MB, which is not a job "
                           "posting. Nothing was imported; paste the posting instead.")


def _malformed() -> GreenhouseError:
    return GreenhouseError("Greenhouse's answer wasn't a job posting ResuApply can read. Nothing was imported; "
                           "paste the posting instead.")


# ---------------------------------------------------------------- reading the posting

_BLOCKS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section",
           "blockquote", "pre", "hr"}
_HIDDEN = {"script", "style", "template", "head"}


class _Text(HTMLParser):
    """The text of an HTML fragment: block elements start new lines, list items get a bullet."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN:
            self.hidden += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in _HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(fragment: str) -> str:
    """Plain text from the posting's content, which the API sends as escaped HTML."""
    parser = _Text()
    parser.feed(html.unescape(fragment or ""))
    parser.close()
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in clean_text("".join(parser.parts)).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _text(value: object, limit: int = 2000) -> str:
    """A short value from the posting as one clean line; anything that isn't text is empty."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", clean_text(html.unescape(value))).strip()[:limit]


def read_posting(data: dict) -> Posting:
    """The job's details and description from the API's JSON. A posting without a title or text is refused."""
    location = data.get("location")
    posting = Posting(
        title=_text(data.get("title"), 200),
        company=_text(data.get("company_name"), 200),
        location=_text(location.get("name") if isinstance(location, dict) else "", 200),
        description=html_text(data["content"]) if isinstance(data.get("content"), str) else "",
    )
    if not posting.title or not posting.description:
        raise _malformed()
    return posting
