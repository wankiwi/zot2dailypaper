from .base import BaseRetriever, register_retriever
import arxiv
from datetime import datetime, timedelta, timezone
from arxiv import Result as ArxivResult
from ..protocol import Paper
from ..identity import normalize_doi
from ..preprint_interests import categories_for, keyword_text
from ..utils import extract_markdown_from_pdf, extract_tex_code_from_tar
from tempfile import TemporaryDirectory
import feedparser
from tqdm import tqdm
import multiprocessing
import os
from queue import Empty
from typing import Any, Callable, TypeVar
from loguru import logger
import requests
from contextlib import contextmanager
from time import sleep
from email.utils import parsedate_to_datetime
import re
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
from ..arxiv_health import record_health

T = TypeVar("T")

DOWNLOAD_TIMEOUT = (10, 60)
PDF_EXTRACT_TIMEOUT = 180
TAR_EXTRACT_TIMEOUT = 180
ATOM = '{http://www.w3.org/2005/Atom}'
OPENSEARCH = '{http://a9.com/-/spec/opensearch/1.1/}'
ARXIV = '{http://arxiv.org/schemas/atom}'


class ArxivFeedError(ValueError):
    """A successful HTTP response did not contain a complete arXiv page."""



class ArxivSession(requests.Session):
    """Bounded transient retries; never retry an access refusal or change endpoints."""
    def __init__(self):
        super().__init__()
        self.expected_total = None
        self._entry_ids = set()

    def _validate_page(self, response, url):
        params = parse_qs(urlsplit(url).query)
        start = int(params.get('start', ['0'])[0])
        maximum = int(params.get('max_results', ['100'])[0])
        try:
            feed = ElementTree.fromstring(response.content)
        except ElementTree.ParseError:
            raise ArxivFeedError(f'arXiv HTTP 200 is malformed XML at start={start}') from None
        if feed.tag != ATOM + 'feed':
            raise ArxivFeedError(f'arXiv HTTP 200 is not an Atom feed at start={start}')

        def number(name):
            elements = feed.findall(OPENSEARCH + name)
            text = elements[0].text if len(elements) == 1 else None
            if text is None or not re.fullmatch(r'[0-9]+', text.strip()):
                raise ArxivFeedError(f'arXiv invalid {name} at start={start}')
            return int(text)

        total, actual_start, page_size = (number(name) for name in ('totalResults', 'startIndex', 'itemsPerPage'))
        entries = feed.findall(ATOM + 'entry')
        if actual_start != start:
            raise ArxivFeedError(f'arXiv startIndex mismatch: requested={start}, returned={actual_start}')
        if (len(entries) > min(maximum, page_size, max(0, total - start))
                or (start < total and not entries)):
            raise ArxivFeedError(f'arXiv inconsistent page at start={start}: totalResults={total}, entries={len(entries)}')
        if start == 0:
            self.expected_total, self._entry_ids = total, set()
        elif self.expected_total is not None and total != self.expected_total:
            raise ArxivFeedError(f'arXiv totalResults changed between pages: {self.expected_total} -> {total}')

        ids = []
        for index, entry in enumerate(entries):
            for field in ('id', 'title', 'summary', 'published', 'updated'):
                element = entry.find(ATOM + field)
                if element is None or not ''.join(element.itertext()).strip():
                    raise ArxivFeedError(f'arXiv entry {index} missing {field} at start={start}')
            for field in ('published', 'updated'):
                try:
                    stamp = datetime.fromisoformat(entry.findtext(ATOM + field).strip().replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        raise ValueError('missing timezone')
                except ValueError:
                    raise ArxivFeedError(f'arXiv entry {index} invalid {field} at start={start}') from None
            identity = entry.findtext(ATOM + 'id').strip()
            parsed_id = urlsplit(identity)
            authors = entry.findall(ATOM + 'author')
            primary = entry.find(ARXIV + 'primary_category')
            categories = entry.findall(ATOM + 'category')
            links = entry.findall(ATOM + 'link')
            if (parsed_id.hostname not in ('arxiv.org', 'www.arxiv.org', 'export.arxiv.org')
                    or parsed_id.scheme not in ('http', 'https') or not parsed_id.path.startswith('/abs/')):
                raise ArxivFeedError(f'arXiv entry {index} invalid article ID at start={start}')
            if not authors or any(not (a.findtext(ATOM + 'name') or '').strip() for a in authors):
                raise ArxivFeedError(f'arXiv entry {index} missing author names at start={start}')
            if primary is None or not primary.get('term') or not categories or any(not c.get('term') for c in categories):
                raise ArxivFeedError(f'arXiv entry {index} missing categories at start={start}')
            if not links or any(urlsplit(link.get('href', '')).scheme not in ('http', 'https')
                                or not urlsplit(link.get('href', '')).netloc for link in links):
                raise ArxivFeedError(f'arXiv entry {index} missing links at start={start}')
            ids.append(identity)
        if len(set(ids)) != len(ids) or self._entry_ids.intersection(ids):
            raise ArxivFeedError(f'arXiv repeated entries at start={start}; refusing partial retrieval')
        self._entry_ids.update(ids)
        logger.info(f'arXiv API page: HTTP=200, start={start}, requested={maximum}, '
                    f'totalResults={total}, entries={len(entries)}')

    def get(self, url, **kwargs):
        kwargs.setdefault('timeout', (5, 20))
        kwargs['allow_redirects'] = False
        for attempt in range(4):
            response = None
            try:
                response = super().get(url, **kwargs)
            except requests.exceptions.ProxyError:
                # A proxy refusal must not be treated as a transient arXiv outage.
                raise
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 3:
                    raise
            else:
                logger.info(f'arXiv API HTTP status={response.status_code}, start={parse_qs(urlsplit(url).query).get("start", ["0"])[0]}')
                if response.status_code not in (500, 502, 503, 504) or attempt == 3:
                    if response.status_code == 200:
                        try:
                            self._validate_page(response, url)
                        except ArxivFeedError:
                            response.close()
                            raise
                    return response
            delay = 15 * 2 ** attempt
            if response is not None:
                retry_after = response.headers.get('Retry-After')
                response.close()
                if retry_after:
                    try:
                        requested = float(retry_after)
                    except ValueError:
                        try:
                            requested = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            raise requests.exceptions.RetryError('Invalid arXiv Retry-After; stopping retries') from None
                    if not 0 <= requested <= 60:
                        raise requests.exceptions.RetryError('arXiv Retry-After exceeds bounded retry allowance')
                    delay = max(delay, requested)
            logger.warning(f'arXiv temporary transport failure; retry {attempt+1}/3 in {delay:g}s')
            sleep(delay)


@contextmanager
def api_client():
    # The pinned arxiv client otherwise repeats every HTTP error at a fixed 3s
    # interval and supplies no request timeout. Keep exactly one retry layer.
    client = arxiv.Client(page_size=100, num_retries=0, delay_seconds=3)
    old_session = getattr(client, '_session', None)
    if old_session is not None:
        old_session.close()
    with ArxivSession() as transport:
        client._session = transport
        yield client

def _download_file(url: str, path: str) -> None:
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        with open(path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)


def _run_in_subprocess(
    result_queue: Any,
    func: Callable[..., T | None],
    args: tuple[Any, ...],
) -> None:
    try:
        result_queue.put(("ok", func(*args)))
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _run_with_hard_timeout(
    func: Callable[..., T | None],
    args: tuple[Any, ...],
    *,
    timeout: float,
    operation: str,
    paper_title: str,
) -> T | None:
    start_methods = multiprocessing.get_all_start_methods()
    context = multiprocessing.get_context("fork" if "fork" in start_methods else start_methods[0])
    result_queue = context.Queue()
    process = context.Process(target=_run_in_subprocess, args=(result_queue, func, args))
    process.start()

    try:
        status, payload = result_queue.get(timeout=timeout)
    except Empty:
        if process.is_alive():
            process.kill()
        process.join(5)
        result_queue.close()
        result_queue.join_thread()
        logger.warning(f"{operation} timed out for {paper_title} after {timeout} seconds")
        return None

    process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    result_queue.close()
    result_queue.join_thread()

    if status == "ok":
        return payload

    logger.warning(f"{operation} failed for {paper_title}: {payload}")
    return None


def _extract_text_from_pdf_worker(pdf_url: str) -> str:
    with TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "paper.pdf")
        _download_file(pdf_url, path)
        return extract_markdown_from_pdf(path)


