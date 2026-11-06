"""Undeployed save service core. Hosting must supply verified server-side sessions.

No HTTP listener, credentials, or library destination is configured by this module.
"""
from dataclasses import dataclass
from contextlib import closing
import hashlib
import hmac
import json
import re
import sqlite3
import time

from .identity import normalize_doi, canonical_doi, paper_id
from .publisher_abstracts import publisher_session, MAX_BYTES
from .zotero_action import confirmation_origin


class SaveError(Exception):
    """Safe public error, never a raw HTTP exception or response body."""


@dataclass(frozen=True)
class VerifiedSession:
    # Construct only from the host's authenticated server-side session store.
    subject: str
    csrf_token: str
    expires_at: float


class ZoteroWriter:
    def __init__(self, key, user_id, collection_key, client=None):
        if not key or not re.fullmatch(r'\d+',user_id) or not re.fullmatch(r'[A-Z0-9]{8}',collection_key):
            raise ValueError('A server-configured credential, user and unique collection key are required')
        self._key=key
        self.user_id=user_id
        self.collection_key=collection_key
        self.client=client or publisher_session()  # No redirects or transport retries.

    def request(self,method,path,*,params=None,data=None,version=None,token=None):
        headers={'Zotero-API-Key':self._key,'Zotero-API-Version':'3'}
        if version is not None:headers['If-Unmodified-Since-Version']=str(version)
        if token is not None:headers['Zotero-Write-Token']=token
        try:
            with self.client.request(method,'https://api.zotero.org'+path,headers=headers,
                    params=params,json=data,timeout=(5,20),allow_redirects=False,stream=True) as response:
                if response.status_code not in (200,204):
                    raise SaveError('Zotero request failed; verify remote state before another write')
                chunks=[];size=0
                for chunk in response.iter_content(65536):
                    size+=len(chunk)
                    if size>MAX_BYTES:raise SaveError('Zotero response exceeds size limit')
                    chunks.append(chunk)
                raw=b''.join(chunks)
                return (json.loads(raw) if raw else None),dict(response.headers)
        except SaveError:
            raise
        except Exception:
            raise SaveError('Zotero response uncertain; reconciliation required') from None

    def find(self,paper):
        doi=normalize_doi(paper.doi)
        if not doi and not paper.url:raise SaveError('Paper has no stable identifier')
        query=canonical_doi(doi) or paper.url
        matches=[];seen=set();library_version=None
        for page in range(20):
            data,headers=self.request('GET',f'/users/{self.user_id}/items',
                params={'q':query,'qmode':'everything','format':'json','limit':100,'start':page*100})
            version=headers.get('Last-Modified-Version')
            if not isinstance(data,list) or version is None or not str(version).isdigit():
                raise SaveError('Cannot verify library search/version')
            if library_version is not None and version!=library_version:
                raise SaveError('Library changed during search; retry confirmation')
            library_version=version
            for item in data:
                key=item.get('key');record=item.get('data') or {}
                if not key or key in seen:raise SaveError('Incomplete item search')
                seen.add(key)
                if record.get('itemType') in ('attachment','note'):continue
                found_doi=canonical_doi(record.get('DOI'))
                if (doi and found_doi==canonical_doi(doi)) or ((not doi or not found_doi) and paper.url and record.get('url')==paper.url):
                    matches.append(item)
            total=headers.get('Total-Results')
            if total is None or not str(total).isdigit():raise SaveError('Missing search count')
            if len(seen)==int(total):break
            if len(data)<100 or len(seen)>int(total):raise SaveError('Incomplete item search')
        else:raise SaveError('Item search limit exceeded')
        if len(matches)>1:raise SaveError('Multiple existing items match; choose one in Zotero')
        return (matches[0] if matches else None),library_version

    def ensure(self,paper,token,allow_write):
        item,library_version=self.find(paper)
        if item:
            data=item['data'];key=item['key']
            if not re.fullmatch(r'[A-Z0-9]{8}',key):raise SaveError('Invalid item identity')
            collections=data.get('collections')
            if not isinstance(collections,list):raise SaveError('Cannot verify existing memberships')
            if self.collection_key in collections:return key
            if not allow_write:raise SaveError('Previous write uncertain; manual reconciliation required')
            version=item.get('version')
            if type(version) is not int:raise SaveError('Missing item version')
            self.request('PATCH',f'/users/{self.user_id}/items/{key}',
                         data={'collections':collections+[self.collection_key]},version=version)
            return key
        if not allow_write:raise SaveError('Previous write uncertain; manual reconciliation required')
        item_type='journalArticle' if paper.journal else 'preprint'
        template,_=self.request('GET','/items/new',params={'itemType':item_type})
        if not isinstance(template,dict) or template.get('itemType')!=item_type:
            raise SaveError('Invalid Zotero item template')
        values={'title':paper.title,'url':paper.url,'abstractNote':paper.abstract or '',
                'DOI':normalize_doi(paper.doi) or '',
                'date':paper.published.strftime('%Y-%m-%d') if paper.published else ''}
        for field,value in values.items():
            if field in template:template[field]=value
        template['creators']=[{'creatorType':'author','name':name} for name in paper.authors]
        template['collections']=[self.collection_key]
        # No attachments, PDF fetches, notes, or client-supplied collection IDs.
        result,_=self.request('POST',f'/users/{self.user_id}/items',data=[template],
                              version=library_version,token=token)
        try:
            key=result['successful']['0']['key']
            if result.get('failed') or not re.fullmatch(r'[A-Z0-9]{8}',key):raise ValueError()
        except (KeyError,TypeError,ValueError):
            raise SaveError('Creation response uncertain; reconciliation required') from None
        return key


