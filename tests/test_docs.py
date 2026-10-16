"""The README and PLAN agree with the code: every outbound host, every documented limit, every local link.

These check facts the code defines rather than exact sentences, so the docs can be reworded freely
while a changed limit or a new outbound request still has to be documented.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.ai import client as ai_client
from app.services import greenhouse, resume_import, resume_ocr

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
PLAN = (ROOT / "PLAN.md").read_text()


def _mentions(text: str, phrase: str) -> bool:
    """Whether ``phrase`` appears, ignoring case and how its words are separated (space, hyphen, line break)."""
    words = [re.escape(word) for word in phrase.split()]
    return re.search(r"[\s-]+".join(words), text, re.IGNORECASE) is not None


def test_every_host_the_app_contacts_is_documented():
    hosts = {urlsplit(ai_client.API_URL).hostname, urlsplit(greenhouse.API.format(board="b", job_id=1)).hostname,
             *greenhouse.HOSTS}
    for host in hosts:
        assert host in README, f"{host} is contacted by the app but not named in the README"
    assert all(host in PLAN for host in greenhouse.HOSTS)


@pytest.mark.parametrize("phrase", [
    f"{greenhouse.TIMEOUT_SECONDS:.0f} seconds",
    f"{greenhouse.MAX_BYTES // (1024 * 1024)} MB",
    f"{resume_import.MAX_BYTES // (1024 * 1024)} MB",
    f"{resume_import.MAX_PAGES} pages",
    f"{resume_import.MAX_CHARS:,} character",
    f"{resume_ocr.TOTAL_SECONDS:.0f} second",
])
def test_documented_limits_match_the_code(phrase):
    assert _mentions(README, phrase), f"README doesn't state the limit {phrase!r}"


@pytest.mark.parametrize("name, text", [("README.md", README), ("PLAN.md", PLAN)])
def test_local_links_point_to_existing_files(name, text):
    for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
        if urlsplit(target).scheme:
            continue
        assert (ROOT / target).exists(), f"{name} links to missing file {target}"
