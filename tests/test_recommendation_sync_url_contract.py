"""Producer rejects URLs that the preserved v8 browser URL parser rejects."""
import json

import pytest

from tests.canned_responses import make_sample_paper
from tests.test_recommendation_sync import D1, FAKE_TOKEN, SQLITE_CLIENTS, history
from zot2dailypaper.recommendation_sync import RecommendationSync, SyncUnavailable, citation_payload


@pytest.mark.parametrize('host', [
    '0x7f.0.0.1', '127.1', '0x7f000001.', '0177.0.0.1', '2130706433.',
    '8.8.8.8', '\uff11\uff12\uff17.0.0.1', '%31%32%37.0.0.1',
    'exa%mple.org', 'exa%2Fmple.org', 'example.org%3A443', 'xn--a.org',
    'example.0x', 'localhost.', 'example.invalid.',
])
def test_worker_incompatible_host_blocks_entire_batch_before_d1(tmp_path, host):
    state, papers = history(tmp_path, 2)
    papers[1].url = 'https://' + host + '/paper'
    client = D1()
    try:
        with pytest.raises(SyncUnavailable, match='Invalid recommendation metadata'):
            RecommendationSync(FAKE_TOKEN).sync(papers, state, client=client)
        assert client.calls == []
    finally:
        client.db.close()
        SQLITE_CLIENTS.remove(client)


@pytest.mark.parametrize('url', [
    'https://example.org/paper', 'https://example.org.:443/paper',
    'https://doi.org/10.1021/ABC', 'https://%65xample.org/paper',
    'https://m\u00fcnich.example/paper', 'https://\u4f8b\u5b50.\u6d4b\u8bd5/paper',
    'https://fa\u00df.de/paper', 'https://xn--fa-hia.de/paper',
    'https://example.org/\u4e2d\u6587?q=\u5b66\u672f#section',
])
def test_public_dns_idna_and_doi_urls_preserve_citation(url):
    paper = make_sample_paper(url=url)
    payload = json.loads(citation_payload(paper))
    assert payload['url'] == url.split('#')[0]
