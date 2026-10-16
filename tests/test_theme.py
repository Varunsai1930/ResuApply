"""The light and dark mode toggle: the CSS that obeys it, the header button and the script."""

from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).parent.parent / "app" / "static"


def declarations(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))


def test_both_dark_blocks_set_the_same_colours():
    css = (STATIC / "style.css").read_text()
    system = re.search(r'@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme="light"\]\) \{(.*?)\}\s*\}',
                       css, re.S)
    chosen = re.search(r':root\[data-theme="dark"\] \{(.*?)\}', css, re.S)
    assert system and chosen
    assert declarations(system.group(1)) == declarations(chosen.group(1))
    assert len(declarations(chosen.group(1))) > 15 and "color-scheme: dark" in chosen.group(1)
    # A chosen light mode overrides a dark system setting: no other rule switches colours on the system alone.
    assert css.count("prefers-color-scheme") == 1


def test_every_page_has_the_toggle_and_loads_the_script_before_drawing(client):
    for path in ("/jobs", "/welcome", "/profile", "/answers"):
        page = client.get(path).text
        assert re.search(r'<script src="/static/theme\.js\?v=\d+"></script>', page)  # not deferred
        assert page.index("theme.js") < page.index("<body>")
        buttons = re.findall(r"<button[^>]*data-theme-toggle[^>]*>", page)
        assert len(buttons) == 1
        button = buttons[0]
        assert "hidden" in button and 'aria-pressed="false"' in button  # shown by the script
        assert '<span class="visually-hidden">Dark mode</span>' in page
        topbar = re.search(r'<header class="site-header">(.*?)</header>', page, re.S).group(1)
        assert "Local workspace" in topbar and "data-theme-toggle" in topbar


def test_the_script_is_served_and_keeps_the_choice_in_this_browser(client):
    response = client.get("/static/theme.js")
    assert response.status_code == 200 and "javascript" in response.headers["content-type"]
    script = response.text
    assert 'localStorage.getItem(KEY)' in script and 'localStorage.setItem(KEY, value)' in script
    assert script.count("try {") >= 2  # blocked storage never breaks the page
    assert 'root.dataset.theme = next' in script and 'button.hidden = false' in script
