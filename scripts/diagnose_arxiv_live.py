"""One-off real arXiv check on a dedicated Actions branch; never run the app."""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import smtplib
import subprocess
import time
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import httpx
from omegaconf import OmegaConf
import requests

from zot2dailypaper.identity import deduplicate, paper_id
from zot2dailypaper.protocol import Paper
from zot2dailypaper.retriever import arxiv_retriever as module


PRODUCTION_BASE = 'bfc898ccf10cea7a5477d3796cd64cb84e5de319'
BRANCH = 'refs/heads/diagnostics/arxiv-live-20261020'
ROOT = Path(__file__).resolve().parents[1]
MAX_REQUESTS, MAX_RECORDS, MAX_SECONDS = 80, 6000, 720


def forbidden(*args, **kwargs):
    raise RuntimeError('SMTP, model calls, Zotero and delivery-state operations are disabled')


def allowed_request(method, url, redirects):
    parsed = urlsplit(url)
    return (method == 'GET' and parsed.scheme == 'https' and parsed.hostname == 'export.arxiv.org'
            and parsed.path == '/api/query' and parsed.username is None and not redirects)


def page_facts(response):
    facts = {'http': response.status_code, 'bytes': len(response.content),
             'content_type': response.headers.get('Content-Type')}
    if response.status_code != 200:
        return facts, []
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        return dict(facts, atom=False, xml_valid=False), []
    entries = root.findall(module.ATOM + 'entry')
    facts.update(atom=root.tag == module.ATOM + 'feed', xml_valid=True, entries=len(entries))
    for field in ('totalResults', 'startIndex', 'itemsPerPage'):
        text = root.findtext(module.OPENSEARCH + field)
        facts[field] = text.strip() if text else None
    samples = [{'id': e.findtext(module.ATOM + 'id'), 'title': e.findtext(module.ATOM + 'title')}
               for e in entries[:3]]
    return facts, samples


