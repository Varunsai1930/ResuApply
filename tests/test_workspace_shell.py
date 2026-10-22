"""The top bar: one header on every page holds the brand, the sections and the page-wide controls."""

from __future__ import annotations

import re

import pytest


PAGES = ("/jobs", "/welcome", "/profile", "/answers", "/jobs/new")


def header(page: str) -> str:
    return re.search(r'<header class="site-header">(.*?)</header>', page, re.S).group(1)


@pytest.mark.parametrize("path", PAGES)
def test_the_top_bar_holds_brand_sections_and_controls(client, path):
    page = client.get(path).text
    bar = header(page)
    assert re.search(r'<a class="brand" href="/welcome"', bar)
    assert '<nav aria-label="Main">' in bar
    assert 'href="/welcome"' in bar and "Getting started" in bar  # outside the main sections
    assert "Local workspace" in bar and "data-theme-toggle" in bar
    assert page.index('<header class="site-header">') < page.index('<main id="main"')


@pytest.mark.parametrize("path", PAGES)
def test_there_is_no_sidebar_to_collapse(client, path):
    page = client.get(path).text
    assert "data-sidebar-toggle" not in page and "workspace-sidebar" not in page
    assert "workspace.js" not in page


def test_the_old_sidebar_script_is_gone(client):
    assert client.get("/static/workspace.js").status_code == 404
