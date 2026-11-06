import hashlib
import time
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
import pytest
from zot2dailypaper.zotero_save import SaveService, SaveError, VerifiedSession, ZoteroWriter
from zot2dailypaper.identity import paper_id
from tests.canned_responses import make_sample_paper


@pytest.fixture(autouse=True)
def closed_sqlite_connections(monkeypatch):
    """Keep real connections alive so GC cannot hide a missing close."""
    connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, 'connect', tracked_connect)
    yield connections
    unclosed = []
    for db in connections:
        try:
            db.in_transaction
        except sqlite3.ProgrammingError as error:
            assert 'closed' in str(error)
        else:
            unclosed.append(db)
        finally:
            db.close()  # Also clean up if this regression fails.
    assert not unclosed, f'{len(unclosed)} SQLite connections were not closed'


def service(tmp_path,writer=None):
    paper=make_sample_paper(doi='10.1000/synthetic',url='https://example.org/paper',abstract='Original abstract')
    writer=writer or SimpleNamespace(user_id='123',collection_key='TARGET01',ensure=lambda *a,**kw:'ITEM0001')
    svc=SaveService(writer,tmp_path/'saves.sqlite','https://papers.example.org','owner',[paper])
    session=VerifiedSession('owner','random-session-csrf-token',time.time()+300)
    args={'method':'POST','origin':svc.origin,'session':session,'csrf_token':session.csrf_token,
          'identifier':hashlib.sha256(paper_id(paper).encode()).hexdigest()}
    return svc,args,paper

@pytest.mark.parametrize('change',[{'method':'GET'},{'method':'HEAD'},{'origin':'https://evil.example'},
 {'csrf_token':'wrong'},{'session':None},{'session':VerifiedSession('other','x',9999999999)},
 {'session':VerifiedSession('owner','x',0)},{'identifier':'unknown'}])
def test_unauthorized_or_scanner_requests_cannot_write(tmp_path,change):
    writer=SimpleNamespace(user_id='123',collection_key='TARGET01',ensure=lambda *a,**kw:pytest.fail('unexpected remote call'))
    svc,args,_=service(tmp_path,writer)
    with pytest.raises(SaveError):svc.save(**(args|change))


def test_preview_is_read_only_and_duplicate_post_is_durable(tmp_path):
    calls=[]
    writer=SimpleNamespace(user_id='123',collection_key='TARGET01',ensure=lambda p,**kw:calls.append(kw) or 'ITEM0001')
    svc,args,paper=service(tmp_path,writer)
    assert svc.preview(args['session'],args['identifier'])['title']==paper.title
    assert calls==[]
    assert svc.save(**args)=='ITEM0001'
    reopened,_,_=service(tmp_path,writer)
    assert reopened.save(**args)=='ITEM0001' and len(calls)==1
    assert calls[0]['allow_write'] and len(calls[0]['token'])==32


def test_timeout_never_blindly_repeats_write(tmp_path):
    calls=[]
    def ensure(p,**kwargs):
        calls.append(kwargs)
        if kwargs['allow_write']:raise TimeoutError('secret must never escape')
        return 'ITEM0001'  # Reconciled as already present remotely.
    svc,args,_=service(tmp_path,SimpleNamespace(user_id='123',collection_key='TARGET01',ensure=ensure))
    with pytest.raises(SaveError,match='reconciliation') as exc:svc.save(**args)
    assert 'secret' not in str(exc.value)
    assert svc.save(**args)=='ITEM0001'
    assert [c['allow_write'] for c in calls]==[True,False]


