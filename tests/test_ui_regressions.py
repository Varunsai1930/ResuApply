"""Offline visual geometry and cold-load budgets, with screenshots for human review.

Run with RESUAPPLY_UI_ARTIFACT_DIR=validation-results/ui to retain PNGs and JSON.
Every request uses the test's synthetic database; there is no external browser traffic.
"""
import json
import os
from html.parser import HTMLParser
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import pytest

from tests.test_draft_recovery import editor  # noqa: F401
from tests.test_resume_export_routes import accepted_job

HTML_LIMIT = 192 * 1024
STATIC_LIMIT = 512 * 1024
CSS_LIMIT = 32 * 1024
JS_LIMIT = 32 * 1024


@pytest.fixture(autouse=True)
def require_ci_browser():
    if os.environ.get('RESUAPPLY_REQUIRE_UI_BROWSER') == '1':
        try:
            import playwright.sync_api  # noqa: F401
            from tests.autofill_site import chromium
            chromium()
        except (ImportError, LookupError):
            pytest.fail('Visual checks require Playwright and Chromium. Install requirements-dev.txt and run playwright install chromium.')


class Assets(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = set()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        key = 'href' if tag == 'link' and attrs.get('rel') in ('stylesheet', 'icon') else 'src'
        if tag in ('script', 'link', 'img') and attrs.get(key):
            self.urls.add(attrs[key])


def loading_budget(client, response, cache):
    assert len(response.content) <= HTML_LIMIT, 'HTML exceeds 192 KiB'
    assets = {}
    for url in sorted(Assets(response.text).urls):
        parsed = urlsplit(url)
        assert not parsed.netloc and parsed.path.startswith('/static/'), f'External or unexpected asset: {url}'
        if url not in cache:
            with client.stream('GET', url, headers={'Accept-Encoding': 'gzip'}) as asset:
                content = b''.join(asset.iter_raw())
                assert asset.status_code == 200, url
                assert 'immutable' in asset.headers.get('cache-control', ''), url
            cache[url] = len(content)
        assets[url] = cache[url]
    css = sum(size for url, size in assets.items() if urlsplit(url).path.endswith('.css'))
    scripts = sum(size for url, size in assets.items() if urlsplit(url).path.endswith('.js'))
    assert css <= CSS_LIMIT, f'Compressed CSS exceeds 32 KiB: {css}'
    assert scripts <= JS_LIMIT, f'Compressed JavaScript exceeds 32 KiB: {scripts}'
    assert sum(assets.values()) <= STATIC_LIMIT, 'Cold static transfer exceeds 512 KiB'
    return {'html_bytes': len(response.content), 'cold_static_bytes': sum(assets.values()),
            'css_bytes': css, 'javascript_bytes': scripts, 'assets': assets}


def geometry(page):
    return page.evaluate('''() => {
      const rect = e => {const r = e.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right};};
      const main = document.querySelector('main');
      const heading = main.querySelector('h1');
      const visible = e => e.getBoundingClientRect().width > 0 && getComputedStyle(e).visibility !== 'hidden';
      const rgb = value => value.match(/[\\d.]+/g).slice(0,3).map(Number);
      const light = values => values.map(v => {v /= 255; return v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4;})
        .reduce((sum,v,i) => sum + v * [.2126,.7152,.0722][i], 0);
      let background = heading;
      while (background.parentElement && getComputedStyle(background).backgroundColor === 'rgba(0, 0, 0, 0)') background = background.parentElement;
      const a = light(rgb(getComputedStyle(heading).color)), b = light(rgb(getComputedStyle(background).backgroundColor));
      return {viewport: innerWidth, document: document.documentElement.scrollWidth, main:rect(main), heading:rect(heading),
        headings:[...main.querySelectorAll('h1')].filter(visible).length, heading_font:parseFloat(getComputedStyle(heading).fontSize),
        heading_contrast:(Math.max(a,b)+.05)/(Math.min(a,b)+.05), header:rect(document.querySelector('header')),
        footer:rect(document.querySelector('footer')), broken_images:[...main.querySelectorAll('img')].filter(e => !e.complete || !e.naturalWidth).map(e => e.src)};
    }''')


def assert_geometry(metrics):
    assert metrics['document'] <= metrics['viewport'] + 1, 'Horizontal page overflow'
    assert metrics['headings'] == 1 and metrics['heading_font'] >= 24, 'Missing primary heading hierarchy'
    assert metrics['heading_contrast'] >= 4.5, 'Primary heading contrast below 4.5:1'
    main = metrics['main']
    assert main['width'] >= metrics['viewport'] * .8, 'Main content unexpectedly narrow'
    assert abs(main['x'] - (metrics['viewport'] - main['right'])) <= 2, 'Main content is not centered'
    assert metrics['heading']['y'] >= metrics['header']['bottom'] - 1, 'Header overlaps the heading'
    assert metrics['footer']['y'] >= main['bottom'] - 1, 'Footer overlaps main content'
    assert not metrics['broken_images'], metrics['broken_images']


@pytest.mark.parametrize('width,theme', [(1273, 'light'), (1273, 'dark'), (390, 'light'), (320, 'dark')])
def test_critical_page_visual_and_loading_budgets(client, sample_profile, editor, tmp_path, width, theme):
    page, expect, requests = editor
    job_id = accepted_job(client, sample_profile)
    label = f'{width}-{theme}'
    root = Path(os.environ.get('RESUAPPLY_UI_ARTIFACT_DIR', tmp_path)) / label
    root.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({'width': width, 'height': 900})
    page.emulate_media(color_scheme=theme, reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    paths = [('welcome', '/welcome'), ('jobs', '/jobs'), ('profile', '/profile'), ('profile-edit', '/profile/edit'),
             ('answers', '/answers'), ('examples', '/resume-examples'), ('data', '/data')]
    paths += [(step, f'/jobs/{job_id}?step={step}') for step in ('requirements', 'resume', 'questions', 'review', 'track')]
    report = {'viewport': width, 'theme': theme, 'reduced_motion': True, 'pages': []}
    cache = {}
    try:
        for name, path in paths:
            started = perf_counter()
            response = client.get(path)
            elapsed = round((perf_counter() - started) * 1000, 2)
            assert response.status_code == 200, path
            entry = {'page': path, 'local_render_ms': elapsed}
            report['pages'].append(entry)
            entry.update(loading_budget(client, response, cache))
            page.goto('http://127.0.0.1:8000' + path)
            for image in page.locator('main img').all():
                image.scroll_into_view_if_needed()
                expect(image).to_have_js_property('complete', True)
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=str(root / f'{name}.png'), full_page=True, animations='disabled')
            entry['geometry'] = geometry(page)
            assert_geometry(entry['geometry'])
            assert not errors, errors
        assert not any(method != 'GET' for method, _ in requests), requests
        # Demonstrate that the visual contract rejects an actual layout regression.
        page.add_style_tag(content='main { width: 1800px !important; max-width:none !important; }')
        with pytest.raises(AssertionError, match='overflow|centered|narrow'):
            assert_geometry(geometry(page))
    finally:
        (root / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
