"""Workspace controls remain reachable outside the collapsible sidebar."""

from __future__ import annotations

import re

import pytest


PAGES = ("/jobs", "/welcome", "/profile", "/answers", "/jobs/new")


def topbar(page: str) -> str:
    return re.search(r'<div class="workspace-bar">(.*?)<main\b', page, re.S).group(1)


@pytest.mark.parametrize("path", PAGES)
def test_sidebar_control_names_its_target_and_has_a_usable_fallback(client, path):
    page = client.get(path).text
    assert len(re.findall(r'\bid="workspace-sidebar"', page)) == 1
    buttons = re.findall(r"<button\b([^>]*\bdata-sidebar-toggle[^>]*)>(.*?)</button>", page, re.S)
    assert len(buttons) == 1
    attrs, content = buttons[0]
    assert 'type="button"' in attrs
    assert 'aria-controls="workspace-sidebar"' in attrs
    assert 'aria-expanded="true"' in attrs
    assert re.search(r"\bhidden(?:\s|$)", attrs)  # shown only when its script is ready
    label = re.search(r'aria-label="([^"]+)"', attrs)
    accessible_name = label.group(1) if label else re.sub(r"<[^>]+>", "", content).strip()
    assert "sidebar" in accessible_name.lower()
    assert "data-sidebar-toggle" in topbar(page)  # available even while the sidebar is closed


@pytest.mark.parametrize("path", PAGES)
def test_workspace_breadcrumb_returns_to_jobs(client, path):
    links = re.findall(r'<a\b[^>]*href="/jobs"[^>]*>(.*?)</a>', topbar(client.get(path).text), re.S)
    assert [re.sub(r"<[^>]+>", "", content).strip() for content in links] == ["Workspace"]


def test_sidebar_script_is_loaded_before_drawing_and_is_served(client):
    page = client.get("/jobs").text
    script = re.search(r'<script src="(/static/workspace\.js\?v=\d+)"></script>', page)
    assert script and script.start() < page.index("<body>")
    response = client.get(script.group(1))
    assert response.status_code == 200 and "javascript" in response.headers["content-type"]
