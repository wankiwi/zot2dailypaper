from datetime import datetime, timezone
from types import SimpleNamespace

from omegaconf import open_dict
import pytest

from zot2dailypaper.journals import CORE, NATURE, discover_nature, selected_journals
from zot2dailypaper.retriever.journal_retriever import JournalRetriever, crossref_date


def test_requested_presets_and_reviews(config):
    journals = selected_journals(config.source.journals)
    titles = {j.title for j in journals}
    assert set(CORE) <= {j.id for j in journals}
    assert 'Nature Reviews Chemistry' in titles
    assert 'Nature Reviews Molecular Cell Biology' in titles
    assert len(titles) == len(journals)


def test_nature_catalog_discovers_new_titles_without_external_links():
    html = ''.join(f'<a href="/{slug}/"><span>{j.title}</span></a>' for slug, j in NATURE.items())
    html += '<a href="/natnew/">Nature New Research</a><a href="https://bad.example/fake">Nature Fake</a>'
    found = discover_nature(html)
    assert found['natnew'].rss == 'https://www.nature.com/natnew.rss'
    assert 'fake' not in found
    with pytest.raises(ValueError):
        discover_nature('<html>Access denied</html>')


def test_unknown_journal_is_rejected():
    with pytest.raises(ValueError, match='Unknown'):
        selected_journals({'presets': ['unknown']})


def test_crossref_uses_real_publication_date():
    item = {'created': {'date-time': '2026-05-01'}, 'published-online': {'date-parts': [[2026, 3, 2]]}, 'issued': {'date-parts': [[2026, 4, 1]]}}
    assert crossref_date(item) == datetime(2026, 3, 2, tzinfo=timezone.utc)
    assert crossref_date({'created': {'date-time': '2026-03-02'}}) is None
    assert crossref_date({'published': {'date-parts': [[2026]]}}).month == 1


def response(data):
    return SimpleNamespace(json=lambda: {'message': data}, raise_for_status=lambda: None)


def entry(doi='10.1021/test', title='A chemical discovery', issn='0002-7863'):
    return {'DOI': doi, 'title': [title], 'ISSN': [issn], 'published-online': {'date-parts': [[2026, 3, 2]]},
            'abstract': '<jats:p>Atoms &amp; molecules</jats:p>', 'author': [{'given': 'A', 'family': 'B'}]}


def test_crossref_pagination_issn_filter_and_missing_abstract(config):
    retriever = JournalRetriever(config)
    calls = []
    pages = [{'items': [entry(f'10.1021/test{i}') for i in range(200)], 'next-cursor': 'next'},
             {'items': [entry('10.1021/last'), entry(issn='9999-9999'), entry(title='Editorial: issue')], 'next-cursor': 'done'}]
    pages[1]['items'][0].pop('abstract')
    def get(url, params, **kwargs):
        calls.append(params.copy())
        return response(pages.pop(0))
    client = SimpleNamespace(get=get)
    journal = CORE['jacs']
    from dataclasses import replace
    papers = retriever._crossref(replace(journal, issns=(journal.issns[0],)), client,
        datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 5, tzinfo=timezone.utc))
    assert len(papers) == 201
    assert calls[0]['cursor'] == '*' and calls[1]['cursor'] == 'next'
    assert 'from-pub-date:2026-03-01' in calls[0]['filter']
    assert 'index-date' not in calls[0]['filter']
    assert papers[-1].abstract == ''
    assert papers[0].abstract == 'Atoms & molecules'
    assert papers[0].journal == journal.title


def test_cover_labels_are_excluded_from_crossref_and_rss(config):
    from dataclasses import replace
    retriever = JournalRetriever(config)
    titles = ['Outside Front Cover: Molecular materials', 'Inside Back Cover (Issue 10)',
              'Front cover surfaces control molecular transport', 'Covers and interfaces in chemistry']
    since, until = datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 5, tzinfo=timezone.utc)
    journal = replace(CORE['jacs'], issns=(CORE['jacs'].issns[0],))
    client = SimpleNamespace(get=lambda *a, **kw: response({'items': [entry(f'10.1021/{i}', t) for i,t in enumerate(titles)]}))
    assert [p.title for p in retriever._crossref(journal, client, since, until)] == titles[2:]
    xml = '<rss version="2.0"><channel>' + ''.join(
        f'<item><title>{t}</title><link>https://doi.org/10.1021/{i}</link><pubDate>Mon, 02 Mar 2026 00:00:00 GMT</pubDate></item>'
        for i,t in enumerate(titles)) + '</channel></rss>'
    client = SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(content=xml.encode(), raise_for_status=lambda:None))
    assert [p.title for p in retriever._rss(journal, client, since, until)] == titles[2:]


def test_crossref_repeated_cursor_is_not_silent_truncation(config):
    retriever = JournalRetriever(config)
    client = SimpleNamespace(get=lambda *a, **kw: response({'items': [entry()] * 200, 'next-cursor': '*'}))
    with pytest.raises(RuntimeError, match='cursor'):
        retriever._crossref(CORE['jacs'], client, datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 5, tzinfo=timezone.utc))


