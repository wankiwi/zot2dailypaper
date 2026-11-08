"""Offline regressions for production run 42313797037."""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
import requests
from zot2dailypaper.journals import CORE, NATURE, Journal, discover_nature, selected_journals
from zot2dailypaper.retriever.journal_retriever import JournalRetriever


def test_acs_presets_keep_crossref_without_blocked_rss():
    for key in ('jacs', 'jctc', 'jpcl'):
        assert CORE[key].issns and CORE[key].rss is None


def test_digest_discovery_and_stale_cache_do_not_reintroduce_dead_feed():
    html = ''.join(f'<a href="/{slug}/">{j.title}</a>' for slug, j in NATURE.items())
    html += '<a href="/ndigest/">Nature Digest</a>'
    assert discover_nature(html)['ndigest'].rss is None
    stale = Journal('ndigest', 'Nature Digest', ('1347-5436',), 'https://www.nature.com/ndigest.rss')
    journal, = selected_journals({'presets': ['ndigest']}, {'ndigest': stale})
    assert journal.rss is None and journal.issns == stale.issns
    custom = {'id': 'ndigest', 'title': 'Nature Digest', 'rss': 'https://example.org/feed'}
    journal, = selected_journals({'presets': ['ndigest'], 'custom': [custom]}, {'ndigest': stale})
    assert journal.rss == custom['rss']


def response(status, items=()):
    r = requests.Response()
    r.status_code = status
    r.url = 'https://api.crossref.org/journals/example/works'
    r.json = lambda: {'message': {'items': items}}
    return r


@pytest.mark.parametrize('statuses', [(404, 200), (200, 404)])
def test_one_missing_issn_keeps_other_index_results(config, statuses):
    item = {'title': ['Real article'], 'DOI': '10.1021/example', 'ISSN': ['0002-7863'],
            'published': {'date-parts': [[2026, 11, 1]]}}
    responses = iter(response(s, [item] if s == 200 else []) for s in statuses)
    client = SimpleNamespace(get=lambda *a, **kw: next(responses))
    papers = JournalRetriever(config)._crossref(CORE['jacs'], client,
        datetime(2026, 10, 30, tzinfo=timezone.utc), datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert [p.doi for p in papers] == ['10.1021/example']


@pytest.mark.parametrize('statuses', [(404, 404), (403,), (429,), (500,)])
def test_absent_indexes_access_and_transport_errors_remain_visible(config, statuses):
    responses = iter(response(s) for s in statuses)
    client = SimpleNamespace(get=lambda *a, **kw: next(responses))
    with pytest.raises(requests.HTTPError):
        JournalRetriever(config)._crossref(CORE['jacs'], client,
            datetime(2026, 10, 30, tzinfo=timezone.utc), datetime(2026, 11, 6, tzinfo=timezone.utc))
