"""Authenticated OpenAlex paging and refusal safety with an in-memory HTTP adapter."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter

from tests.retriever.test_openalex_researchsquare import SOURCES, item, page
from zot2dailypaper.retriever import openalex_researchsquare as module


KEY = 'fake-openalex-test-key-never-a-real-secret'


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.delenv('OPENALEX_API_KEY', raising=False)
    calls, closed = [], []
    responses = []

    class Adapter(BaseAdapter):
        def send(self, request, **kwargs):
            calls.append(request)
            status, data, headers = responses.pop(0)
            response = requests.Response()
            response.status_code, response.url, response.request = status, request.url, request
            response._content = json.dumps(data).encode()
            response._content_consumed = True
            response.headers.update(headers)
            response.close = lambda: closed.append(request.url)
            return response

        def close(self):
            pass

    client = requests.Session()
    client.trust_env = False
    client.mount(module.API + '/', Adapter())
    monkeypatch.setattr(module, 'session', lambda: client)
    yield client, calls, closed, responses
    client.close()


def retriever():
    return module.OpenAlexResearchSquare({'source_ids': [SOURCES[0]], 'type': ['preprint'],
        'subfield': ['Synthetic Chemistry'], 'window_days': 1, 'max_pages': 3})


def catalog(responses):
    responses.extend([
        (200, {'id': 'https://openalex.org/' + SOURCES[0], 'display_name': 'Research Square'}, {}),
        (200, {'meta': {'count': 1}, 'results': [
            {'id': 'https://openalex.org/subfields/1000', 'display_name': 'Synthetic Chemistry'}]}, {}),
    ])


def test_configured_key_authenticates_catalog_and_every_cursor_page_without_query_secret(transport, monkeypatch):
    _, calls, closed, responses = transport
    monkeypatch.setenv('OPENALEX_API_KEY', KEY)
    catalog(responses)
    responses.extend([(200, page([item()], 2, 'opaque-next-cursor'), {}),
                      (200, page([item(2)], 2), {})])
    papers = retriever().retrieve()
    assert len(papers) == 2 and len(calls) == len(closed) == 4
    assert all(request.headers['Authorization'] == 'Bearer ' + KEY for request in calls)
    assert all(KEY not in request.url and 'api_key=' not in request.url for request in calls)
    params = [parse_qs(urlsplit(request.url).query) for request in calls if urlsplit(request.url).path == '/works']
    assert params[0]['cursor'] == ['*'] and params[1]['cursor'] == ['opaque-next-cursor']
    assert params[0]['filter'] == params[1]['filter']
    assert params[0]['per_page'] == params[1]['per_page'] == ['100']


def test_keyless_first_page_remains_supported_when_it_is_complete(transport):
    _, calls, closed, responses = transport
    catalog(responses)
    responses.append((200, page([item()]), {}))
    assert len(retriever().retrieve()) == 1
    assert len(calls) == len(closed) == 3
    assert all('Authorization' not in request.headers for request in calls)


@pytest.mark.parametrize('with_key', [False, True])
@pytest.mark.parametrize('status', [401, 403, 429])
def test_cursor_refusal_is_visible_without_partial_results_or_credential_downgrade(transport, monkeypatch, with_key, status):
    _, calls, closed, responses = transport
    if with_key:
        monkeypatch.setenv('OPENALEX_API_KEY', KEY)
    catalog(responses)
    responses.extend([(200, page([item()], 2, 'opaque-next-cursor'), {}),
                      (status, {'error': 'untrusted body ' + KEY}, {})])
    with pytest.raises(RuntimeError, match=rf'HTTP {status} at works cursor continuation') as error:
        retriever().retrieve()
    assert KEY not in str(error.value) and 'untrusted body' not in str(error.value)
    assert ('is configured' if with_key else 'is not configured') in str(error.value)
    assert 'no paid fallback' in str(error.value)
    assert len(calls) == len(closed) == 4 and not responses
    assert all(('Authorization' in request.headers) is with_key for request in calls)


def test_initial_cursor_refusal_is_identified_separately(transport):
    client, calls, closed, responses = transport
    responses.append((401, {'error': 'untrusted body'}, {}))
    with pytest.raises(RuntimeError, match='HTTP 401 at works cursor initial'):
        module.get_json(client, '/works', {'cursor': '*'})
    assert len(calls) == len(closed) == 1


def test_bearer_key_is_not_forwarded_to_redirect_target(transport, monkeypatch):
    client, calls, closed, responses = transport
    monkeypatch.setenv('OPENALEX_API_KEY', KEY)
    responses.append((302, {}, {'Location': 'https://other.example/collect'}))
    with pytest.raises(RuntimeError, match='HTTP 302.*redirects are disabled'):
        module.get_json(client, '/works', {'cursor': '*'})
    assert len(calls) == 1 and set(closed) == {calls[0].url}
    assert urlsplit(calls[0].url).netloc == 'api.openalex.org'


@pytest.mark.parametrize('key', ['line\nbreak', 'carriage\rreturn', 'white space', '非ASCII', 'x' * 513])
def test_invalid_key_stops_before_network_and_hides_value(transport, monkeypatch, key):
    client, calls, _, _ = transport
    monkeypatch.setenv('OPENALEX_API_KEY', key)
    with pytest.raises(ValueError, match='header-safe API key; value withheld') as error:
        module.get_json(client, '/works', {'cursor': '*'})
    assert key not in str(error.value) and calls == []


def test_transport_errors_cannot_echo_request_credentials(monkeypatch):
    monkeypatch.setenv('OPENALEX_API_KEY', KEY)

    class Client:
        def get(self, *args, **kwargs):
            raise requests.ConnectionError('untrusted diagnostics ' + KEY)

    with pytest.raises(RuntimeError, match='OpenAlex transport failed at works cursor initial') as error:
        module.get_json(Client(), '/works', {'cursor': '*'})
    assert KEY not in str(error.value) and 'untrusted diagnostics' not in str(error.value)


def test_workflow_exposes_key_only_to_retrieval_without_runtime_yaml_or_replay(tmp_path, monkeypatch):
    from pathlib import Path
    import shutil
    import yaml
    from scripts.prepare_workflow import prepare
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('OPENALEX_API_KEY', KEY)
    shutil.copytree(root / 'config', tmp_path / 'config', ignore=shutil.ignore_patterns('runtime.yaml', 'private.yaml'))
    prepare(tmp_path, {'GITHUB_EVENT_NAME': 'schedule'})
    runtime = (tmp_path / 'config/runtime.yaml').read_text()
    assert KEY not in runtime and 'OPENALEX_API_KEY' not in runtime
    workflow = yaml.load((root / '.github/workflows/main.yml').read_text(), Loader=yaml.BaseLoader)
    steps = workflow['jobs']['recommend']['steps']
    carrying = [step for step in steps if 'OPENALEX_API_KEY' in step.get('env', {})]
    assert len(carrying) == 1 and carrying[0]['name'] == 'Retrieve, rank and deliver'
    assert carrying[0]['env']['OPENALEX_API_KEY'] == '${{ secrets.OPENALEX_API_KEY }}'
    assert 'OPENALEX_API_KEY' not in (root / '.github/workflows/replay-delivery.yml').read_text()
