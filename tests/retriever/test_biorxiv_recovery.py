"""Offline regression tests for the 2026-11-10 bioRxiv category timeout."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests
from urllib3.exceptions import MaxRetryError, ReadTimeoutError

from zot2dailypaper.retriever.biorxiv_retriever import BiorxivRetriever
from zot2dailypaper.retriever.medrxiv_retriever import MedrxivRetriever


def item(doi, category='biochemistry', version=1):
    return dict(doi=f'10.1101/{doi}', category=category, version=version,
                date=datetime.now(timezone.utc).date().isoformat())


def page(items, total=None):
    return {'messages': [{'status': 'ok', 'total': len(items) if total is None else total}],
            'collection': items}


def client(monkeypatch, outcomes):
    calls = []
    results = iter(outcomes)
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            outcome = next(results)
            if isinstance(outcome, Exception):
                raise outcome
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: outcome)
    monkeypatch.setattr('zot2dailypaper.retriever.biorxiv_retriever.session', Client)
    return calls


def retriever(config, cls=BiorxivRetriever, categories=None, max_pages=100):
    config.preprint_interests = {cls.name: {'categories': categories or ['biochemistry', 'biophysics']}}
    config.source[cls.name].window_days = 1
    config.source[cls.name].max_pages = max_pages
    return cls(config)


@pytest.mark.parametrize('exc', [requests.ReadTimeout('slow category'),
    requests.ConnectionError(MaxRetryError(None, '/details', ReadTimeoutError(None, '/details', '30s')))])
def test_full_window_recovers_all_categories_and_versions(config, monkeypatch, exc):
    r = retriever(config)
    old = item('keep', version=1)
    newest = item('keep', version=2)
    outside = item('exclude', 'neuroscience')
    physics = item('physics', 'biophysics')
    calls = client(monkeypatch, [page([item('partial')]), exc,
                                page([old, outside], 4), page([newest, physics], 4)])
    assert r._retrieve_raw_papers() == [newest, physics]
    assert '?category=biochemistry' in calls[0][0]
    assert '?category=biophysics' in calls[1][0]
    assert [url.rsplit('/', 1)[1] for url, _ in calls[2:]] == ['0', '2']
    assert all(kw['timeout'] == (10, 60) for _, kw in calls)


@pytest.mark.parametrize('outcome, error', [
    (requests.ReadTimeout('date endpoint also slow'), requests.ReadTimeout),
    (page([], 2), RuntimeError),
    ({'messages': [{'status': 'error'}]}, RuntimeError),
    ({'messages': [{'status': 'ok', 'total': 'invalid'}]}, ValueError),
])
def test_failed_recovery_still_raises(config, monkeypatch, outcome, error):
    r = retriever(config)
    calls = client(monkeypatch, [requests.ReadTimeout(), outcome])
    with pytest.raises(error):
        r._retrieve_raw_papers()
    assert len(calls) == 2


def test_recovery_page_limit_never_returns_partial_success(config, monkeypatch):
    r = retriever(config, max_pages=1)
    client(monkeypatch, [requests.ReadTimeout(), page([item('one')], 2)])
    with pytest.raises(RuntimeError, match='max_pages'):
        r._retrieve_raw_papers()


@pytest.mark.parametrize('exc', [requests.HTTPError('403'), requests.HTTPError('429'),
    requests.exceptions.RetryError('status retries exhausted'), requests.exceptions.SSLError('TLS')])
def test_no_fallback_after_access_or_tls_failures(config, monkeypatch, exc):
    r = retriever(config)
    calls = client(monkeypatch, [exc])
    with pytest.raises(type(exc)):
        r._retrieve_raw_papers()
    assert len(calls) == 1


def test_wildcard_timeout_not_retried_with_same_query(config, monkeypatch):
    r = retriever(config, categories=['*'])
    calls = client(monkeypatch, [requests.ReadTimeout()])
    with pytest.raises(requests.ReadTimeout):
        r._retrieve_raw_papers()
    assert len(calls) == 1


def test_empty_recovery_is_valid(config, monkeypatch):
    r = retriever(config)
    client(monkeypatch, [requests.ReadTimeout(), {'messages': [{'status': 'no posts found'}]}])
    assert r._retrieve_raw_papers() == []


def test_medrxiv_inherits_recovery_without_changing_server(config, monkeypatch):
    r = retriever(config, cls=MedrxivRetriever, categories=['epidemiology'])
    record = item('med', 'epidemiology')
    calls = client(monkeypatch, [requests.ReadTimeout(), page([record])])
    assert r._retrieve_raw_papers() == [record]
    assert all('/details/medrxiv/' in url for url, _ in calls)
