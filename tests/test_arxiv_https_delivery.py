"""Only validated official arXiv article HTTP URLs may cross the HTTPS boundary."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from tests.canned_responses import make_sample_paper
from zot2dailypaper.identity import arxiv_https_url, deduplicate, paper_id
from zot2dailypaper.recommendation_sync import citation_payload, SyncUnavailable
from zot2dailypaper.retriever import arxiv_retriever as module
from zot2dailypaper.state import State, load_paper, paper_dict
from zot2dailypaper.zotero_action import confirmation_link


@pytest.mark.parametrize('url', [
    'http://arxiv.org/abs/2610.23776v1', 'http://arxiv.org/pdf/2610.23776v1.pdf',
    'http://www.arxiv.org/html/2610.23776v2', 'http://export.arxiv.org/abs/hep-th/9901001v2',
    'http://arxiv.org/abs/math.GT/0309136', 'HTTP://ARXIV.ORG/abs/2401.1234',
])
def test_official_article_upgrade_preserves_identity_and_confirmation_link(url):
    paper = make_sample_paper(url=url, doi=None)
    upgraded = replace(paper, url=arxiv_https_url(url))
    assert upgraded.url.startswith('https://')
    assert paper_id(upgraded) == paper_id(paper)
    assert confirmation_link(upgraded, 'https://zot2dailypaper-save.wankaiweii.workers.dev') == confirmation_link(paper, 'https://zot2dailypaper-save.wankaiweii.workers.dev')
    assert len(deduplicate([paper, upgraded])) == 1
    assert json.loads(citation_payload(paper))['url'] == upgraded.url


@pytest.mark.parametrize('url', [
    'http://example.org/paper', 'http://arxiv.org.evil.example/abs/2610.23776v1',
    'http://evil@arxiv.org/abs/2610.23776v1', 'http://arxiv.org:80/abs/2610.23776v1',
    'http://arxiv.org./abs/2610.23776v1', 'http://%61rxiv.org/abs/2610.23776v1',
    'http://arxiv.org/abs/not-an-id', 'http://arxiv.org/abs/2613.23776v1',
    'http://arxiv.org/abs/2610.23776v0', 'http://arxiv.org/abs/2610.23776v1.pdf',
    'http://arxiv.org/abs/2610.23776v1/extra', 'http://arxiv.org/abs/%32%36%31%30.23776v1',
    'http://arxiv.org/abs/2610.23776v1?next=https://example.org',
    'http://arxiv.org/abs/2610.23776v1#section', 'http://arxiv.org\\@example.org/abs/2610.23776v1',
    'http://arxiv.org/abs/2610.23776v1\n', 'http://arxiv.org/abs/２６１０.23776v1',
])
def test_lookalikes_malformed_and_non_arxiv_http_remain_rejected(url):
    assert arxiv_https_url(url) == url
    with pytest.raises(SyncUnavailable):
        citation_payload(make_sample_paper(url=url))


def test_new_retrieval_normalizes_both_links_and_enrichment_raw_key(config, monkeypatch):
    raw = SimpleNamespace(entry_id='http://arxiv.org/abs/2610.23776v1',
                          pdf_url='http://arxiv.org/pdf/2610.23776v1', title='Saved paper',
                          authors=[SimpleNamespace(name='Author')], summary='Original abstract.')
    source = module.ArxivRetriever(config)
    paper = source.convert_to_paper(raw)
    assert paper.url == 'https://arxiv.org/abs/2610.23776v1'
    assert paper.pdf_url == 'https://arxiv.org/pdf/2610.23776v1'
    assert source._raw[paper.url] is raw
    seen = []
    monkeypatch.setattr(module, 'extract_text_from_html', lambda value: seen.append(value) or 'Extracted text')
    assert source.enrich(paper).full_text == 'Extracted text' and seen == [raw]


def test_restored_pending_and_delivered_records_keep_channels_and_saved_summary(tmp_path):
    path = tmp_path / 'state.json'
    pending = make_sample_paper(url='http://arxiv.org/abs/2610.23776v1', doi=None,
                                pdf_url='http://arxiv.org/pdf/2610.23776v1', tldr='已保存的摘要。', tldr_status='generated')
    delivered = make_sample_paper(url='http://arxiv.org/abs/2610.10000v2', doi=None, title='Previously delivered')
    state = State(path)
    state.add([pending, delivered]); state.mark([delivered], 'email'); state.save()
    before = json.loads(path.read_text())
    restored = State(path)
    papers = restored.pending('email')
    assert len(papers) == 1 and papers[0].url.startswith('https://arxiv.org/')
    assert papers[0].tldr == pending.tldr and papers[0].tldr_attempts == pending.tldr_attempts
    assert set(restored.records) == set(before['records']) and restored.has(pending)
    assert restored.records[paper_id(delivered)]['channels'] == {'email': True}
    assert paper_dict(load_paper(paper_dict(pending)))['url'].startswith('https://')
