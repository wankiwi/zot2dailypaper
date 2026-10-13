import io
from types import SimpleNamespace
import pytest
import requests
from urllib3.response import HTTPResponse
from zot2dailypaper.http import CrossrefAdapter, BoundedRetry, session


def test_crossref_uses_shared_gate_and_bounded_retries():
    with session() as client:
        adapter = client.get_adapter('https://api.crossref.org/journals')
        assert isinstance(adapter, CrossrefAdapter)
        assert adapter.max_retries.total == 5
        assert adapter.max_retries.backoff_max == 30
        assert 429 in adapter.max_retries.status_forcelist
        assert not isinstance(client.get_adapter('https://www.nature.com/'), CrossrefAdapter)


def test_crossref_pacing_across_sessions(monkeypatch):
    clock=[100.0]
    starts=[]
    monkeypatch.setattr('zot2dailypaper.http.time.monotonic',lambda:clock[0])
    monkeypatch.setattr('zot2dailypaper.http.time.sleep',lambda delay:clock.__setitem__(0,clock[0]+delay))
    monkeypatch.setattr(CrossrefAdapter,'_last_start',0.0)
    monkeypatch.setattr('requests.adapters.HTTPAdapter.send',lambda *a,**kw:starts.append(clock[0]))
    CrossrefAdapter().send(object())
    CrossrefAdapter().send(object())
    assert starts == [100.0,101.0]


def test_retry_after_respected_or_budget_error():
    retry=BoundedRetry(total=5)
    assert retry.get_retry_after(SimpleNamespace(headers={'Retry-After':'30'})) == 30
    with pytest.raises(requests.exceptions.RetryError, match='budget'):
        retry.get_retry_after(SimpleNamespace(headers={'Retry-After':'600'}))


@pytest.mark.parametrize('retry_after', ['30', '7200'])
def test_generic_adapter_respects_short_retry_after_and_refuses_long_wait(monkeypatch, retry_after):
    waits, responses = [], []

    def synthetic_response(*args, **kwargs):
        status = 503 if not responses else 200
        responses.append(status)
        return HTTPResponse(body=io.BytesIO(b'{}'), preload_content=False, status=status,
                            headers={'Retry-After': retry_after} if status == 503 else {})

    monkeypatch.setattr('urllib3.connectionpool.HTTPSConnectionPool._make_request', synthetic_response)
    monkeypatch.setattr('urllib3.util.retry.time.sleep', waits.append)
    with session() as client:
        client.trust_env = False  # No real network or ambient credentials in this test.
        adapter = client.get_adapter('https://api.openalex.org/works')
        assert adapter.max_retries.allowed_methods == frozenset({'GET'})
        if retry_after == '7200':
            # Requests may wrap the retry refusal as a connection error.
            with pytest.raises(requests.exceptions.RequestException, match='budget'):
                client.get('https://api.openalex.org/works', timeout=(10, 30))
            assert responses == [503] and waits == []
        else:
            assert client.get('https://api.openalex.org/works', timeout=(10, 30)).status_code == 200
            assert responses == [503, 200] and waits == [30]
