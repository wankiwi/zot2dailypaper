import hashlib
import pytest
from zot2dailypaper.zotero_action import confirmation_link
from zot2dailypaper.identity import paper_id
from zot2dailypaper.construct_email import render_email, email_plain_text
from tests.canned_responses import make_sample_paper


def test_confirmation_is_disabled_by_default():
    paper=make_sample_paper()
    assert confirmation_link(paper) is None
    assert '/zotero/confirm' not in render_email([paper])


def test_email_action_has_only_public_paper_identifier():
    paper=make_sample_paper()
    link=confirmation_link(paper,'https://papers.example.org')
    assert link=='https://papers.example.org/zotero/confirm?paper='+hashlib.sha256(paper_id(paper).encode()).hexdigest()
    html=render_email([paper],zotero_action_origin='https://papers.example.org')
    assert link in html and link in email_plain_text(html)
    assert '保存到 Zotero' in html and '需登录确认' not in html
    assert '<form' not in html and 'api.zotero.org' not in html

@pytest.mark.parametrize('origin',['http://papers.example.org','https://user:secret@papers.example.org',
 'https://papers.example.org/?token=secret','https://papers.example.org/secret',
 'https://papers.example.org/#secret','https://papers.example.org:444','https://localhost',42])
def test_no_credentials_or_capabilities_in_configured_origin(origin):
    with pytest.raises(ValueError):confirmation_link(make_sample_paper(),origin)