def test_rss_xml_and_unknown_dates(config):
    retriever = JournalRetriever(config)
    content = b'''<rss version="2.0"><channel><title>JACS</title>
    <item><title>Article</title><link>https://doi.org/10.1021/example</link><description>Useful &amp; clear</description><pubDate>Mon, 02 Mar 2026 00:00:00 GMT</pubDate></item>
    <item><title>Undated</title><link>https://doi.org/10.1021/undated</link></item>
    </channel></rss>'''
    client = SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(content=content, raise_for_status=lambda: None))
    papers = retriever._rss(CORE['jacs'], client, datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 5, tzinfo=timezone.utc))
    assert len(papers) == 1
    assert papers[0].doi == '10.1021/example'
    assert papers[0].abstract == 'Useful & clear'


def test_aps_official_feed_excerpts_are_not_full_abstracts(config):
    retriever = JournalRetriever(config)
    assert CORE['prl'].rss == 'https://feeds.aps.org/rss/recent/prl.xml'
    content = b'<rss version="2.0"><channel><item><title>Research</title><link>https://doi.org/10.1103/example</link><description>Author(s): Names. Truncated abstr...</description><pubDate>Mon, 02 Mar 2026 00:00:00 GMT</pubDate></item></channel></rss>'
    client = SimpleNamespace(get=lambda *a,**kw:SimpleNamespace(content=content,raise_for_status=lambda:None))
    papers = retriever._rss(CORE['prl'],client,datetime(2026,3,1,tzinfo=timezone.utc),datetime(2026,3,5,tzinfo=timezone.utc))
    assert len(papers)==1 and papers[0].abstract==''


def test_exact_issn_resolution_rejects_similar_titles(config):
    retriever = JournalRetriever(config)
    client = SimpleNamespace(get=lambda *a, **kw: response({'items': [
        {'title': 'Nature Chemistry News', 'ISSN': ['9999-9999']}, {'title': 'Nature Chemistry', 'ISSN': ['1755-4349']}]}))
    assert retriever._resolve_issns(NATURE['nchem'], client).issns == ('1755-4349',)


def test_parallel_journals_catalog_cache_and_failure_reporting(config, tmp_path, monkeypatch):
    with open_dict(config):
        config.source.journals.presets = ['jacs', 'jctc']
        config.source.journals.catalog_cache = str(tmp_path / 'catalog.json')
    retriever = JournalRetriever(config)
    monkeypatch.setattr(retriever, '_journal', lambda j, *args: (j, [], j.id == 'jctc'))
    assert retriever.retrieve_papers() == []
    assert retriever.failures == [CORE['jctc'].title]
    assert (tmp_path / 'catalog.json').exists()


def test_nature_index_excludes_brand_services():
    html = ''.join(f'<a href="/{slug}">{j.title}</a>' for slug, j in NATURE.items())
    html += '<a href="/nature-index">Nature Index</a><a href="/nature-portfolio">Nature Portfolio</a>'
    assert 'nature-index' not in discover_nature(html)
    assert 'nature-portfolio' not in discover_nature(html)


def test_strict_nature_refresh_failure_is_visible(config, tmp_path, monkeypatch):
    with open_dict(config):
        config.source.journals.catalog_cache = str(tmp_path / 'catalog.json')
        config.source.journals.require_live_catalog = True
    retriever = JournalRetriever(config)
    from contextlib import contextmanager
    @contextmanager
    def unavailable(*args):
        raise OSError('catalog offline')
        yield
    monkeypatch.setattr('zot2dailypaper.retriever.journal_retriever.session', unavailable)
    with pytest.raises(OSError, match='catalog offline'):
        retriever._catalog()


def test_journal_configuration_rejects_invalid_issn_and_plain_http_feed():
    from zot2dailypaper.journals import Journal
    with pytest.raises(ValueError, match='ISSN'):
        Journal('bad', 'Bad', ('wrong',))
    with pytest.raises(ValueError, match='HTTPS'):
        Journal('bad', 'Bad', rss='http://example.org/feed')


@pytest.mark.parametrize('content', ['{broken', '[]', '{"nature": null}', '{"issns": {"jacs": ["invalid"]}}'])
def test_invalid_catalog_cache_is_rebuilt(config, tmp_path, content):
    path = tmp_path / 'catalog.json'
    path.write_text(content)
    config.source.journals.catalog_cache = str(path)
    config.source.journals.presets = ['jacs']
    retriever = JournalRetriever(config)
    assert retriever._catalog() == [CORE['jacs']]
    assert retriever.cache == {}


def test_explicit_issns_override_old_catalog_cache(config, tmp_path):
    path = tmp_path / 'catalog.json'
    path.write_text('{"issns": {"jacs": ["1549-9618"]}}')
    config.source.journals.catalog_cache = str(path)
    config.source.journals.presets = ['jacs']
    assert JournalRetriever(config)._catalog()[0].issns == CORE['jacs'].issns