def _extract_text_from_html_worker(html_url: str) -> str | None:
    import trafilatura

    downloaded = trafilatura.fetch_url(html_url)
    if downloaded is None:
        raise ValueError(f"Failed to download HTML from {html_url}")
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=True)
    if not text:
        raise ValueError(f"No text extracted from {html_url}")
    return text


def _extract_text_from_tar_worker(source_url: str, paper_id: str) -> str | None:
    with TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "paper.tar.gz")
        _download_file(source_url, path)
        file_contents = extract_tex_code_from_tar(path, paper_id)
        if not file_contents or "all" not in file_contents:
            raise ValueError("Main tex file not found.")
        return file_contents["all"]


@register_retriever("arxiv")
class ArxivRetriever(BaseRetriever):
    def __init__(self, config):
        super().__init__(config)
        self._raw = {}
        self._converted_count = self._api_count = self._category_count = 0
        self.categories = categories_for(config, self.name) if self.interests.get('enabled', True) else []

    def retrieve_papers(self) -> list[Paper]:
        if not self.interests.get('enabled', True):
            return []
        self._raw.clear()
        self._converted_count = self._api_count = self._category_count = 0
        papers = super().retrieve_papers()
        counts = {'api_entries': self._api_count, 'primary_category_kept': self._category_count,
                  'converted': self._converted_count, 'keyword_kept': len(papers)}
        logger.info(f'arXiv retrieval stages: {counts}; missing_pdf_links={sum(not p.pdf_url for p in papers)}')
        if self._converted_count != self._category_count:
            self._raw.clear()
            raise ArxivFeedError('arXiv conversion skipped entries; refusing incomplete candidate set')
        scope = {'categories': sorted(self.categories), 'keywords': sorted(self.interests.get('keywords', [])),
                 'window_days': self.retriever_config.get('window_days'),
                 'include_cross_list': self.retriever_config.get('include_cross_list', False)}
        record_health(self.config, scope, counts, datetime.now(timezone.utc))
        return papers

    @staticmethod
    def _api_results(client, search):
        results = list(client.results(search))
        transport = getattr(client, '_session', None)
        if isinstance(transport, ArxivSession) and transport.expected_total is not None:
            expected = min(transport.expected_total, search.max_results or transport.expected_total)
            if len(results) != expected:
                raise ArxivFeedError(f'arXiv SDK result count mismatch: expected={expected}, parsed={len(results)}')
        return results

    def _retrieve_raw_papers(self) -> list[ArxivResult]:
        if not self.interests.get('enabled', True):
            return []
        categories = self.categories
        keywords = self.interests.get('keywords', [])
        days = self.retriever_config.get('window_days')
        if '*' in categories or days is not None or keywords:
            # Submission dates precede public announcements, especially over weekends.
            days = int(days if days is not None else 7)
            if not 1 <= days <= 90:
                raise ValueError('arxiv window_days must be between 1 and 90')
            until = datetime.now(timezone.utc)
            since = until - timedelta(days=days)
            logger.info(f'arXiv query: mode=submittedDate, since={since.isoformat()}, until={until.isoformat()}, '
                        f'window_days={days}, categories={categories}, '
                        f'include_cross_list={self.retriever_config.get("include_cross_list", False)}, keyword_terms={len(keywords)}')
            query = f'submittedDate:[{since:%Y%m%d%H%M} TO {until:%Y%m%d%H%M}]'
            if '*' not in categories:
                query += ' AND (' + ' OR '.join(f'cat:{category}' for category in categories) + ')'
            if keywords:
                query += ' AND (' + ' OR '.join(f'{field}:"{keyword_text(term)}"'
                    for term in keywords for field in ('ti', 'abs')) + ')'
            search = arxiv.Search(query=query, max_results=10 if self.config.executor.debug else None,
                                  sort_by=arxiv.SortCriterion.SubmittedDate,
                                  sort_order=arxiv.SortOrder.Descending)
            with api_client() as client:
                results = self._api_results(client, search)
            self._api_count = len(results)
            if '*' not in categories and not self.retriever_config.get('include_cross_list', False):
                results = [paper for paper in results if paper.primary_category in categories]
            self._category_count = len(results)
            return results
        query = '+'.join(categories)
        logger.info(f'arXiv query: mode=RSS, categories={categories}')
        include_cross_list = self.config.source.arxiv.get("include_cross_list", False)
        # Get the latest paper from arxiv rss feed
        feed = feedparser.parse(f"https://rss.arxiv.org/atom/{query}")
        if 'Feed error for query' in feed.feed.get('title', ''):
            raise Exception(f"Invalid ARXIV_QUERY: {query}.")
        raw_papers = []
        allowed_announce_types = {"new", "cross"} if include_cross_list else {"new"}
        all_paper_ids = [
            i.id.removeprefix("oai:arXiv.org:")
            for i in feed.entries
            if i.get("arxiv_announce_type", "new") in allowed_announce_types
        ]
        if self.config.executor.debug:
            all_paper_ids = all_paper_ids[:10]

        # Get full information of each paper from arxiv api
        bar = tqdm(total=len(all_paper_ids))
        with api_client() as client:
            for i in range(0, len(all_paper_ids), 20):
                search = arxiv.Search(id_list=all_paper_ids[i:i + 20])
                batch = self._api_results(client, search)
                bar.update(len(batch))
                raw_papers.extend(batch)
        bar.close()
        self._api_count = self._category_count = len(raw_papers)
        return raw_papers

    def convert_to_paper(self, raw_paper: ArxivResult) -> Paper:
        title = raw_paper.title
        authors = [a.name for a in raw_paper.authors]
        abstract = raw_paper.summary
        pdf_url = raw_paper.pdf_url
        self._raw[raw_paper.entry_id] = raw_paper
        full_text = None
        paper = Paper(
            source=self.name,
            title=title,
            authors=authors,
            abstract=abstract,
            url=raw_paper.entry_id,
            pdf_url=pdf_url,
            full_text=full_text,
            doi=normalize_doi(getattr(raw_paper, "doi", None)),
            published=getattr(raw_paper, "published", None),
        )
        self._converted_count += 1
        return paper

    def enrich(self, paper: Paper) -> Paper:
        raw = self._raw.get(paper.url)
        if raw is not None and not paper.full_text:
            paper.full_text = extract_text_from_html(raw)
            if paper.full_text is None:
                paper.full_text = extract_text_from_pdf(raw)
            if paper.full_text is None:
                paper.full_text = extract_text_from_tar(raw)
        return paper


