"""Optional Chromium layout checks; synthetic citations, no email or external requests."""
import os
import json
from pathlib import Path

import pytest

from tests.canned_responses import make_sample_paper
from zot2dailypaper.construct_email import render_email

playwright = pytest.importorskip('playwright.sync_api')


@pytest.fixture(scope='module')
def browser():
    with playwright.sync_playwright() as api:
        chrome = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
        options = {'executable_path': str(chrome)} if chrome.is_file() else {}
        instance = api.chromium.launch(headless=True, **options)
        yield instance
        instance.close()


@pytest.mark.parametrize('width', [1200, 375, 320])
@pytest.mark.parametrize('with_pdf', [True, False])
def test_actions_have_gaps_and_wrap_without_overflow(browser, tmp_path, monkeypatch, width, with_pdf):
    # Show the normal layout; failure-warning rendering has separate budget tests.
    monkeypatch.setattr('zot2dailypaper.construct_email.pricing_warning', lambda: '')
    paper = make_sample_paper(title='Synthetic layout check', doi='10.1000/example',
                              pdf_url='https://example.org/paper.pdf' if with_pdf else None)
    html = render_email([paper], zotero_action_origin='https://zot2dailypaper-save.wankaiweii.workers.dev')
    context = browser.new_context(viewport={'width': width, 'height': 1100})
    try:
        page = context.new_page()
        page.route('**/*', lambda route: route.abort())
        page.set_content(html, wait_until='domcontentloaded')
        boxes = page.locator('.paper-action a').evaluate_all("""anchors => anchors.map(a => {
            const b=a.getBoundingClientRect(); return {x:b.x,y:b.y,right:b.right,bottom:b.bottom,text:a.textContent};
        })""")
        assert len(boxes) == (3 if with_pdf else 2)
        assert boxes[-1]['text'] == '保存到 Zotero'
        assert all(0 <= box['x'] < box['right'] <= width for box in boxes)
        for first, second in zip(boxes, boxes[1:]):
            if abs(first['y'] - second['y']) <= 1:
                assert second['x'] - first['right'] >= 19.5
            else:
                assert second['y'] >= first['bottom']
        if width == 1200:
            assert len({round(box['y']) for box in boxes}) == 1
        assert page.evaluate('document.documentElement.scrollWidth') <= width
        destination = Path(os.environ.get('EMAIL_LAYOUT_OUTPUT', str(tmp_path)))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / f'metrics-{width}-{"pdf" if with_pdf else "article"}.json').write_text(
            json.dumps({'width': width, 'buttons': boxes, 'scroll_width': page.evaluate('document.documentElement.scrollWidth'),
                        'external_page_requests_blocked': True}, ensure_ascii=True), encoding='utf-8')
        page.screenshot(path=str(destination / f'email-{width}-{"pdf" if with_pdf else "article"}.png'), full_page=True)
    finally:
        context.close()
