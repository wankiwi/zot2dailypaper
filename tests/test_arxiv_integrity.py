"""Real SDK and synthetic Atom responses; no network, SMTP or model calls."""
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from omegaconf import open_dict

from zot2dailypaper.retriever import arxiv_retriever as module
from zot2dailypaper.identity import deduplicate


def entry(number, primary='cs.AI', title=None):
    return f'''<entry><id>https://arxiv.org/abs/2610.{number:05d}v1</id>
    <title>{title or f'Paper {number}'}</title><summary>Complete original abstract.</summary>
    <published>2026-10-15T10:00:00Z</published><updated>2026-10-15T10:00:00Z</updated>
    <author><name>First Author</name></author><author><name>Second Author</name></author>
    <link rel="alternate" type="text/html" href="https://arxiv.org/abs/2610.{number:05d}v1"/>
    <link title="pdf" rel="related" type="application/pdf" href="https://arxiv.org/pdf/2610.{number:05d}v1"/>
    <category term="cs.AI"/><arxiv:primary_category term="{primary}"/></entry>'''


def page(entries='', total=0, start=0, maximum=100):
    return (f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
    xmlns:arxiv="http://arxiv.org/schemas/atom"><opensearch:totalResults>{total}</opensearch:totalResults>
    <opensearch:startIndex>{start}</opensearch:startIndex><opensearch:itemsPerPage>{maximum}</opensearch:itemsPerPage>
    {entries}</feed>''').encode()


def transport(monkeypatch, pages):
    calls = []
    def get(self, url, **kwargs):
        calls.append(parse_qs(urlsplit(url).query))
        response = requests.Response()
        response.status_code = 200
        response._content = pages[len(calls) - 1]
        response._content_consumed = True
        return response
    monkeypatch.setattr(requests.Session, 'get', get)
    monkeypatch.setattr(module.arxiv.time, 'sleep', lambda _: None)
    return calls


def retriever(config):
    config.source.arxiv.window_days = 7
    config.source.arxiv.category = ['cs.AI']
    config.preprint_interests = None
    return module.ArxivRetriever(config)


@pytest.mark.parametrize('payload,reason', [
    (b'<html><body>Temporary page</body></html>', 'not an Atom'),
    (b'<feed', 'malformed XML'),
    (b'<feed xmlns="wrong"/>', 'not an Atom'),
    (page(total=50), 'inconsistent page'),
    (page(entry(1), total=0), 'inconsistent page'),
    (page(entry(1), total=1, start=1), 'startIndex mismatch'),
    (page().replace(b'<opensearch:totalResults>0</opensearch:totalResults>', b''), 'totalResults'),
    (page(total=-1), 'totalResults'),
    (page(total='1.5'), 'totalResults'),
    (page().replace(b'<opensearch:itemsPerPage>100</opensearch:itemsPerPage>', b''), 'itemsPerPage'),
    (page(entry(1) + entry(2), total=2, maximum=1), 'inconsistent page'),
    (page(entry(1) + entry(1), total=2), 'repeated entries'),
])
def test_http_200_invalid_feeds_fail_once_without_partial_conversion(config, monkeypatch, payload, reason):
    calls = transport(monkeypatch, [payload])
    source = retriever(config)
    with pytest.raises(module.ArxivFeedError, match=reason):
        source.retrieve_papers()
    assert len(calls) == 1 and source._raw == {}


@pytest.mark.parametrize('old,new,reason', [
    ('<title>Paper 1</title>', '', 'title'),
    ('<summary>Complete original abstract.</summary>', '', 'summary'),
    ('<name>First Author</name>', '<name/>', 'author'),
    ('<published>2026-10-15T10:00:00Z</published>', '<published>broken</published>', 'published'),
    ('<updated>2026-10-15T10:00:00Z</updated>', '<updated>2026-10-15T10:00:00</updated>', 'updated'),
    ('<arxiv:primary_category term="cs.AI"/>', '', 'categories'),
    ('https://arxiv.org/abs/2610.00001v1', 'https://example.org/error', 'article ID'),
    ('href="https://arxiv.org/pdf/2610.00001v1"', '', 'links'),
    ('href="https://arxiv.org/pdf/2610.00001v1"', 'href="javascript:broken"', 'links'),
])
def test_incomplete_entry_metadata_is_not_silently_skipped(config, monkeypatch, old, new, reason):
    transport(monkeypatch, [page(entry(1).replace(old, new), total=1)])
    with pytest.raises(module.ArxivFeedError, match=reason):
        retriever(config).retrieve_papers()


@pytest.mark.parametrize('last,reason', [
    (page(total=2, start=1), 'inconsistent page'),
    (page(entry(2), total=3, start=1), 'totalResults changed'),
    (page(entry(1), total=2, start=1), 'repeated entries'),
    (b'<html>Temporary second page</html>', 'not an Atom'),
])
def test_broken_later_page_never_delivers_first_page(config, monkeypatch, last, reason):
    calls = transport(monkeypatch, [page(entry(1), total=2), last])
    source = retriever(config)
    with pytest.raises(module.ArxivFeedError, match=reason):
        source.retrieve_papers()
    assert [int(c['start'][0]) for c in calls] == [0, 1]
    assert source._raw == {}


def test_legitimate_empty_atom_is_successful_and_logs_stages(config, monkeypatch):
    calls = transport(monkeypatch, [page()])
    logs = []
    monkeypatch.setattr(module, 'logger', SimpleNamespace(info=logs.append, warning=logs.append))
    assert retriever(config).retrieve_papers() == []
    assert len(calls) == 1
    assert any('HTTP=200' in line and 'totalResults=0' in line for line in logs)
    assert any("'api_entries': 0" in line and "'keyword_kept': 0" in line for line in logs)


def test_full_and_short_pages_preserve_fields_and_category_keyword_counts(config, monkeypatch):
    # Nonempty short pages are valid: the SDK advances by the actual entry count.
    payloads = [page(entry(1, title='Molecular simulation') + entry(2, primary='cs.CV'), total=3),
                page(entry(3, title='Unrelated topic'), total=3, start=2)]
    calls = transport(monkeypatch, payloads)
    logs = []
    monkeypatch.setattr(module, 'logger', SimpleNamespace(info=logs.append, warning=logs.append))
    source = retriever(config)
    source.interests = {'keywords': ['molecular']}
    papers = source.retrieve_papers()
    assert [int(c['start'][0]) for c in calls] == [0, 2]
    assert len(papers) == 1
    paper = papers[0]
    assert paper.title == 'Molecular simulation' and paper.abstract == 'Complete original abstract.'
    assert paper.authors == ['First Author', 'Second Author']
    assert paper.url.endswith('/abs/2610.00001v1') and paper.pdf_url.endswith('/pdf/2610.00001v1')
    assert paper.published == datetime(2026, 10, 15, 10, tzinfo=timezone.utc) and paper.full_text is None
    assert any("'api_entries': 3, 'primary_category_kept': 2, 'converted': 2, 'keyword_kept': 1" in l for l in logs)


def test_debug_limit_and_version_dedup_remain_supported(config, monkeypatch):
    payload = page(''.join(entry(n) for n in range(1, 12)), total=11)
    transport(monkeypatch, [payload])
    source = retriever(config)
    config.executor.debug = True
    papers = source.retrieve_papers()
    assert len(papers) == 10
    from dataclasses import replace
    assert len(deduplicate([papers[0], replace(papers[0], url=papers[0].url.replace('v1', 'v2'))])) == 1


def test_full_100_entry_page_and_final_short_page_are_complete(config, monkeypatch):
    calls = transport(monkeypatch, [page(''.join(entry(n) for n in range(1, 101)), total=101),
                                    page(entry(101), total=101, start=100)])
    papers = retriever(config).retrieve_papers()
    assert len(papers) == 101 and len({p.url for p in papers}) == 101
    assert [int(c['start'][0]) for c in calls] == [0, 100]


def test_sdk_skipped_results_are_rejected(config, monkeypatch):
    transport(monkeypatch, [page(entry(1), total=1)])
    def missing(cls, entry):
        raise module.arxiv.Result.MissingFieldError('synthetic')
    monkeypatch.setattr(module.arxiv.Result, '_from_feed_entry', classmethod(missing))
    with pytest.raises(module.ArxivFeedError, match='SDK result count mismatch'):
        retriever(config).retrieve_papers()


def test_conversion_failure_is_not_reported_as_valid_empty(config, monkeypatch):
    transport(monkeypatch, [page(entry(1), total=1)])
    monkeypatch.setattr(module, 'Paper', lambda **kwargs: (_ for _ in ()).throw(ValueError('synthetic conversion error')))
    source = retriever(config)
    with pytest.raises(module.ArxivFeedError, match='conversion skipped'):
        source.retrieve_papers()
    assert source._raw == {}


def test_scheduled_empty_health_is_separate_from_delivery_records(config, monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'schedule')
    with open_dict(config):
        config.state.enabled = True
        config.state.path = str(tmp_path / 'recommendations.json')
    history = tmp_path / 'recommendations.json'
    history.write_text('preserve delivery history')
    transport(monkeypatch, [page()])
    assert retriever(config).retrieve_papers() == []
    assert history.read_text() == 'preserve delivery history'
    assert (tmp_path / 'arxiv_health.json').exists()


def test_incomplete_response_cannot_advance_daily_health(config, monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'schedule')
    with open_dict(config):
        config.state.enabled = True
        config.state.path = str(tmp_path / 'recommendations.json')
    history = tmp_path / 'arxiv_health.json'
    history.write_text('preserve prior source health')
    transport(monkeypatch, [page(total=10)])
    with pytest.raises(module.ArxivFeedError):
        retriever(config).retrieve_papers()
    assert history.read_text() == 'preserve prior source health'
