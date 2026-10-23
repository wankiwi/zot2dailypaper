"""Bounded, authenticated source-only verification; no recommendation state or delivery."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from loguru import logger
from omegaconf import OmegaConf
import requests
from requests.adapters import HTTPAdapter

from zot2dailypaper.identity import paper_id
from zot2dailypaper.retriever.openalex_researchsquare import OpenAlexResearchSquare, get_json


ROOT = Path(__file__).resolve().parents[1]
REF = 'refs/heads/diagnostics/openalex-auth-20261023'
MAX_REQUESTS = 24


class AuditSession(requests.Session):
    def __init__(self, key, audit):
        super().__init__()
        self.key, self.audit = key, audit
        self.trust_env = False  # No .netrc, proxy identity or ambient credentials.
        self.headers['User-Agent'] = 'zot2dailypaper-source-verification/1.0'
        self.mount('https://api.openalex.org/', HTTPAdapter(max_retries=0))

    def send(self, request, **kwargs):
        target = urlsplit(request.url)
        if (request.method != 'GET' or target.scheme != 'https' or target.netloc != 'api.openalex.org'
                or not re.fullmatch(r'/(?:works|subfields|sources/S\d+)', target.path)
                or kwargs.get('allow_redirects', True)
                or request.headers.get('Authorization') != 'Bearer ' + self.key
                or 'api_key=' in target.query or self.key in request.url):
            raise RuntimeError('Read-only OpenAlex request guard rejected an unexpected request')
        if self.audit['requests'] >= MAX_REQUESTS:
            raise RuntimeError('OpenAlex diagnostic request bound reached; no retry')
        self.audit['requests'] += 1
        response = super().send(request, **kwargs)
        logger.info(f'OpenAlex read-only request {self.audit["requests"]}: '
                    f'phase={self.audit["phase"]}, endpoint={target.path}, HTTP={response.status_code}')
        return response

    def get(self, url, **kwargs):
        response = super().get(url, **kwargs)
        if response.status_code == 200 and urlsplit(url).path == '/works':
            data, params = response.json(), kwargs['params']
            self.audit['filter'] = params['filter']
            self.audit['pages'].append({'phase': self.audit['phase'], 'http_status': 200,
                'per_page': params['per_page'], 'cursor': 'initial' if params['cursor'] == '*' else 'continuation',
                'entries': len(data['results']), 'total': int(data['meta']['count'])})
        return response


def converted_stats(records, filters, *, require_unique=True):
    scope = dict(part.split(':', 1) for part in filters.split(','))
    source_ids = set(scope['locations.source.id'].split('|'))
    subfields = set(scope['topics.subfield.id'].split('|'))
    papers = []
    for record in records:
        sources = {(entry.get('source') or {}).get('id', '').rsplit('/', 1)[-1] for entry in record['locations']}
        fields = {(entry.get('subfield') or {}).get('id', '').rsplit('/', 1)[-1] for entry in record['topics']}
        if (record['type'] != 'preprint' or not sources.intersection(source_ids)
                or not fields.intersection(subfields)
                or not scope['from_publication_date'] <= record['publication_date'] <= scope['to_publication_date']):
            raise RuntimeError('OpenAlex verification found an out-of-scope record')
        paper = OpenAlexResearchSquare.convert(record)
        if paper is not None:
            if (not paper.title or not paper.url.startswith('https://') or paper.published is None
                    or (paper.doi and not paper.doi.startswith('10.21203/'))):
                raise RuntimeError('OpenAlex converted citation fields failed verification')
            papers.append(paper)
    unique = len({paper_id(paper) for paper in papers})
    if require_unique and unique != len(papers):
        raise RuntimeError('OpenAlex converted citations contain duplicate identities')
    return {'retained_records': len(records), 'converted': len(papers),
        'unique_identities': unique,
        'excluded_withdrawn_or_untitled': len(records) - len(papers),
        'titles_populated': sum(bool(p.title) for p in papers),
        'authors_populated': sum(bool(p.authors) for p in papers),
        'abstracts_populated': sum(bool(p.abstract) for p in papers),
        'doi_populated': sum(bool(p.doi) for p in papers),
        'https_links': sum(p.url.startswith('https://') for p in papers),
        'dates_populated': sum(p.published is not None for p in papers)}


def diagnose(options):
    key = os.environ.get('OPENALEX_API_KEY', '')
    if not key:
        logger.error('OPENALEX_API_KEY is missing; no anonymous diagnostic fallback')
        raise RuntimeError('OPENALEX_API_KEY must be configured and activated; no anonymous diagnostic fallback')
    audit = {'requests': 0, 'phase': 'source_window', 'pages': [], 'filter': None}
    factory = lambda: AuditSession(key, audit)
    options = OmegaConf.merge(options, {'window_days': 1, 'max_pages': 10})
    retriever = OpenAlexResearchSquare(options)
    records = retriever.retrieve(session_factory=factory)
    if retriever.failures or not audit['filter']:
        raise RuntimeError('Source verification did not establish complete coverage')
    filters = audit['filter']
    source_stats = converted_stats(records, filters)
    window_pages = list(audit['pages'])
    # Reuse verified source/subfield filters, sampling the exact failed date window.
    historical = re.sub(r'from_publication_date:[^,]+', 'from_publication_date:2026-10-21', filters)
    historical = re.sub(r'to_publication_date:[^,]+', 'to_publication_date:2026-10-22', historical)
    audit['phase'] = 'continuation_probe'
    with factory() as client:
        first = get_json(client, '/works', {'filter': historical, 'per_page': 1, 'cursor': '*'})
        cursor = first['meta'].get('next_cursor')
        if int(first['meta']['count']) < 2 or len(first['results']) != 1 or not cursor or cursor == '*':
            raise RuntimeError('Historical sample cannot establish a real cursor continuation')
        second = get_json(client, '/works', {'filter': historical, 'per_page': 1, 'cursor': cursor})
        if len(second['results']) != 1:
            raise RuntimeError('Cursor continuation did not return the expected citation sample')
    if first['results'][0]['id'] == second['results'][0]['id']:
        raise RuntimeError('Cursor continuation repeated the first work ID')
    probe_stats = converted_stats(first['results'] + second['results'], historical, require_unique=False)
    if not probe_stats['converted'] and not source_stats['converted']:
        raise RuntimeError('Source and cursor samples did not convert any valid citations')
    scope = dict(part.split(':', 1) for part in filters.split(','))
    return {'source_commit': os.environ.get('GITHUB_SHA'), 'authentication': 'activated-key requests accepted',
        'requests': audit['requests'], 'source_window': {'from': scope['from_publication_date'], 'to': scope['to_publication_date'],
            'api_entries': sum(p['entries'] for p in window_pages), 'pages': window_pages, **source_stats},
        'continuation_probe': {'from': '2026-10-21', 'to': '2026-10-22', 'full_retrieval': False,
            'pages': [p for p in audit['pages'] if p['phase'] == 'continuation_probe'], **probe_stats},
        'full_text_checked': False, 'mail_model_and_production_state_operations': 0}


def main():
    if (os.environ.get('GITHUB_REPOSITORY') != 'wankiwi/zot2dailypaper'
            or os.environ.get('GITHUB_EVENT_NAME') != 'push' or os.environ.get('GITHUB_REF') != REF):
        raise RuntimeError('Use only the authorized read-only diagnostic branch')
    if any(os.environ.get(name) for name in ('OPENAI_API_KEY', 'ZOTERO_KEY', 'SENDER_PASSWORD', 'ZOTERO_WORKER_D1_TOKEN')):
        raise RuntimeError('Only the OpenAlex credential may be injected into this diagnostic')
    base = OmegaConf.load(ROOT / 'config/base.yaml').source.researchsquare
    interests = OmegaConf.load(ROOT / 'config/interests.yaml').preprint_interests.researchsquare
    try:
        report = diagnose(OmegaConf.merge(base, interests))
    except Exception as error:
        logger.error(f'OpenAlex source verification failed ({type(error).__name__}); inspect fixed HTTP stages above')
        raise SystemExit(1) from None
    output = Path('artifacts/openalex-source-verification.json')
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
