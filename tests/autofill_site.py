"""Local stand-ins for Greenhouse-hosted application forms, served to the autofill browser without any network.

``FixtureSite.install`` is the browser context's ``prepare_context``: every request the page makes
is answered from ``tests/fixtures/greenhouse_forms`` or refused, and recorded, so a test can check
that nothing was posted. Fictional content only.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

FORMS = Path(__file__).parent / "fixtures" / "greenhouse_forms"
POSTING_URL = "https://job-boards.greenhouse.io/examplerobotics/jobs/4012345"
EMPLOYER_URL = "https://careers.example.com/apply"


class FixtureSite:
    """Answers the browser's requests: ``pages`` maps a URL to a fixture file, or to (status, file)."""

    def __init__(self, pages: dict[str, object]):
        self.pages = pages
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def install(self, context) -> None:
        context.route("**/*", self._handle)

    def _handle(self, route) -> None:
        request = route.request
        with self._lock:
            self.requests.append((request.method, request.url))
        url = request.url.split("#", 1)[0]
        target = self.pages.get(url)
        if target is None:
            if "recaptcha" in url:  # the invisible reCAPTCHA frame most Greenhouse forms carry
                route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>reCAPTCHA</title>")
            else:
                route.abort()
            return
        status, name = target if isinstance(target, tuple) else (200, target)
        route.fulfill(status=status, content_type="text/html; charset=utf-8", body=(FORMS / name).read_text())

    @property
    def posts(self) -> list[tuple[str, str]]:
        """Every request that could have sent the form: anything but GET and HEAD."""
        with self._lock:
            return [r for r in self.requests if r[0] not in ("GET", "HEAD")]


_CHROMIUM: list[str | None] = []


def chromium() -> str | None:
    """The Chromium to launch: None for Playwright's own when it's installed, else one found under
    PLAYWRIGHT_BROWSERS_PATH (a preinstalled browser of another revision). Raises LookupError without one."""
    if _CHROMIUM:
        if _CHROMIUM[0] == "missing":
            raise LookupError
        return _CHROMIUM[0]
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        default = Path(playwright.chromium.executable_path)
    found: str | None = None
    if not default.exists():
        root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
        candidates = sorted([*root.glob("chromium-*/chrome-linux*/chrome"), *root.glob("chromium-*/chrome-mac*/*.app"
                             "/Contents/MacOS/*"), *root.glob("chromium-*/chrome-win*/chrome.exe")])
        found = str(candidates[-1]) if candidates else "missing"
    _CHROMIUM.append(found)
    if found == "missing":
        raise LookupError
    return found
