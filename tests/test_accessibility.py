import pytest
from tests.test_draft_recovery import editor  # noqa: F401


def test_skip_link_and_repeat_row_removal_keep_keyboard_focus(client, editor):
    page, expect, _ = editor
    page.goto('http://127.0.0.1:8000/profile/edit')
    page.keyboard.press('Tab')
    expect(page.get_by_role('link', name='Skip to content')).to_be_focused()
    page.keyboard.press('Enter')
    expect(page.get_by_role('main')).to_be_focused()
    add = page.get_by_role('button', name='Add experience', exact=True)
    add.focus()
    page.keyboard.press('Enter')
    entries = page.locator('#experience-list > [data-item]')
    expect(entries.last.locator('input[name$="-organization"]')).to_be_focused()
    remove = entries.last.get_by_role('button', name='Remove experience', exact=True)
    remove.focus()
    page.keyboard.press('Enter')
    expect(entries.first.locator('input[name$="-organization"]')).to_be_focused()
    entries.first.get_by_role('button', name='Remove experience', exact=True).focus()
    page.keyboard.press('Enter')
    expect(add).to_be_focused()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_input_boundaries_have_visible_contrast(editor, theme):
    page, _, _ = editor
    page.goto('http://127.0.0.1:8000/profile/edit')
    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
    colors = page.locator('#contact-name').evaluate('''e => {
      const s = getComputedStyle(e); return [s.borderTopColor, s.backgroundColor];
    }''')
    def luminance(color):
        values = [int(x.strip()) / 255 for x in color.split('(')[1].split(')')[0].split(',')[:3]]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
        return sum(c * weight for c, weight in zip(linear, [.2126, .7152, .0722]))
    a, b = sorted(map(luminance, colors))
    assert (b + .05) / (a + .05) >= 3


def test_error_summary_is_reachable_and_links_to_the_invalid_field(editor):
    page, expect, _ = editor
    page.goto('http://127.0.0.1:8000/profile/edit')
    page.get_by_role('button', name='Review changes', exact=False).click()
    summary = page.locator('.error-summary')
    expect(summary).to_be_focused()
    summary.locator('a[href="#contact-name"]').focus()
    page.keyboard.press('Enter')
    field = page.locator('#contact-name')
    expect(field).to_be_focused()
    expect(field).to_have_attribute('aria-invalid', 'true')
