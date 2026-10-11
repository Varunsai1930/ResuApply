"""The README and PLAN describe what the app does, including every request it makes."""

from __future__ import annotations

from pathlib import Path

from app.services import greenhouse

ROOT = Path(__file__).resolve().parent.parent


def test_the_docs_describe_the_greenhouse_import_and_its_limits():
    readme = (ROOT / "README.md").read_text()
    plan = (ROOT / "PLAN.md").read_text()
    assert "Greenhouse import and autofill are not started" not in readme
    assert "Greenhouse job import (fetched only when you choose Import) are built" in readme
    assert "The only other request the app makes is a Greenhouse job import" in readme
    assert f"gives up after {greenhouse.TIMEOUT_SECONDS:.0f} seconds or {greenhouse.MAX_BYTES // (1024 * 1024)} MB" in readme
    assert all(host in readme and host in plan for host in greenhouse.HOSTS)
    assert "nullable `jobs.posting_import` column" in plan
