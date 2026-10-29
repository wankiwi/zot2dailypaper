"""Bounded public-abstract retrieval. Raw text stays in memory, never artifacts."""
import hashlib
from html import unescape
from html.parser import HTMLParser
import re
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET

MAX_BYTES=2*1024*1024


class SourceStop(ValueError):pass


def norm(value):
    value=unicodedata.normalize("NFKC",unescape(value)).casefold().replace("&"," and ")
    return " ".join(re.findall(r"\w+",value))


class Page(HTMLParser):
    def __init__(self,text):
        super().__init__(convert_charrefs=True)
        self.lines=[];self.parts=[];self.metas={};self.headings=[];self.h1=[]
        self.in_h1=False;self.skip=0;self.depth=0;self.capture=None;self.abstract=[];self.heading_ends=[]
        self.feed(text);self.flush()
    def flush(self):
        value=" ".join(" ".join(self.parts).split());self.parts=[]
        if value:self.lines.append(value)
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in ('script','style'):self.skip+=1
        if tag=='meta' and a.get('name') and a.get('content'):self.metas[a['name'].casefold()]=a['content']
        if tag=='h1':self.in_h1=True;self.h1=[]
        if tag in ('p','div','section','h1','h2','h3','br'):self.flush()
        if tag=='h1':self.heading_ends.append(len(self.lines)-1)
        if tag not in ('meta','br','img','link','input','hr'):
            self.depth+=1
            if a.get('id')=='Abs1-content':self.capture=self.depth
    def handle_endtag(self,tag):
        if tag in ('script','style') and self.skip:self.skip-=1
        if tag=='h1':
            self.headings.append(' '.join(self.h1));self.in_h1=False
        if tag in ('p','div','section','h1','h2','h3','br'):self.flush()
        if self.capture==self.depth:self.capture=None
        if tag not in ('meta','br','img','link','input','hr'):self.depth=max(0,self.depth-1)
    def handle_data(self,text):
        if self.skip:return
        self.parts.append(text)
        if self.in_h1:self.h1.append(text)
        if self.capture is not None:self.abstract.append(text)


def validated_text(text):
    text=' '.join(text.split())
    if not 150<=len(text.encode('utf-8'))<=12000:raise SourceStop('abstract_missing_short_or_overbound')
    return text


def html_abstract(raw,route,title):
    page=Page(raw);expected=norm(title)
    titles=page.headings+[page.metas.get(k,'') for k in ('citation_title','dc.title','dc.title.alternative')]
    if not any(norm(t)==expected for t in titles):raise SourceStop('source_title_mismatch')
    ident=route['identifier'].casefold()
    if ident not in unescape(raw).casefold():raise SourceStop('source_identifier_mismatch')
    if page.abstract:return validated_text(' '.join(page.abstract))
    matching_ends=[end for heading,end in zip(page.headings,page.heading_ends) if norm(heading)==expected]
    after=max(matching_ends) if matching_ends else -1
    for i,line in enumerate(page.lines):
        if i<=after:continue
        if norm(line)!='abstract':continue
        selected=[]
        for following in page.lines[i+1:]:
            if re.match(route['end_pattern'],following,re.I):break
            selected.append(following)
        else:continue
        try:return validated_text(' '.join(selected))
        except SourceStop:continue
    raise SourceStop('exact_abstract_section_not_found')


def pubmed_abstract(raw,route,title):
    root=ET.fromstring(raw)
    article=root.find('.//PubmedArticle')
    if article is None:raise SourceStop('pubmed_article_missing')
    pmid=article.findtext('./MedlineCitation/PMID')
    identifiers={e.text.strip().casefold() for e in article.findall('./PubmedData/ArticleIdList/ArticleId') if e.get('IdType')=='doi' and e.text}
    actual=''.join(article.find('./MedlineCitation/Article/ArticleTitle').itertext())
    if pmid!='42949622' or '10.1002/anie.3951000' not in identifiers or norm(actual)!=norm(title):
        raise SourceStop('pubmed_identity_mismatch')
    parts=[''.join(e.itertext()) for e in article.findall('./MedlineCitation/Article/Abstract/AbstractText')]
    return validated_text(' '.join(parts))


def fetch(url):
    request=urllib.request.Request(url,headers={'User-Agent':'zot2dailypaper-public-abstract-analysis/2'})
    with urllib.request.urlopen(request,timeout=15) as response:
        raw=response.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise SourceStop('public_response_too_large')
        return raw.decode('utf-8')


def prepare(papers,source_config,get=fetch):
    completed=[];evidence={};total_gets=0
    for original in papers:
        paper={k:original[k] for k in ('id','title','abstract')};key=paper['id']
        if paper['abstract'].strip():
            origin={'kind':'primary_existing_v1_arxiv_abstract','url':source_config[key]['existing_source']}
        else:
            failures=[];origin=None
            for route in source_config[key]['routes']:
                total_gets+=1
                if total_gets>6:raise SourceStop('public_lookup_request_bound_exceeded')
                try:
                    raw=get(route['url'])
                    text=pubmed_abstract(raw,route,paper['title']) if route['format']=='pubmed_xml' else html_abstract(raw,route,paper['title'])
                    paper['abstract']=text
                    origin={'kind':route['provenance'],'url':route['url'],'identity_checked':route['identifier'],
                            'license':route['license'],'attempts':total_gets}
                    break
                except Exception as error:
                    failures.append({'url':route['url'],'error_class':type(error).__name__,
                                     'http_status':getattr(error,'code',None),
                                     'reason':str(error) if isinstance(error,SourceStop) else str(getattr(error,'reason',''))[:200]})
            if origin is None:
                evidence[key]={'evidence_status':'insufficient_evidence','relevance_score':None,
                               'reason':'original_abstract_not_obtained','attempts':failures}
                completed.append(paper);continue
        raw=paper['abstract'].encode('utf-8')
        evidence[key]={'evidence_status':'abstract_available','source':origin,
                       'abstract_sha256':hashlib.sha256(raw).hexdigest(),'abstract_bytes':len(raw)}
        completed.append(paper)
    ready=all(e['evidence_status']=='abstract_available' for e in evidence.values())
    return completed,evidence,ready
