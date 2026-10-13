"""Synthetic official-login failures; no live account, model or delivery calls."""
import pytest
import requests

from zot2dailypaper import openreview_auth as auth
from zot2dailypaper.retriever.openreview_retriever import OpenReviewRetriever


class Response:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self.data = {'token': 'synthetic-token'} if data is None else data
        self.headers, self.closed = headers or {}, False

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def close(self):
        self.closed = True


class Client:
    def __init__(self, outcomes):
        self.outcomes, self.calls, self.mounts = iter(outcomes), [], []
        self.headers = {'Authorization': 'Bearer stale-token'}

    def mount(self, url, adapter):
        self.mounts.append((url, adapter))

    def post(self, url, **kwargs):
        assert 'Authorization' not in self.headers
        self.calls.append((url, kwargs))
        item = next(self.outcomes)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, *args, **kwargs):
        pytest.fail('Rejected login attempted retrieval or anonymous fallback')


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv('OPENREVIEW_USERNAME', 'synthetic-user')
    monkeypatch.setenv('OPENREVIEW_PASSWORD', 'synthetic-password')
    waits = []
    monkeypatch.setattr(auth.time, 'sleep', waits.append)
    return waits


@pytest.mark.parametrize('first', [Response(503, ValueError('SENSITIVE HTML')), Response(502, {}),
                                  requests.ConnectTimeout('SENSITIVE request'), requests.ReadTimeout('SENSITIVE request'),
                                  requests.ConnectionError('SENSITIVE password')],
                         ids=['service-html', 'bad-gateway', 'connect-timeout', 'read-timeout', 'connection'])
def test_transient_login_recovers_once_at_fixed_official_endpoint(config, credentials, first):
    success = Response()
    client = Client([first, success])
    r = OpenReviewRetriever(config)
    r._authenticate(client)
    assert r.authenticated and client.headers['Authorization'] == 'Bearer synthetic-token'
    assert len(client.calls) == 2 and credentials == [2]
    assert all(url == auth.API + '/login' and kw['allow_redirects'] is False and kw['timeout'] == (10, 30)
               and kw['json'] == {'id': 'synthetic-user', 'password': 'synthetic-password', 'expiresIn': 3600}
               for url, kw in client.calls)
    assert client.mounts[0][0] == auth.API + '/login' and client.mounts[0][1].max_retries.total == 0
    assert success.closed and (not isinstance(first, Response) or first.closed)


@pytest.mark.parametrize('status,category', [(400, 'request_rejected'), (401, 'authentication_rejected'),
                                          (403, 'access_denied'), (429, 'rate_limited'), (302, 'redirect_rejected')])
def test_refusals_and_redirects_never_retry_or_continue(config, credentials, status, category):
    response = Response(status, {'message': 'SENSITIVE provider body'})
    client = Client([response])
    r = OpenReviewRetriever(config)
    with pytest.raises(auth.OpenReviewLoginError) as error:
        r._authenticate(client)
    assert error.value.http_status == status and error.value.category == category
    assert error.value.attempts == 1 and 'response withheld' in str(error.value)
    assert 'SENSITIVE' not in str(error.value) and not r.authenticated and not client.headers
    assert len(client.calls) == 1 and not credentials and response.closed


@pytest.mark.parametrize('message,category', [
    ('Invalid username or password SENSITIVE', 'credentials_or_login_identifier_rejected'),
    ('Account not activated SENSITIVE', 'account_activation_required'),
    ('MFA required SENSITIVE', 'MFA_required'),
    ('expiresIn must be a string SENSITIVE', 'login_expiry_parameter_rejected'),
])
def test_explicit_account_or_parameter_rejection_not_retried_even_on_503(config, credentials, message, category):
    client = Client([Response(503, {'errors': [{'message': message}]})])
    with pytest.raises(auth.OpenReviewLoginError) as error:
        OpenReviewRetriever(config)._authenticate(client)
    assert error.value.category == category and len(client.calls) == 1 and not credentials
    assert 'SENSITIVE' not in str(error.value)


@pytest.mark.parametrize('first', [Response(503, {}), requests.Timeout('SENSITIVE')], ids=['503', 'timeout'])
def test_transient_recovery_exhausts_at_two_attempts(config, credentials, first):
    client = Client([first, first])
    r = OpenReviewRetriever(config)
    with pytest.raises(auth.OpenReviewLoginError) as error:
        r._authenticate(client)
    assert error.value.attempts == 2 and len(client.calls) == 2 and credentials == [2]
    assert not r.authenticated and not client.headers and 'SENSITIVE' not in str(error.value)


@pytest.mark.parametrize('retry_after,allowed', [('5', True), ('60', False), ('SENSITIVE invalid', False),
                                              ('Fri, 01 Jan 2100 00:00:00 GMT', False)])
def test_retry_after_is_respected_or_defers_without_retry(config, credentials, retry_after, allowed):
    response = Response(503, {}, {'Retry-After': retry_after})
    client = Client([response, Response()])
    r = OpenReviewRetriever(config)
    if allowed:
        r._authenticate(client)
        assert credentials == [5] and len(client.calls) == 2
    else:
        with pytest.raises(auth.OpenReviewLoginError, match='retry_after_not_within_bound') as error:
            r._authenticate(client)
        assert len(client.calls) == 1 and not credentials and 'SENSITIVE' not in str(error.value)
    assert response.closed


@pytest.mark.parametrize('failure', [requests.exceptions.SSLError('SENSITIVE certificate'),
                                   requests.exceptions.ProxyError('SENSITIVE proxy'), RuntimeError('SENSITIVE client')],
                         ids=['tls', 'proxy', 'unexpected'])
def test_security_and_unexpected_client_errors_stop_without_disclosure(config, credentials, failure):
    client = Client([failure])
    with pytest.raises(auth.OpenReviewLoginError) as error:
        OpenReviewRetriever(config)._authenticate(client)
    assert len(client.calls) == 1 and not credentials and 'SENSITIVE' not in str(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__


@pytest.mark.parametrize('data', [ValueError('SENSITIVE body'), {'mfaPending': True, 'token': 'SENSITIVE'},
                                {}, {'token': 'SENSITIVE\n'}, {'token': '\x00'}, {'token': '\u00e9'}],
                         ids=['json', 'mfa', 'missing-token', 'header-newline', 'header-nul', 'non-ascii'])
def test_malformed_or_mfa_success_never_retries_or_sets_authorization(config, credentials, data):
    response = Response(200, data)
    client = Client([response])
    with pytest.raises(auth.OpenReviewLoginError) as error:
        OpenReviewRetriever(config)._authenticate(client)
    assert len(client.calls) == 1 and not credentials and not client.headers and response.closed
    assert 'SENSITIVE' not in str(error.value)


def test_auth_refusal_stops_before_notes_or_anonymous_fallback(config, credentials, monkeypatch):
    client = Client([Response(403, {})])
    class Context:
        def __enter__(self): return client
        def __exit__(self, *args): pass
    monkeypatch.setattr('zot2dailypaper.retriever.openreview_retriever.session', Context)
    with pytest.raises(auth.OpenReviewLoginError, match='HTTP 403'):
        OpenReviewRetriever(config).retrieve_papers()
    assert len(client.calls) == 1 and not client.headers
