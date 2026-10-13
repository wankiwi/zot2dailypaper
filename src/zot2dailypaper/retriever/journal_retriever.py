"""Publisher feeds plus precise, paginated Crossref journal queries."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from ..identity import crossref_equivalent_dois
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from html import unescape
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit

import feedparser
from loguru import logger

from .base import BaseRetriever, register_retriever
from ..http import session
from ..identity import deduplicate, normalize_doi
from ..journals import Journal, discover_nature, selected_journals
from ..protocol import Paper
from ..selection import is_cover_title
from ..metadata import crossref_metadata
from ..abstracts import clean_abstract

def clean_text(value):
    return ' '.join(unescape(re.sub(r'<[^>]+>', ' ', value or '')).split())


def rss_abstract(value, title):
    text = clean_abstract(value)
    # Nature feeds may supply only a publication notice and repeated title.
    # Strip this precise boilerplate rather than labeling it scientific evidence.
    text, count = re.subn(r'^.{1,100}, Published online: [^;]+; doi:\s*10\.\S+\s*', '', text)
    if count and text.startswith(title):
        text = text[len(title):].strip()
    return text


def crossref_date(item):
    # created/indexed are metadata timestamps, not publication dates.
    for field in ('published-online', 'published-print', 'published', 'issued'):
        parts = item.get(field, {}).get('date-parts', [])
        if parts and parts[0]:
            date = parts[0]
            try:
                return datetime(date[0], date[1] if len(date) > 1 else 1,
                                date[2] if len(date) > 2 else 1, tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


@register_retriever('journals')
class JournalRetriever(BaseRetriever):
    def __init__(self, config):
        super().__init__(config)
        self.cache_path = Path(self.retriever_config.get('catalog_cache', 'data/journal_catalog.json'))
        self.timeout = (10, 30)
        self.failures = []

    def _catalog(self):
        cached, discovered = {}, {}
        if self.cache_path.exists():
            try:
                cached = json.loads(self.cache_path.read_text())
                if not isinstance(cached, dict):
                    raise ValueError('catalogue must be an object')
                updated = cached.get('updated', 0)
                if not isinstance(updated, (int, float)) or not math.isfinite(updated):
                    raise ValueError('invalid catalogue timestamp')
                discovered = {key: Journal(value['id'], value['title'], tuple(value.get('issns', [])), value.get('rss'))
                              for key, value in cached.get('nature', {}).items()}
                for key, value in cached.get('issns', {}).items():
                    Journal(key, key, tuple(value))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                logger.warning(f'Ignoring invalid journal catalogue cache; rebuilding: {exc}')
                cached, discovered = {}, {}
        needs_nature = 'nature_family' in self.retriever_config.get('presets', [])
        if needs_nature and self.retriever_config.get('discover_nature', True) and time.time() - cached.get('updated', 0) > 7 * 86400:
            try:
                with session() as client:
                    response = client.get('https://www.nature.com/siteindex', timeout=self.timeout)
                    response.raise_for_status()
                    discovered = discover_nature(response.text)
                cached['updated'] = time.time()
                cached['nature'] = {k: asdict(v) for k, v in discovered.items()}
            except Exception as exc:
                logger.warning(f'Cannot refresh Nature catalogue; using cached/bootstrap list: {exc}')
                if self.retriever_config.get('require_live_catalog', False):
                    raise
        journals = selected_journals(self.retriever_config, discovered)
        if not journals:
            raise ValueError('At least one journal preset or custom journal must be selected')
        self.cache = cached
        # Explicit preset/custom ISSNs outrank discoveries cached for an old config.
        return [replace(j, issns=j.issns or tuple(cached.get('issns', {}).get(j.id, ()))) for j in journals]

    def _resolve_issns(self, journal, client):
        if journal.issns:
            return journal
        # Nature's official journal footer exposes its ISSN without a fuzzy
        # Crossref title search. Cache it through the existing catalogue path.
        if journal.rss and urlsplit(journal.rss).hostname == 'www.nature.com':
            path = urlsplit(journal.rss).path
            if re.fullmatch(r'/[a-z][a-z0-9-]*\.rss', path):
                homepage = 'https://www.nature.com/' + path[1:-4] + '/'
                try:
                    page = client.get(homepage, timeout=self.timeout)
                    page.raise_for_status()
                    text = clean_text(page.text)
                    if clean_text(journal.title).casefold() in text.casefold():
                        found = tuple(dict.fromkeys(re.findall(r'ISSN(?:\s*\(International Standard Serial Number\))?\s+(\d{4}-\d{3}[\dX])\s*\((?:online|print)\)', text, re.I)))
                        if found:
                            return replace(journal, issns=found)
                except Exception as exc:
                    logger.warning(f'{journal.title}: publisher ISSN lookup unavailable: {type(exc).__name__}')
        response = client.get('https://api.crossref.org/journals', params={'query': journal.title, 'rows': 20}, timeout=self.timeout)
        response.raise_for_status()
        normalize = lambda s: re.sub(r'[^a-z0-9]', '', clean_text(s).lower().replace('&', ' and '))
        for item in response.json()['message']['items']:
            if normalize(item.get('title', '')) == normalize(journal.title) and item.get('ISSN'):
                return replace(journal, issns=tuple(item['ISSN']))
        raise ValueError(f'No exact Crossref ISSN match for {journal.title}')

    def _crossref(self, journal, client, since, until):
        results = []
        for issn in journal.issns:
            cursor = '*'
            seen_cursors = set()
            # Publication filters avoid paging through a journal's entire reindexed archive.
            filters = f'from-pub-date:{since.date()},until-pub-date:{until.date()},type:journal-article'
            for _ in range(int(self.retriever_config.get('max_pages', 100))):
                params = {'filter': filters, 'rows': 200, 'cursor': cursor}
                if self.retriever_config.get('mailto'):
                    params['mailto'] = self.retriever_config.mailto
                response = client.get(f'https://api.crossref.org/journals/{quote(issn)}/works', params=params, timeout=self.timeout)
                response.raise_for_status()
                message = response.json()['message']
                items = message.get('items', [])
                if not items:
                    break
                for item in items:
                    try:
                        if not set(item.get('ISSN', [])) & set(journal.issns):
                            continue
                        if item.get('subtype') in ('editorial', 'correction', 'retraction', 'news'):
                            continue
                        title = clean_text((item.get('title') or [''])[0])
                        doi = normalize_doi(item.get('DOI'))
                        published = crossref_date(item)
                        if not title or is_cover_title(title) or not doi or not published or not since <= published <= until:
                            continue
                        if re.match(r'^(correction|erratum|retraction|editorial)\s*[:：]', title, re.I):
                            continue
                        links = [l.get('URL') for l in item.get('link', []) if l.get('content-type') == 'application/pdf']
                        authors, affiliations = crossref_metadata(item)
                        results.append(Paper(source='journals', title=title,
                            authors=authors,
                            abstract=clean_abstract(item.get('abstract')), url=f'https://doi.org/{doi}',
                            pdf_url=links[0] if links else None, doi=doi, journal=journal.title,
                            related_dois=crossref_equivalent_dois(item),
                            issns=list(journal.issns), published=published,
                            affiliations=affiliations or None,
                            authors_source='Crossref' if authors else None,
                            authors_source_url='https://api.crossref.org/works/' + quote(doi, safe='') if authors else None,
                            affiliations_source='Crossref' if affiliations else None,
                            affiliations_source_url='https://api.crossref.org/works/' + quote(doi, safe='') if affiliations else None))
                    except (TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
                        self.failures.append(journal.title)
                        logger.warning(f'{journal.title}: skipping malformed Crossref record: {exc}')
                next_cursor = message.get('next-cursor')
                if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors:
                    if len(items) >= 200:
                        raise RuntimeError(f'Crossref cursor did not advance for {journal.title}')
                    break
                seen_cursors.add(cursor)
                cursor = next_cursor
                if len(items) < 200:
                    break
            else:
                raise RuntimeError(f'Crossref page limit reached for {journal.title}; increase max_pages')
        return results

    def _rss(self, journal, client, since, until):
        response = client.get(journal.rss, timeout=self.timeout)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise ValueError(f'Invalid feed for {journal.title}')
        if not feed.entries and not feed.get('version'):
            raise ValueError(f'Not an RSS/Atom feed: {journal.title}')
        results = []
        for entry in feed.entries:
            try:
                title = clean_text(entry.get('title'))
                if not title or is_cover_title(title) or re.match(r'^(correction|erratum|retraction|editorial)\s*[:：]', title, re.I):
                    continue
                parsed = entry.get('published_parsed') or entry.get('updated_parsed')
                if not parsed:
                    continue  # Crossref can recover entries without publication timestamps.
                published = datetime(*parsed[:6], tzinfo=timezone.utc)
                if not since <= published <= until:
                    continue
                doi = next((doi for field in ('prism_doi', 'dc_identifier', 'id', 'link')
                            if (doi := normalize_doi(entry.get(field)))), None)
                url = entry.get('link') or (f'https://doi.org/{doi}' if doi else '')
                if not url:
                    continue
                parts = urlsplit(url)
                if parts.scheme not in ('http', 'https') or not parts.hostname:
                    raise ValueError('article link must be an absolute HTTP(S) URL')
                authors = [a.get('name', '') for a in entry.get('authors', []) if a.get('name')]
                results.append(Paper(source='journals', title=title, authors=authors,
                    # APS feeds publish excerpts with author/date boilerplate, not full abstracts.
                    abstract='' if urlsplit(journal.rss).hostname == 'feeds.aps.org' else rss_abstract(entry.get('summary') or entry.get('description'), title),
                    url=url, doi=doi, journal=journal.title, issns=list(journal.issns), published=published,
                    authors_source='Publisher RSS' if authors else None, authors_source_url=journal.rss if authors else None))
            except (TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
                self.failures.append(journal.title)
                logger.warning(f'{journal.title}: skipping malformed RSS entry: {exc}')
        return results

    def _journal(self, journal, since, until):
        papers, errors = [], []
        rss_ok, unsupported = False, False
        with session(self.retriever_config.get('mailto')) as client:
            if journal.rss and self.retriever_config.get('use_rss', True):
                try:
                    papers.extend(self._rss(journal, client, since, until))
                    rss_ok = True
                except Exception as exc:
                    errors.append(f'RSS: {exc}')
            crossref_ok = False
            try:
                journal = self._resolve_issns(journal, client)
                papers = self._crossref(journal, client, since, until) + papers
                crossref_ok = True
            except Exception as exc:
                errors.append(f'Crossref: {exc}')
                unsupported = (getattr(getattr(exc, 'response', None), 'status_code', None) == 404
                               or isinstance(exc, ValueError) and str(exc).startswith('No exact Crossref ISSN match'))
        if not crossref_ok and rss_ok and unsupported:
            logger.warning(f'{journal.title}: Crossref index unavailable; publisher RSS-only fallback '
                           f'({len(papers)} items in window). Historical window coverage is not verified.')
        elif not crossref_ok and papers:
            logger.warning(f'{journal.title}: RSS recovered {len(papers)} papers, but complete date-window coverage is unverified')
        if errors:
            logger.warning(f'{journal.title}: ' + '; '.join(errors))
        # Unindexed journals can use a valid publisher feed with an explicit
        # warning. Rate limits, server errors and invalid feeds remain failures.
        failed = (not crossref_ok and not (rss_ok and unsupported)) or journal.title in self.failures
        return journal, deduplicate(papers), failed

    def _retrieve_raw_papers(self):
        days = int(self.retriever_config.get('window_days', 7))
        workers = int(self.retriever_config.get('workers', 4))
        if days < 1 or not 1 <= workers <= 16:
            raise ValueError('window_days must be positive and workers must be between 1 and 16')
        until = datetime.now(timezone.utc)
        since = until.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
        journals = self._catalog()
        self.failures = []
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._journal, j, since, until) for j in journals]
            for future in as_completed(futures):
                journal, papers, failed = future.result()
                if failed:
                    self.failures.append(journal.title)
                self.cache.setdefault('issns', {})[journal.id] = journal.issns
                results.extend(papers)
                logger.info(f'{journal.title}: {len(papers)} candidates')
        self.failures = sorted(set(self.failures))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.cache, ensure_ascii=False))
        temporary.replace(self.cache_path)
        return sorted(deduplicate(results), key=lambda p: (p.published, p.url), reverse=True)

    def convert_to_paper(self, raw_paper):
        return raw_paper
