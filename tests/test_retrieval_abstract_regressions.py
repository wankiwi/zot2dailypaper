"""Scientific plaintext survives each public abstract ingestion boundary."""
from datetime import datetime, timezone
from contextlib import nullcontext
import json
from types import SimpleNamespace

import pytest
from omegaconf import OmegaConf

from zot2dailypaper import abstracts, aps_metadata
from zot2dailypaper.journals import Journal
from zot2dailypaper.protocol import Paper
from zot2dailypaper.retriever.journal_retriever import JournalRetriever
from zot2dailypaper.retriever.researchsquare_retriever import ResearchSquareRetriever


@pytest.mark.parametrize('raw,expected', [
    ('<jats:p>The phase persists at T<Tc and x>0.</jats:p>', 'The phase persists at T<Tc and x>0.'),
    ('<p>The phase persists at T&lt;Tc and x&gt;0.</p>', 'The phase persists at T<Tc and x>0.'),
    ('<p>A <i>molecular</i> study &amp; results.</p>', 'A molecular study & results.'),
    ('No abstract available', ''),
])
@pytest.mark.parametrize('source', ['crossref_journal', 'publisher_rss', 'researchsquare'])
def test_ingested_abstract_preserves_scientific_text(monkeypatch, raw, expected, source):
    cfg = OmegaConf.create({'source': {'journals': {}, 'researchsquare': {}},
                           'preprint_interests': None})
    item = {'DOI': '10.5555/test', 'title': ['Phase study'], 'ISSN': ['1234-5678'],
            'published': {'date-parts': [[2026, 10, 13]]}, 'abstract': raw}
    since, until = datetime(2026, 10, 12, tzinfo=timezone.utc), datetime(2026, 10, 14, tzinfo=timezone.utc)
    journal = Journal('test', 'Test Journal', ('1234-5678',), 'https://publisher.example/feed.xml')
    retriever = JournalRetriever(cfg)
    if source == 'crossref_journal':
        response = SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {'message': {'items': [item]}})
        client = SimpleNamespace(get=lambda *args, **kwargs: response)
        paper = retriever._crossref(journal, client, since, until)[0]
    elif source == 'publisher_rss':
        entry = {'title': 'Phase study', 'summary': raw, 'link': 'https://doi.org/10.5555/test',
                 'published_parsed': (2026, 10, 13, 0, 0, 0)}
        feed = SimpleNamespace(bozo=False, entries=[entry])
        monkeypatch.setattr('zot2dailypaper.retriever.journal_retriever.feedparser.parse', lambda _: feed)
        client = SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None, content=b'offline-feed'))
        paper = retriever._rss(journal, client, since, until)[0]
    else:
        paper = ResearchSquareRetriever(cfg).convert_to_paper(dict(item, DOI='10.21203/rs.3.rs-1/v1'))
    assert paper.abstract == expected


@pytest.mark.parametrize('researchsquare', [False, True])
@pytest.mark.parametrize('provider', ['Crossref', 'OpenAlex'])
@pytest.mark.parametrize('mismatch', [None, 'title', 'doi'])
def test_recovery_compares_cleaned_title_and_still_requires_exact_identity(
        monkeypatch, researchsquare, provider, mismatch):
    doi = '10.21203/rs.3.rs-10/v1' if researchsquare else '10.5555/title-test'
    paper = Paper('researchsquare' if researchsquare else 'journals', 'Atomistic study & benchmark',
                  [], '', 'https://doi.org/' + doi, doi=doi)
    found_doi = '10.5555/another' if mismatch == 'doi' else doi
    found_title = 'An unrelated study' if mismatch == 'title' else '<i>Atomistic</i> study &amp; benchmark'
    text = 'A sufficiently long complete abstract describing an atomistic molecular benchmark.'
    crossref = {'message': {'DOI': found_doi, 'title': [found_title], 'abstract': text}}
    openalex = {'doi': 'https://doi.org/' + found_doi, 'title': found_title,
                'abstract_inverted_index': {word: [i for i, item in enumerate(text.split()) if item == word]
                                            for word in set(text.split())}}
    def get(url, **kwargs):
        actual_provider = 'Crossref' if 'crossref' in url else 'OpenAlex'
        if actual_provider != provider:
            return SimpleNamespace(status_code=404)
        payload = crossref if actual_provider == 'Crossref' else openalex
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)
    client = SimpleNamespace(get=get)
    monkeypatch.setattr(abstracts, 'session', lambda *args: nullcontext(client))
    monkeypatch.setattr(abstracts, 'publisher_session', lambda: nullcontext(client))
    def fetch(client, url, actual_provider, blocked):
        if actual_provider != provider:
            return None
        return json.dumps(crossref if actual_provider == 'Crossref' else openalex).encode()
    monkeypatch.setattr(aps_metadata, 'fetch', fetch)
    abstracts.recover_abstracts([paper], {'enabled': True, 'publisher_fallback': False})
    assert paper.abstract == (text if mismatch is None else '')
    if mismatch is None:
        assert paper.abstract_source == provider
    else:
        statuses = {attempt['status'] for attempt in paper.abstract_recovery_attempts}
        assert statuses & {'doi_version_title_mismatch', 'title_mismatch', 'doi_version_mismatch'}
