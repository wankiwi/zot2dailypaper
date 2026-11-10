import requests
from datetime import datetime, timedelta, timezone
from ..identity import normalize_doi
from ..preprint_interests import categories_for
from urllib.parse import urlencode
from ..http import session
from .base import BaseRetriever, register_retriever
from ..protocol import Paper
from loguru import logger
from typing import Any
from time import sleep

@register_retriever("biorxiv")
class BiorxivRetriever(BaseRetriever):
    server = "biorxiv"

    def __init__(self, config):
        super().__init__(config)
        self.categories = categories_for(config, self.name) if self.interests.get("enabled", True) else []

    def _retrieve_raw_papers(self) -> list[dict[str, Any]]:
        if not self.interests.get('enabled', True):
            return []
        days = self.retriever_config.get('window_days')
        if days is not None or '*' in self.categories or self.interests:
            return self._retrieve_window(int(days if days is not None else 1))
        api_url = f"https://api.biorxiv.org/details/{self.server}/2d"
        retry_num = 10
        delay_time = 10
        for i in range(retry_num):
            try:
                response = requests.get(api_url, timeout=(10, 30))
                response.raise_for_status()
                break
            except Exception as e:
                if i == retry_num - 1:
                    raise e
                else:
                    logger.warning(f"Failed to retrieve papers: {str(e)}. Retry in {delay_time} seconds.")
                    sleep(delay_time)
        result = response.json()
        collection = result['collection']
        if len(collection) == 0:
            logger.warning(f"No paper found. API Message: {result['messages']}")
            return []
        all_dates = set(c['date'] for c in collection)
        latest_date = sorted(all_dates)[-1]
        collection = [c for c in collection if c['date'] == latest_date]
        categories = self.categories
        collection = [c for c in collection if c['category'].lower() in categories]
        return collection


    def _retrieve_window(self, days):
        max_pages = int(self.retriever_config.get('max_pages', 100))
        if not 1 <= days <= 90 or max_pages < 1:
            raise ValueError(f'{self.name} requires window_days 1–90 and positive max_pages')
        until = datetime.now(timezone.utc).date()
        since = until - timedelta(days=days)
        categories = set(self.categories)
        records = []
        with session() as client:
            try:
                for category in sorted(categories):
                    records.extend(self._retrieve_query(client, since, until, category, max_pages))
            except (requests.Timeout, requests.ConnectionError) as exc:
                # A category query can stall even while the date-only endpoint works.
                # Never change route after TLS/access failures or retry an already
                # unfiltered request. Discard partial category results before recovery.
                if isinstance(exc, requests.exceptions.SSLError) or '*' in categories:
                    raise
                logger.warning(f'{self.name}: category transport failed; recovering the complete date window before local category filtering: {exc}')
                records = self._retrieve_query(client, since, until, '*', max_pages)
                logger.warning(f'{self.name}: recovered complete date window; applying configured category scope locally')
        records = [item for item in records
                   if '*' in categories or item.get('category', '').lower() in categories]
        # bioRxiv and medRxiv use the same DOI for all versions.
        newest = {}
        for item in records:
            doi = normalize_doi(item.get('doi'))
            if doi and (doi not in newest or int(item.get('version', 0)) > int(newest[doi].get('version', 0))):
                newest[doi] = item
        records = list(newest.values())
        return records

    def _retrieve_query(self, client, since, until, category, max_pages):
        """Return a complete query or raise; never pass partial pages downstream."""
        records = []
        cursor = 0
        expected_total = None
        for _ in range(max_pages):
            query = '' if category == '*' else '?' + urlencode({'category': category})
            response = client.get(f'https://api.biorxiv.org/details/{self.server}/{since}/{until}/{cursor}{query}',
                                  timeout=(10, 60))
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError(f'{self.name} API response must be an object')
            messages = result.get('messages', [])
            if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
                raise ValueError(f'{self.name} API messages must be a list of objects')
            statuses = {m.get('status') for m in messages}
            total = messages[0].get('total') if messages else None
            if total is not None:
                if isinstance(total, bool) or not str(total).isdigit():
                    raise ValueError(f'{self.name} API total must be a nonnegative integer')
                expected_total = max(expected_total or 0, int(total))
            if statuses == {'no posts found'}:
                # This official empty-window response may omit collection.
                # It is valid only when it cannot conceal unfinished pagination.
                if result.get('collection', []) != [] or (expected_total is not None and cursor < expected_total):
                    raise RuntimeError(f'{self.name} API pagination returned an incomplete or contradictory empty collection')
                logger.info(f'{self.name}: no posts in category {category} for {since}–{until}')
                break
            if statuses - {None, 'ok'}:
                raise RuntimeError(f'{self.name} API returned a failure status')
            collection = result['collection']
            if not isinstance(collection, list) or any(not isinstance(c, dict) for c in collection):
                raise ValueError(f'{self.name} API collection must be a list of objects')
            for item in collection:
                if since.isoformat() <= item.get('date', '') <= until.isoformat():
                    records.append(item)
            cursor += len(collection)
            if expected_total is not None and cursor >= expected_total:
                break
            if not collection:
                if expected_total is not None:
                    raise RuntimeError(f'{self.name} API pagination returned an incomplete collection')
                break
        else:
            raise RuntimeError(f'{self.name} max_pages reached; narrow window_days or increase max_pages')
        return records

    def retrieve_papers(self):
        papers = super().retrieve_papers()
        return papers[:10] if self.config.executor.debug else papers

    def convert_to_paper(self, raw_paper:dict[str, Any]) -> Paper | None:
        title = raw_paper['title']
        authors = [a.strip() for a in raw_paper['authors'].split(';')]
        abstract = raw_paper['abstract']
        pdf_url = f"https://www.{self.server}.org/content/{raw_paper['doi']}v{raw_paper['version']}.full.pdf"
        full_text = None # biorxiv forbids scraping its pdf
        return Paper(
            source=self.name,
            title=title,
            authors=authors,
            abstract=abstract,
            url=pdf_url,
            pdf_url=pdf_url,
            full_text=full_text,
            doi=normalize_doi(raw_paper.get('doi')),
            published=datetime.fromisoformat(raw_paper['date']).replace(tzinfo=timezone.utc) if raw_paper.get('date') else None,
        )

