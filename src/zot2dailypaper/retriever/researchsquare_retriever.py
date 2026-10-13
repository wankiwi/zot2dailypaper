"""Research Square preprint metadata from its Crossref DOI prefix."""
from datetime import datetime, timedelta, timezone
import re

from .base import BaseRetriever, register_retriever
from .journal_retriever import clean_text, crossref_date
from ..http import session
from ..identity import canonical_doi, deduplicate, normalize_doi
from ..protocol import Paper
from ..metadata import crossref_metadata
from ..abstracts import clean_abstract
from .openalex_researchsquare import OpenAlexResearchSquare


def posted_date(item):
    return crossref_date({'published': item.get('posted', {})}) or crossref_date(item)


@register_retriever('researchsquare')
class ResearchSquareRetriever(BaseRetriever):
    def __init__(self, config):
        super().__init__(config)
        options = dict(self.retriever_config) | self.interests
        backend = options.get('backend', 'crossref')
        if backend not in ('crossref', 'openalex'):
            raise ValueError('Research Square backend must be crossref or openalex')
        self.openalex = OpenAlexResearchSquare(options) if backend == 'openalex' and self.interests.get('enabled', True) else None
        self.failures = self.openalex.failures if self.openalex else []
        if self.openalex:
            # OpenAlex subfields replace the legacy keyword approximation.
            self.interests = {key: value for key, value in self.interests.items() if key != 'keywords'}

    def _retrieve_raw_papers(self):
        if not self.interests.get('enabled', True):
            return []
        if self.openalex:
            return self.openalex.retrieve()
        days = int(self.retriever_config.get('window_days', 1))
        max_pages = int(self.retriever_config.get('max_pages', 100))
        if not 1 <= days <= 90 or max_pages < 1:
            raise ValueError('Research Square requires window_days 1–90 and positive max_pages')
        until = datetime.now(timezone.utc)
        since = until - timedelta(days=days)
        cursor, seen, results = '*', set(), []
        filters = f'type:posted-content,from-pub-date:{since.date()},until-pub-date:{until.date()}'
        with session(self.retriever_config.get('mailto')) as client:
            for _ in range(max_pages):
                response = client.get('https://api.crossref.org/prefixes/10.21203/works',
                                      params={'filter': filters, 'rows': 200, 'cursor': cursor},
                                      timeout=(10, 30))
                response.raise_for_status()
                message = response.json()['message']
                items = message.get('items', [])
                results.extend(items)
                total = message.get('total-results')
                if total is not None and len(results) >= int(total):
                    break
                if not items:
                    if total is not None:
                        raise RuntimeError('Research Square pagination returned an incomplete collection')
                    break
                if total is None and len(items) < 200:
                    break
                next_cursor = message.get('next-cursor')
                if not next_cursor or next_cursor in seen or next_cursor == cursor:
                    raise RuntimeError('Research Square pagination stopped before all records were retrieved')
                seen.add(cursor)
                cursor = next_cursor
            else:
                raise RuntimeError('Research Square max_pages reached; narrow window_days or increase max_pages')
        # Some prefix records are not preprints. In Review is a valid group-title.
        results = [item for item in results
                   if item.get('type') == 'posted-content' and item.get('subtype') == 'preprint'
                   and re.fullmatch(r'10\.21203/rs\.\d+\.rs-\d+(?:/v\d+)?', normalize_doi(item.get('DOI')) or '')
                   and (date := posted_date(item)) is not None
                   and since.date() <= date.date() <= until.date()]
        # Select the newest version before stable-identity deduplication.
        def version(item):
            match = re.search(r'/v(\d+)$', item['DOI'], re.I)
            return int(match[1]) if match else 0
        results.sort(key=version, reverse=True)
        newest = {}
        for item in results:
            key = canonical_doi(item['DOI'])
            newest.setdefault(key, item)
        return list(newest.values())

    def retrieve_papers(self):
        papers = deduplicate(super().retrieve_papers())
        return papers[:10] if self.config.executor.debug else papers

    def convert_to_paper(self, raw_paper):
        if self.openalex:
            return self.openalex.convert(raw_paper)
        doi = normalize_doi(raw_paper.get('DOI'))
        title = clean_text(' '.join(raw_paper.get('title', [])))
        if re.match(r'^(?:withdrawn|retracted|withdrawal|retraction)\s*[:：]|^\[(?:withdrawn|retracted)\]', title, re.I):
            return None
        if not doi or not title:
            return None
        authors, affiliations = crossref_metadata(raw_paper)
        return Paper(source=self.name, title=title, authors=[name for name in authors if name],
                     affiliations=affiliations or None,
                     abstract=clean_abstract(raw_paper.get('abstract')),
                     url=f'https://doi.org/{doi}', pdf_url=None, full_text=None,
                     doi=doi, published=posted_date(raw_paper))
