"""Offline transport regression for the October 3 arXiv 503 failure."""
from datetime import datetime,timezone,timedelta
from email.utils import format_datetime
import pytest
import requests
from zot2dailypaper.retriever import arxiv_retriever as module


def replies(monkeypatch,values):
    calls=[];waits=[]
    def get(self,url,**kwargs):
        calls.append((url,kwargs));value=values[len(calls)-1]
        if isinstance(value,Exception):raise value
        response=requests.Response();response.status_code=value[0] if isinstance(value,tuple) else value
        response._content=(b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
                           b'<opensearch:totalResults>0</opensearch:totalResults><opensearch:startIndex>0</opensearch:startIndex>'
                           b'<opensearch:itemsPerPage>100</opensearch:itemsPerPage></feed>')
        response._content_consumed=True
        if isinstance(value,tuple):response.headers['Retry-After']=value[1]
        return response
    monkeypatch.setattr(requests.Session,'get',get)
    monkeypatch.setattr(module,'sleep',waits.append)
    return calls,waits


def test_transient_503_recovers_without_changing_query(monkeypatch):
    calls,waits=replies(monkeypatch,[503,503,200])
    with module.ArxivSession() as client:response=client.get('https://export.arxiv.org/api/query?original=1')
    assert response.status_code==200 and waits==[15,30]
    assert len(calls)==3 and len({url for url,_ in calls})==1
    assert all(kwargs=={'timeout':(5,20),'allow_redirects':False} for _,kwargs in calls)


@pytest.mark.parametrize('status',[401,403,429,302,404])
def test_refusal_and_nontransient_errors_never_retry(monkeypatch,status):
    calls,waits=replies(monkeypatch,[status])
    with module.ArxivSession() as client:assert client.get('https://export.arxiv.org/api/query').status_code==status
    assert len(calls)==1 and waits==[]


def test_persistent_failure_has_exactly_four_attempts(monkeypatch):
    calls,waits=replies(monkeypatch,[503]*4)
    with module.ArxivSession() as client:assert client.get('https://export.arxiv.org/api/query').status_code==503
    assert len(calls)==4 and waits==[15,30,60]


@pytest.mark.parametrize('error',[requests.Timeout,requests.ConnectionError])
def test_transport_errors_are_bounded(monkeypatch,error):
    calls,waits=replies(monkeypatch,[error('synthetic')]*4)
    with module.ArxivSession() as client:
        with pytest.raises(error):client.get('https://export.arxiv.org/api/query')
    assert len(calls)==4 and waits==[15,30,60]


def test_proxy_refusal_is_not_retried_or_misreported_as_arxiv_outage(monkeypatch):
    calls, waits = replies(monkeypatch, [requests.exceptions.ProxyError('synthetic CONNECT 403')])
    with module.ArxivSession() as client:
        with pytest.raises(requests.exceptions.ProxyError):
            client.get('https://export.arxiv.org/api/query')
    assert len(calls) == 1 and waits == []


@pytest.mark.parametrize('header',['120','invalid','nan'])
def test_retry_after_never_bypassed(monkeypatch,header):
    calls,waits=replies(monkeypatch,[(503,header)])
    with module.ArxivSession() as client:
        with pytest.raises(requests.exceptions.RetryError):client.get('https://export.arxiv.org/api/query')
    assert len(calls)==1 and waits==[]


@pytest.mark.parametrize('header',['45','http-date'])
def test_retry_after_wait_is_respected(monkeypatch,header):
    if header=='http-date':header=format_datetime(datetime.now(timezone.utc)+timedelta(seconds=50))
    calls,waits=replies(monkeypatch,[(503,header),200])
    with module.ArxivSession() as client:assert client.get('https://export.arxiv.org/api/query').status_code==200
    assert len(calls)==2 and 15<=waits[0]<=60
    if header=='45':assert waits==[45]


def test_sdk_has_no_nested_retry_and_smaller_pages():
    with module.api_client() as client:
        assert client.num_retries==0 and client.page_size==100 and client.delay_seconds==3
        assert isinstance(client._session,module.ArxivSession)
        assert client._session.get_adapter('https://export.arxiv.org').max_retries.total==0


def atom_page(index):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom">
    <opensearch:totalResults>2</opensearch:totalResults><opensearch:startIndex>{index-1}</opensearch:startIndex>
    <opensearch:itemsPerPage>100</opensearch:itemsPerPage><entry>
    <id>https://arxiv.org/abs/2610.0000{index}v1</id><title>Paper {index}</title>
    <updated>2026-10-03T00:00:00Z</updated><published>2026-10-03T00:00:00Z</published>
    <summary>Complete original abstract.</summary><author><name>Author</name></author>
    <link href="https://arxiv.org/abs/2610.0000{index}v1" rel="alternate" type="text/html"/>
    <category term="cs.AI"/><arxiv:primary_category term="cs.AI"/>
    </entry></feed>'''.encode()


@pytest.mark.parametrize('persistent',[False,True])
def test_sdk_pagination_retries_same_page_without_partial_delivery(config,monkeypatch,persistent):
    from urllib.parse import urlsplit,parse_qs
    config.source.arxiv.window_days=1
    config.source.arxiv.category=['cs.AI']
    config.preprint_interests=None
    calls=[];waits=[]
    def get(self,url,**kwargs):
        start=int(parse_qs(urlsplit(url).query)['start'][0]);calls.append(start)
        r=requests.Response();r._content_consumed=True
        r.status_code=503 if start==1 and (persistent or calls.count(1)==1) else 200
        r._content=atom_page(start+1) if r.status_code==200 else b''
        return r
    monkeypatch.setattr(requests.Session,'get',get)
    monkeypatch.setattr(module,'sleep',waits.append)
    monkeypatch.setattr(module.arxiv.time,'sleep',lambda _:None)
    retriever=module.ArxivRetriever(config)
    if persistent:
        with pytest.raises(module.arxiv.HTTPError):retriever.retrieve_papers()
        assert calls==[0,1,1,1,1] and waits==[15,30,60]
        assert retriever._raw=={} # No partial page was converted or recommended.
    else:
        papers=retriever.retrieve_papers()
        assert [p.title for p in papers]==['Paper 1','Paper 2']
        assert calls==[0,1,1] and waits==[15]