class Observer:
    def __init__(self):
        self.started = time.monotonic()
        self.last_request = None
        self.requests = 0
        self.window = None
        self.pages = []
        self.samples = []
        self.send = requests.Session.send

    def observe(self, session, request, **kwargs):
        if not allowed_request(request.method, request.url, kwargs.get('allow_redirects', True)):
            raise RuntimeError('Diagnostic permits only non-redirecting HTTPS GET to the official arXiv query API')
        if self.requests >= MAX_REQUESTS or time.monotonic() - self.started >= MAX_SECONDS:
            raise RuntimeError('Diagnostic request/time bound reached; completeness not verified')
        if self.last_request is not None:
            time.sleep(max(0, 3 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.requests += 1
        response = self.send(session, request, **kwargs)
        facts, samples = page_facts(response)
        facts.update(window_days=self.window, request=self.requests,
                     requested_start=parse_qs(urlsplit(request.url).query).get('start', ['0'])[0])
        self.pages.append(facts)
        if samples and not self.samples:
            self.samples = samples
        print('LIVE_PAGE ' + json.dumps(facts, ensure_ascii=False), flush=True)
        total = facts.get('totalResults')
        if total and total.isdecimal() and int(total) > MAX_RECORDS:
            raise RuntimeError('Diagnostic record bound reached; refusing a larger harvest')
        return response


def run_window(days, categories, include_cross_list, observer):
    config = OmegaConf.create({
        'source': {'arxiv': {'window_days': days, 'include_cross_list': include_cross_list}},
        'preprint_interests': {'arxiv': {'categories': categories}},
        'executor': {'debug': False, 'source': ['arxiv']},
        'state': {'enabled': False}, 'llm': {'enabled': False},
        'output': {'email': {'enabled': False}, 'rss': {'enabled': False}},
    })
    observer.window = days
    first = len(observer.pages)
    source = module.ArxivRetriever(config)
    try:
        papers = source.retrieve_papers()
    except Exception as exc:
        return {'window_days': days, 'status': 'failed', 'error_type': type(exc).__name__,
                'error': str(exc)[:500], 'pages': observer.pages[first:], 'raw_samples': observer.samples}
    unique = deduplicate(papers)
    fields = {'title': lambda p: bool(p.title.strip()), 'abstract': lambda p: bool(p.abstract.strip()),
              'authors': lambda p: bool(p.authors) and all(a.strip() for a in p.authors),
              'url': lambda p: urlsplit(p.url).scheme in ('http', 'https') and bool(urlsplit(p.url).netloc),
              'pdf_url': lambda p: bool(p.pdf_url), 'published': lambda p: p.published is not None}
    result = {'window_days': days, 'status': 'complete', 'pages': observer.pages[first:],
              'api_entries': source._api_count, 'primary_category_kept': source._category_count,
              'converted': source._converted_count, 'keyword_kept': len(papers),
              'unique_after_dedup': len(unique), 'duplicate_identities': len(papers) - len({paper_id(p) for p in papers}),
              'missing_fields': {name: sum(not check(p) for p in papers) for name, check in fields.items()},
              'full_text_requested': False, 'full_text_present': sum(bool(p.full_text) for p in papers)}
    if days == 7:
        result['samples'] = [{'id': p.url.rsplit('/', 1)[-1], 'title': p.title, 'url': p.url} for p in papers[:3]]
    return result


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'wankiwi/zot2dailypaper'
            or os.environ.get('GITHUB_REF') != BRANCH or os.environ.get('GITHUB_EVENT_NAME') != 'push'):
        raise RuntimeError('Run only through the dedicated read-only GitHub Actions diagnostic branch')
    credentials = ('OPENAI_API_KEY', 'ZOTERO_KEY', 'ZOTERO_ID', 'SENDER', 'SENDER_PASSWORD', 'RECEIVER')
    if any(name in os.environ for name in credentials):
        raise RuntimeError('Application credential variable present; refusing diagnostic execution')
    subprocess.run(['git', 'diff', '--no-ext-diff', '--quiet', PRODUCTION_BASE, '--',
                    'src', 'config', 'uv.lock', 'pyproject.toml'], cwd=ROOT, check=True)
    preferences = OmegaConf.load(ROOT / 'config/interests.yaml').preprint_interests.arxiv
    categories = list(preferences.categories)
    cross_list = OmegaConf.load(ROOT / 'config/base.yaml').source.arxiv.include_cross_list
    until = datetime.now(timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return until

    module.datetime = Clock
    smtplib.SMTP = smtplib.SMTP_SSL = forbidden
    httpx.Client.send = httpx.AsyncClient.send = forbidden
    Paper.generate_tldr = Paper.generate_affiliations = forbidden
    module.extract_text_from_html = module.extract_text_from_pdf = module.extract_text_from_tar = forbidden
    from zot2dailypaper.state import State
    State.add = State.mark = State.save = forbidden
    observer = Observer()

    def send(session, request, **kwargs):
        return observer.observe(session, request, **kwargs)

    requests.Session.send = send
    report = {'production_source_base': PRODUCTION_BASE, 'production_source_unchanged': True,
              'until_utc': until.isoformat(), 'categories': categories, 'include_cross_list': bool(cross_list),
              'permissions': 'contents:read', 'smtp': False, 'llm': False, 'zotero': False,
              'delivery_state_write': False, 'full_text': False, 'windows': []}
    for days in (1, 7):
        result = run_window(days, categories, cross_list, observer)
        report['windows'].append(result)
        print('LIVE_WINDOW ' + json.dumps(result, ensure_ascii=False), flush=True)
        if result['status'] != 'complete':
            break  # No second query after a denial, exhausted upstream failure or invalid response.
    report['http_status_counts'] = dict(Counter(str(p['http']) for p in observer.pages))
    seven = next((r for r in report['windows'] if r['window_days'] == 7), None)
    report['nonempty_seven_day_retrieval_verified'] = bool(seven and seven['status'] == 'complete' and seven['keyword_kept'])
    print('LIVE_REPORT ' + json.dumps(report, ensure_ascii=False), flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as output:
            output.write('Read-only real arXiv retrieval diagnostic\n\n```json\n' + json.dumps(report, ensure_ascii=False, indent=2) + '\n```\n')
    return 0 if len(report['windows']) == 2 and all(r['status'] == 'complete' for r in report['windows']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