def saved_rows(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute('SELECT status,item_key FROM saves').fetchall()


def inject_sql_failure(monkeypatch, predicate):
    connect = sqlite3.connect

    class FaultyConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if predicate(sql, args):
                raise sqlite3.OperationalError('synthetic database failure')
            return super().execute(sql, *args, **kwargs)

    def faulty_connect(*args, **kwargs):
        return connect(*args, factory=FaultyConnection, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', faulty_connect)


def test_schema_failure_closes_connection(tmp_path, monkeypatch):
    inject_sql_failure(monkeypatch, lambda sql, args: sql.startswith('CREATE TABLE'))
    with pytest.raises(sqlite3.OperationalError, match='synthetic'):
        service(tmp_path)


def test_query_failure_rolls_back_without_remote_write(tmp_path, monkeypatch):
    writer = SimpleNamespace(user_id='123', collection_key='TARGET01',
                             ensure=lambda *a, **kw: pytest.fail('unexpected remote call'))
    svc, args, _ = service(tmp_path, writer)
    inject_sql_failure(monkeypatch, lambda sql, args: sql.startswith('SELECT status,item_key FROM saves WHERE'))
    with pytest.raises(sqlite3.OperationalError, match='synthetic'):
        svc.save(**args)
    assert saved_rows(svc.database) == []
    with closing(sqlite3.connect(svc.database, timeout=0)) as db, db:
        db.execute('BEGIN IMMEDIATE')  # The failed operation released its lock.


def test_pending_reservation_is_durable_before_remote_write(tmp_path):
    calls = []

    def ensure(paper, **kwargs):
        calls.append(kwargs)
        assert saved_rows(tmp_path / 'saves.sqlite') == [('pending', None)]
        return 'ITEM0001'

    writer = SimpleNamespace(user_id='123', collection_key='TARGET01', ensure=ensure)
    svc, args, _ = service(tmp_path, writer)
    assert svc.save(**args) == 'ITEM0001'
    assert saved_rows(svc.database) == [('complete', 'ITEM0001')]
    assert svc.save(**args) == 'ITEM0001'
    assert len(calls) == 1


@pytest.mark.parametrize('error_type,status', [(TimeoutError, 'uncertain'), (KeyboardInterrupt, 'pending')])
def test_remote_failure_closes_and_preserves_reconciliation(tmp_path, error_type, status):
    calls = []

    def ensure(paper, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise error_type('synthetic interruption')
        return 'ITEM0001'

    writer = SimpleNamespace(user_id='123', collection_key='TARGET01', ensure=ensure)
    svc, args, _ = service(tmp_path, writer)
    expected = SaveError if issubclass(error_type, Exception) else error_type
    with pytest.raises(expected):
        svc.save(**args)
    assert saved_rows(svc.database) == [(status, None)]
    assert svc.save(**args) == 'ITEM0001'
    assert [call['allow_write'] for call in calls] == [True, False]
    assert saved_rows(svc.database) == [('complete', 'ITEM0001')]


@pytest.mark.parametrize('fail_uncertain', [False, True])
def test_receipt_failure_preserves_reservation_and_closes(tmp_path, monkeypatch, fail_uncertain):
    calls = []
    writer = SimpleNamespace(user_id='123', collection_key='TARGET01',
                             ensure=lambda *a, **kw: calls.append(kw) or 'ITEM0001')
    svc, args, _ = service(tmp_path, writer)
    with monkeypatch.context() as patch:
        inject_sql_failure(patch, lambda sql, args: sql.startswith('UPDATE saves SET status=')
                           and (fail_uncertain or args[0][0] == 'complete'))
        expected = sqlite3.OperationalError if fail_uncertain else SaveError
        with pytest.raises(expected):
            svc.save(**args)
    assert saved_rows(svc.database) == [('pending' if fail_uncertain else 'uncertain', None)]
    assert svc.save(**args) == 'ITEM0001'
    assert [call['allow_write'] for call in calls] == [True, False]
    assert saved_rows(svc.database) == [('complete', 'ITEM0001')]


class Response:
    def __init__(self,data,status=200,headers=None):self.data=data;self.status_code=status;self.headers=headers or {}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_content(self,n):
        if self.data is not None:yield json.dumps(self.data).encode()

class Client:
    def __init__(self,responses):self.responses=iter(responses);self.calls=[]
    def request(self,method,url,**kwargs):
        assert 'secret-key' not in url and url.startswith('https://api.zotero.org/')
        assert kwargs['allow_redirects'] is False
        self.calls.append((method,url,kwargs))
        return next(self.responses)


def item(collections):
    return {'key':'ITEM0001','version':4,'data':{'itemType':'preprint','DOI':'10.1000/synthetic','collections':collections}}

@pytest.mark.parametrize('collections,expected_methods',[(['TARGET01'],['GET']),(['OTHER001'],['GET','PATCH'])])
def test_existing_item_memberships_preserved(collections,expected_methods):
    client=Client([Response([item(collections)],headers={'Last-Modified-Version':'8','Total-Results':'1'}),Response(None,204)])
    writer=ZoteroWriter('secret-key','123','TARGET01',client)
    p=make_sample_paper(doi='10.1000/synthetic')
    assert writer.ensure(p,'x'*32,True)=='ITEM0001'
    assert [c[0] for c in client.calls]==expected_methods
    if len(client.calls)>1:
        assert client.calls[1][2]['json']=={'collections':['OTHER001','TARGET01']}
        assert client.calls[1][2]['headers']['If-Unmodified-Since-Version']=='4'


def test_create_citation_original_url_only_with_conflict_guard():
    template={'itemType':'preprint','title':'','url':'','abstractNote':'','DOI':'','date':'','creators':[],'collections':[]}
    client=Client([Response([],headers={'Last-Modified-Version':'8','Total-Results':'0'}),Response(template),
                   Response({'successful':{'0':{'key':'ITEM0001'}},'failed':{}})])
    writer=ZoteroWriter('secret-key','123','TARGET01',client)
    p=make_sample_paper(doi='10.1000/synthetic',journal=None,pdf_url='https://example.org/paper.pdf')
    assert writer.ensure(p,'x'*32,True)=='ITEM0001'
    assert [c[0] for c in client.calls]==['GET','GET','POST']
    post=client.calls[-1][2]
    assert post['headers']['If-Unmodified-Since-Version']=='8'
    assert post['headers']['Zotero-Write-Token']=='x'*32
    assert post['json'][0]['url']==p.url and post['json'][0]['collections']==['TARGET01']
    assert p.pdf_url not in json.dumps(client.calls)


def test_ambiguous_existing_matches_never_write():
    other=item([]);other['key']='ITEM0002'
    client=Client([Response([item([]),other],headers={'Last-Modified-Version':'8','Total-Results':'2'})])
    writer=ZoteroWriter('secret-key','123','TARGET01',client)
    with pytest.raises(SaveError,match='Multiple'):writer.ensure(make_sample_paper(doi='10.1000/synthetic'),'x'*32,True)
    assert len(client.calls)==1


def test_uncertain_no_match_refuses_second_creation():
    client=Client([Response([],headers={'Last-Modified-Version':'8','Total-Results':'0'})])
    writer=ZoteroWriter('secret-key','123','TARGET01',client)
    with pytest.raises(SaveError,match='uncertain'):writer.ensure(make_sample_paper(doi='10.1000/synthetic'),'x'*32,False)
    assert len(client.calls)==1

@pytest.mark.parametrize('status',[301,401,403,409,412,429,500])
def test_http_failures_do_not_retry_or_leak(status):
    client=Client([Response({'secret':'do not expose'},status)])
    writer=ZoteroWriter('secret-key','123','TARGET01',client)
    with pytest.raises(SaveError) as exc:writer.request('POST','/users/123/items',data=[])
    assert len(client.calls)==1 and 'secret' not in str(exc.value)