def test_rss_bad_entry_does_not_discard_good_entries(config, monkeypatch):
    import feedparser
    entries = [
        {'title': 'Invalid link', 'link': 'javascript:alert(1)', 'published_parsed': (2026,3,2,0,0,0)},
        {'title': 'Invalid date', 'link': 'https://example.org/bad', 'published_parsed': (2026,99,2,0,0,0)},
        {'title': 'Good', 'link': 'https://example.org/good', 'published_parsed': (2026,3,2,0,0,0)},
    ]
    monkeypatch.setattr(feedparser, 'parse', lambda _: SimpleNamespace(bozo=False, entries=entries))
    client = SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(content=b'', raise_for_status=lambda: None))
    retriever = JournalRetriever(config)
    papers = retriever._rss(CORE['jacs'], client, datetime(2026,3,1,tzinfo=timezone.utc), datetime(2026,3,5,tzinfo=timezone.utc))
    assert [p.title for p in papers] == ['Good']
    assert CORE['jacs'].title in retriever.failures


def test_six_additional_journals_survive_live_catalog(config):
    added = {'pnas','acs_catalysis','npjcompumats','angew','chemical_science','mlst'}
    discovered = dict(NATURE)
    journals = selected_journals(config.source.journals, discovered)
    assert added <= {j.id for j in journals}
    assert sum(j.id == 'npjcompumats' for j in journals) == 1
    assert len({j.id for j in journals}) == len(journals)
    assert all(CORE[key].issns for key in added)
    assert CORE['npjcompumats'].issns == ('2057-3960',)


def test_discovered_entry_does_not_erase_verified_issn(config):
    from zot2dailypaper.journals import Journal
    discovered = {'npjcompumats': Journal('npjcompumats', 'npj Computational Materials')}
    journals = selected_journals(config.source.journals, discovered)
    assert [j for j in journals if j.id == 'npjcompumats'] == [CORE['npjcompumats']]


def test_crossref_html_entities_and_and_are_equivalent(config):
    retriever = JournalRetriever(config)
    client = SimpleNamespace(get=lambda *a, **kw: response({'items': [
        {'title': 'Nature Ecology &amp; Evolution', 'ISSN': ['2397-334X']}]}))
    assert retriever._resolve_issns(NATURE['natecolevol'], client).issns == ('2397-334X',)


def test_nature_official_footer_avoids_crossref_title_search(config):
    retriever = JournalRetriever(config)
    calls=[]
    def get(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(raise_for_status=lambda:None, text='<h1>Nature Ecology &amp; Evolution</h1><p>ISSN (International Standard Serial Number) 2397-334X (online)</p>')
    resolved = retriever._resolve_issns(NATURE['natecolevol'], SimpleNamespace(get=get))
    assert resolved.issns == ('2397-334X',)
    assert calls == ['https://www.nature.com/natecolevol/']


def test_rss_recovery_is_not_reported_as_complete_window(config, monkeypatch):
    from contextlib import nullcontext
    retriever = JournalRetriever(config)
    paper = SimpleNamespace(title='Article', doi='10.1021/example', url='https://doi.org/10.1021/example')
    monkeypatch.setattr('zot2dailypaper.retriever.journal_retriever.session', lambda *args:nullcontext(object()))
    monkeypatch.setattr(retriever,'_rss',lambda *args:[paper])
    monkeypatch.setattr(retriever,'_crossref',lambda *args:(_ for _ in ()).throw(RuntimeError('429 exhausted')))
    _, papers, failed = retriever._journal(CORE['prl'], None, None)
    assert papers == [paper] and failed


@pytest.mark.parametrize('indexed_error', ['404', 'unmatched'])
@pytest.mark.parametrize('rss_available', [True, False])
def test_unindexed_journal_rss_fallback_is_explicit(config, monkeypatch, indexed_error, rss_available):
    from contextlib import nullcontext
    import requests
    retriever = JournalRetriever(config)
    monkeypatch.setattr('zot2dailypaper.retriever.journal_retriever.session', lambda *args:nullcontext(object()))
    def rss(*args):
        if not rss_available:
            raise ValueError('invalid feed')
        return []  # A valid empty feed is not evidence of published articles.
    monkeypatch.setattr(retriever, '_rss', rss)
    def crossref(*args):
        if indexed_error == 'unmatched':
            raise ValueError('No exact Crossref ISSN match for new journal')
        response = requests.Response()
        response.status_code = 404
        raise requests.HTTPError('Not indexed', response=response)
    monkeypatch.setattr(retriever, '_crossref', crossref)
    warnings = []
    monkeypatch.setattr('zot2dailypaper.retriever.journal_retriever.logger.warning', warnings.append)
    _, papers, failed = retriever._journal(CORE['prl'], None, None)
    assert papers == [] and failed == (not rss_available)
    if rss_available:
        assert any('RSS-only fallback (0 items' in message and 'not verified' in message for message in warnings)