def extract_text_from_html(paper: ArxivResult) -> str | None:
    html_url = paper.entry_id.replace("/abs/", "/html/")
    try:
        return _run_with_hard_timeout(_extract_text_from_html_worker, (html_url,),
            timeout=PDF_EXTRACT_TIMEOUT, operation="HTML extraction", paper_title=paper.title)
    except Exception as exc:
        logger.warning(f"HTML extraction failed for {paper.title}: {exc}")
        return None


def extract_text_from_pdf(paper: ArxivResult) -> str | None:
    if paper.pdf_url is None:
        logger.warning(f"No PDF URL available for {paper.title}")
        return None
    return _run_with_hard_timeout(
        _extract_text_from_pdf_worker,
        (paper.pdf_url,),
        timeout=PDF_EXTRACT_TIMEOUT,
        operation="PDF extraction",
        paper_title=paper.title,
    )


def extract_text_from_tar(paper: ArxivResult) -> str | None:
    source_url = paper.source_url()
    if source_url is None:
        logger.warning(f"No source URL available for {paper.title}")
        return None
    return _run_with_hard_timeout(
        _extract_text_from_tar_worker,
        (source_url, paper.entry_id),
        timeout=TAR_EXTRACT_TIMEOUT,
        operation="Tar extraction",
        paper_title=paper.title,
    )