class SaveService:
    def __init__(self,writer,database,origin,allowed_subject,papers):
        self.writer=writer;self.database=str(database)
        self.origin=confirmation_origin(origin)
        if not self.origin or not allowed_subject:raise ValueError('Verified hosting identity configuration required')
        self.subject=allowed_subject
        # Only the trusted digest loader supplies papers, never request body metadata.
        self.papers={hashlib.sha256(paper_id(p).encode()).hexdigest():p for p in papers}
        # SQLite's context handles transactions, not connection lifetime.
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS saves (identity TEXT PRIMARY KEY, status TEXT NOT NULL, item_key TEXT)')

    def _authorize(self,session,identifier):
        if not isinstance(session,VerifiedSession) or session.subject!=self.subject or session.expires_at<=time.time():
            raise SaveError('Authentication required')
        if identifier not in self.papers:raise SaveError('Unknown digest paper')
        return self.papers[identifier]

    def preview(self,session,identifier):
        paper=self._authorize(session,identifier)
        # Hosting must escape these values in its authenticated HTML preview.
        return {'title':paper.title,'doi':paper.doi,'url':paper.url}

    def save(self,*,method,origin,session,csrf_token,identifier):
        if method!='POST':raise SaveError('Only an intentional POST can save')
        paper=self._authorize(session,identifier)
        if origin!=self.origin or not isinstance(csrf_token,str) or not session.csrf_token or not hmac.compare_digest(csrf_token,session.csrf_token):
            raise SaveError('Invalid confirmation origin or CSRF token')
        identity=hashlib.sha256('|'.join([self.subject,self.writer.user_id,self.writer.collection_key,paper_id(paper)]).encode()).hexdigest()
        with closing(sqlite3.connect(self.database,timeout=30,isolation_level=None)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status,item_key FROM saves WHERE identity=?',(identity,)).fetchone()
            if row and row[0]=='complete':
                db.commit();return row[1]
            fresh=row is None
            if fresh:
                db.execute('INSERT INTO saves VALUES (?, ?, NULL)',(identity,'pending'))
                db.commit()  # Durable reservation BEFORE any remote write; survives process crash.
                db.execute('BEGIN IMMEDIATE')
            try:
                key=self.writer.ensure(paper,token=identity[:32],allow_write=fresh)
                db.execute('UPDATE saves SET status=?,item_key=? WHERE identity=?',('complete',key,identity))
                db.commit();return key
            except Exception:
                db.execute('UPDATE saves SET status=? WHERE identity=?',('uncertain',identity))
                db.commit()
                raise SaveError('Save not confirmed; remote reconciliation required before another write') from None
