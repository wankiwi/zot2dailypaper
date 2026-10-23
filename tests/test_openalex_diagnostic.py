"""Verify the actual source-only CLI's request bounds and authenticated paging offline."""
import json
from pathlib import Path

import pytest
import requests
import yaml

from scripts import diagnose_openalex as diagnostic
from tests.test_openalex_auth import KEY, catalog, retriever, transport  # noqa: F401
from tests.retriever.test_openalex_researchsquare import item, page


def audited_transport(transport, monkeypatch):
    client, calls, closed, responses = transport
    original = diagnostic.AuditSession
    adapter = client.get_adapter('https://api.openalex.org/')

    class StubSession(original):
        def __init__(self, key, audit):
            super().__init__(key, audit)
            self.mount('https://api.openalex.org/', adapter)

    monkeypatch.setattr(diagnostic, 'AuditSession', StubSession)
    monkeypatch.setenv('OPENALEX_API_KEY', KEY)
    return calls, closed, responses


def test_source_window_and_real_cursor_sample_validate_converted_fields(transport, monkeypatch):
    calls, _, responses = audited_transport(transport, monkeypatch)
    catalog(responses)
    responses.extend([(200, page([item()], 2, 'source-next'), {}), (200, page([item(2)], 2), {}),
        (200, page([item(3, publication_date='2026-10-21')], 25, 'historical-next'), {}),
        (200, page([item(4, publication_date='2026-10-22')], 25, 'another-cursor'), {})])
    report = diagnostic.diagnose(retriever().config)
    assert report['requests'] == len(calls) == 6
    assert report['source_window']['converted'] == report['source_window']['api_entries'] == 2
    probe = report['continuation_probe']
    assert probe['converted'] == probe['titles_populated'] == probe['authors_populated'] == 2
    assert probe['abstracts_populated'] == probe['https_links'] == probe['dates_populated'] == 2
    assert [p['cursor'] for p in probe['pages']] == ['initial', 'continuation']
    assert all(p['per_page'] == 1 and p['total'] == 25 for p in probe['pages'])
    assert not probe['full_retrieval'] and not report['full_text_checked']
    assert report['mail_model_and_production_state_operations'] == 0
    assert KEY not in json.dumps(report) and all(KEY not in r.url for r in calls)
    assert not responses


@pytest.mark.parametrize('status', [401, 403, 429])
def test_upstream_refusal_stops_the_diagnostic_without_fallback(transport, monkeypatch, status):
    calls, _, responses = audited_transport(transport, monkeypatch)
    catalog(responses)
    responses.extend([(200, page([item()]), {}), (status, {'error': 'untrusted ' + KEY}, {})])
    with pytest.raises(RuntimeError, match=f'HTTP {status}') as error:
        diagnostic.diagnose(retriever().config)
    assert KEY not in str(error.value) and len(calls) == 4 and not responses


def test_missing_key_never_attempts_anonymous_verification(transport):
    with pytest.raises(RuntimeError, match='no anonymous diagnostic fallback'):
        diagnostic.diagnose(retriever().config)
    assert transport[1] == []


def test_continuation_sample_distinguishes_versions_without_claiming_unique_papers(transport, monkeypatch):
    _, _, responses = audited_transport(transport, monkeypatch)
    catalog(responses)
    responses.extend([(200, page([item()]), {}),
        (200, page([item(3, publication_date='2026-10-21')], 25, 'historical-next'), {}),
        (200, page([item(4, publication_date='2026-10-22', doi='https://doi.org/10.21203/rs.3.rs-3/v2')], 25), {})])
    probe = diagnostic.diagnose(retriever().config)['continuation_probe']
    assert probe['converted'] == 2 and probe['unique_identities'] == 1


@pytest.mark.parametrize('method,url', [('POST', 'https://api.openalex.org/works'),
    ('GET', 'http://api.openalex.org/works'), ('GET', 'https://other.example/works'),
    ('GET', 'https://api.openalex.org/api-keys'), ('GET', 'https://api.openalex.org/works?api_key=bad')])
def test_network_guard_rejects_writes_other_hosts_and_query_credentials(method, url):
    audit = {'requests': 0, 'phase': 'test'}
    with diagnostic.AuditSession(KEY, audit) as client:
        request = requests.Request(method, url, headers={'Authorization': 'Bearer ' + KEY}).prepare()
        with pytest.raises(RuntimeError, match='guard rejected'):
            client.send(request, allow_redirects=False)
        assert audit['requests'] == 0


def test_live_transport_has_no_retries_and_enforces_shared_request_limit():
    audit = {'requests': diagnostic.MAX_REQUESTS, 'phase': 'test'}
    with diagnostic.AuditSession(KEY, audit) as client:
        assert not client.trust_env
        assert client.get_adapter('https://api.openalex.org/works').max_retries.total == 0
        request = requests.Request('GET', 'https://api.openalex.org/works',
                                   headers={'Authorization': 'Bearer ' + KEY}).prepare()
        with pytest.raises(RuntimeError, match='request bound reached'):
            client.send(request, allow_redirects=False)


def test_diagnostic_workflow_is_read_only_with_only_its_source_key():
    root = Path(__file__).resolve().parents[1]
    text = (root / '.github/workflows/diagnose-openalex.yml').read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert workflow['on'] == {'push': {'branches': ['diagnostics/openalex-auth-20261023']}}
    assert workflow['permissions'] == {'contents': 'read'}
    steps = workflow['jobs']['verify']['steps']
    assert steps[0]['with']['persist-credentials'] == 'false'
    environments = [step['env'] for step in steps if 'env' in step]
    assert environments == [{'OPENALEX_API_KEY': '${{ secrets.OPENALEX_API_KEY }}'}]
    assert all('zot2dailypaper.main' not in step.get('run', '') for step in steps)


def test_cli_refuses_any_other_branch(monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'wankiwi/zot2dailypaper')
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'push')
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/main')
    with pytest.raises(RuntimeError, match='authorized read-only diagnostic branch'):
        diagnostic.main()
