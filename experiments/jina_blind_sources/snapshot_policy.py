"""Freeze acquisition dates before network/scoring; keep HTTP retry clocks real."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import importlib
from types import MethodType
import re


def freeze_cutoff(policy, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if policy.get('mode') != 'completed_utc_day_end':
        raise ValueError('unsupported_snapshot_date_policy')
    lag = policy.get('additional_lag_days', 0)
    if type(lag) is not int or not 0 <= lag <= 7:
        raise ValueError('invalid_snapshot_lag')
    cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=lag)
    minimum = datetime.fromisoformat(policy['minimum_asof_exclusive'].replace('Z', '+00:00'))
    if cutoff <= minimum:
        raise ValueError('no_new_completed_snapshot_yet')
    return {'asof_exclusive': cutoff.isoformat(), 'frozen_at': now.isoformat(),
            'rule': 'all source queries share this immutable cutoff; no relevance-dependent date choice'}


@contextmanager
def frozen_source_dates(asof):
    # arxiv's module also uses datetime for HTTP Retry-After calculations. It is
    # intentionally excluded; its query builder is bound separately below.
    names = ('journal_retriever', 'biorxiv_retriever', 'researchsquare_retriever',
             'openalex_researchsquare', 'openreview_retriever', 'chemrxiv_retriever')
    changed = []
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return asof.astimezone(tz) if tz is not None else asof.replace(tzinfo=None)
        @classmethod
        def utcnow(cls):
            return asof.astimezone(timezone.utc).replace(tzinfo=None)
        @classmethod
        def today(cls):
            return cls.now()
    try:
        for name in names:
            module = importlib.import_module('zot2dailypaper.retriever.' + name)
            changed.append((module, module.datetime))
            module.datetime = Frozen
        yield
    finally:
        for module, original in changed:
            module.datetime = original


def bind_arxiv_cutoff(retriever, asof):
    """Same production API query, replacing only its acquisition date endpoints."""
    from zot2dailypaper.retriever import arxiv_retriever as module
    import arxiv
    def raw(self):
        if not self.interests.get('enabled', True):
            return []
        days = self.retriever_config.get('window_days')
        if type(days) is not int or not 1 <= days <= 90:
            raise ValueError('explicit_arxiv_window_required_for_frozen_snapshot')
        since = asof - timedelta(days=days)
        query = f'submittedDate:[{since:%Y%m%d%H%M} TO {asof:%Y%m%d%H%M}]'
        if '*' not in self.categories:
            query += ' AND (' + ' OR '.join(f'cat:{category}' for category in self.categories) + ')'
        keywords = self.interests.get('keywords', [])
        if keywords:
            query += ' AND (' + ' OR '.join(f'{field}:"{module.keyword_text(term)}"'
                                          for term in keywords for field in ('ti', 'abs')) + ')'
        search = arxiv.Search(query=query, max_results=10 if self.config.executor.debug else None,
                              sort_by=arxiv.SortCriterion.SubmittedDate,
                              sort_order=arxiv.SortOrder.Descending)
        with module.api_client() as client:
            values = self._api_results(client, search)
        self._api_count = len(values)
        if '*' not in self.categories and not self.retriever_config.get('include_cross_list', False):
            values = [value for value in values if value.primary_category in self.categories]
        self._category_count = len(values)
        return values
    retriever._retrieve_raw_papers = MethodType(raw, retriever)


def capture_version_metadata(retriever):
    """Preserve already available source version facts without extra requests."""
    old = retriever.convert_to_paper
    retriever._capture_conversion_failures = 0
    def convert(self, raw, *args, **kwargs):
        try:
            paper = old(raw, *args, **kwargs)
        except Exception:
            self._capture_conversion_failures += 1
            raise
        if paper is None:
            return None
        version = None
        if isinstance(raw, dict) and str(raw.get('version', '')).isdigit():
            version = int(raw['version'])
        if version is None:
            match = re.search(r'(?:v|/v)(\d+)(?:\.full\.pdf)?$', paper.url or '', re.I)
            if match:
                version = int(match[1])
        updated = getattr(raw, 'updated', None)
        paper._capture_version = version
        paper._capture_updated = updated.isoformat() if isinstance(updated, datetime) else None
        paper._capture_original_date_status = (
            'unknown_revised_record' if paper.source in ('biorxiv', 'medrxiv', 'researchsquare') and version and version > 1
            else 'unknown_related_versions' if paper.related_dois
            else 'source_original_published')
        return paper
    retriever.convert_to_paper = MethodType(convert, retriever)


def date_eligibility(row, asof, source_days):
    """No one-day post-ranking crop; use the source's frozen production window."""
    value = row.get('first_public_date')
    if not value or row.get('original_date_status', '').startswith('unknown'):
        return False, 'original_date_unverified'
    try:
        published = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False, 'original_date_unverified'
    days = source_days.get(row['source'])
    if type(days) is not int or not 1 <= days <= 90:
        return False, 'source_window_unverified'
    if not asof-timedelta(days=days) <= published < asof:
        return False, 'outside_frozen_source_window'
    if row.get('record_updated_at'):
        updated = datetime.fromisoformat(row['record_updated_at'].replace('Z', '+00:00'))
        if updated >= asof:
            return False, 'record_updated_after_cutoff'
    return True, None


def public_row(paper, pool, before_abstract=None):
    status = getattr(paper, '_capture_original_date_status', 'source_original_published')
    published = paper.published.isoformat() if paper.published else ''
    return {'id': __import__('zot2dailypaper.identity', fromlist=['paper_id']).paper_id(paper),
            'source': paper.source, 'title': paper.title, 'abstract': paper.abstract,
            'url': paper.url, 'doi': paper.doi, 'related_dois': paper.related_dois,
            'journal': paper.journal, 'publication_kind': paper.publication_kind,
            'abstract_before_recovery': paper.abstract if before_abstract is None else before_abstract,
            'abstract_complete': bool(paper.abstract.strip()) and not paper.abstract.rstrip().endswith(('...', '…')),
            'abstract_provenance': {'source': paper.abstract_source or paper.source,
                                    'url': paper.abstract_source_url or paper.url},
            'retrieved_at': datetime.now(timezone.utc).isoformat(), 'pool': pool,
            'first_public_date': published if status == 'source_original_published' else '',
            'raw_source_published_at': published, 'original_date_status': status,
            'source_version': getattr(paper, '_capture_version', None),
            'record_updated_at': getattr(paper, '_capture_updated', None)}
