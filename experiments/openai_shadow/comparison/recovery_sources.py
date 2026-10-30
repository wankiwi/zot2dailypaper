"""Refetch only the fixed public records; any abstract drift stops before payment."""
import json
import re
import urllib.request
from urllib.parse import quote,urlsplit,unquote
from time import monotonic,sleep
import logic as L

MAX_BYTES=2_000_000
_last_crossref_start=0.0
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def fetch(url):
    global _last_crossref_start
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.hostname not in ('api.crossref.org','api.biorxiv.org') or parts.username or parts.password:raise L.Stop('unapproved_public_source_endpoint')
    if parts.hostname=='api.crossref.org':
        delay=1.0-(monotonic()-_last_crossref_start)
        if delay>0:sleep(delay)
        _last_crossref_start=monotonic()
    request=urllib.request.Request(url,headers={'User-Agent':'zot2dailypaper-isolated-recovery/1.0','Accept':'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request,timeout=30) as response:
        raw=response.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise L.Stop('public_metadata_response_too_large')
    return json.loads(raw)

def source_doi(row):
    if row['source']=='chemrxiv':
        path=urlsplit(row['abstract_provenance']['url']).path
        if not path.startswith('/works/'):raise L.Stop('original_crossref_provenance_missing')
        doi=unquote(path[len('/works/'):])
    else:doi=row['canonical_id']
    if L.prior.canonical(doi)!=row['canonical_id'] or not re.fullmatch(r'10\.[0-9]{4,9}/[^\s?#]+',doi):raise L.Stop('original_source_doi_mismatch')
    return doi

def resolve(row,get=fetch):
    from zot2dailypaper.abstracts import clean_abstract
    from zot2dailypaper.identity import title_key
    doi=source_doi(row)
    if row['source'] in ('journals','chemrxiv'):
        url='https://api.crossref.org/works/'+quote(doi,safe='')
        item=get(url)['message']
        if L.prior.canonical(item.get('DOI',''))!=row['canonical_id']:raise L.Stop('refetched_doi_mismatch')
        values=[(' '.join(item.get('title') or []),item.get('abstract'),url)]
    elif row['source']=='biorxiv':
        url='https://api.biorxiv.org/details/biorxiv/'+quote(doi,safe='/')
        data=get(url);values=[]
        for item in data.get('collection',[]):
            if L.prior.canonical(item.get('doi',''))==row['canonical_id']:values.append((item.get('title',''),item.get('abstract'),url))
    else:raise L.Stop('frozen_source_not_supported_no_replacement')
    for title,raw,url in values:
        abstract=clean_abstract(raw)
        if title_key(clean_abstract(title))!=title_key(row['title']):continue
        if L.prior.text_sha(abstract)!=row['abstract_sha256'] or len(abstract.encode())!=row['abstract_bytes']:continue
        return dict(row,abstract=abstract,recovery_source_url=url)
    raise L.Stop('original_abstract_or_title_drift_no_replacement')

def resolve_all(rows,get=fetch):
    resolved=[];failures=[]
    for row in rows:
        try:resolved.append(resolve(row,get))
        except Exception as error:
            reason=re.sub(r'(?<=://)[^/@\s]+@','[redacted]@',str(error))[:600]
            failures.append({'paper_id':row['id'],'error_class':type(error).__name__,'reason':reason,'http_status':getattr(error,'code',None)})
    return resolved,failures
