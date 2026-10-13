"""Public DOI metadata recovery; never invent an abstract or scrape a paywall."""
from html import unescape
import re
from urllib.parse import quote, urlsplit
from dataclasses import dataclass, field
from time import monotonic
from loguru import logger
from .http import abstract_session as session
from .identity import paper_doi, normalize_doi, title_key
from .publisher_abstracts import publisher_session, publisher_url, recover_publisher
from .aps_metadata import semantic_scholar, arxiv_manuscript

# Strip only known formatting tags. Unknown '<Tc' and '<y ...>' sequences
# are scientific plaintext, not HTML. Decode entities once, never reparse them
# as arbitrary tags; mathematical inequalities must survive cleaning.
_MARKUP = re.compile(r"</?(?:jats:)?(?:p|title|abstract|sec|div|span|br|i|b|em|strong|italic|bold|sup|sub|a|ul|ol|li|h[1-6]|table|tr|td|th|tbody|thead|xref|ext-link)(?:\s+[\w:-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s<>]+))*\s*/?>", re.I)


def clean_abstract(value):
    if not isinstance(value, str):
        return ''
    text = unescape(value)
    text = re.sub(r'<(script|style)\b[^>]*>.*?</\1\s*>', '', text, flags=re.I | re.S)
    text = ' '.join(_MARKUP.sub(' ', text).split())
    if text.casefold().strip(' .') in {'', 'no abstract', 'no abstract available', 'abstract unavailable', 'n/a'}:
        return ''
    return text

def inverted_abstract(index):
    if not isinstance(index, dict): return ''
    words = {}
    for word, positions in index.items():
        if isinstance(word, str) and isinstance(positions, list):
            for pos in positions:
                if type(pos) is int and 0 <= pos < 100000: words.setdefault(pos, word)
    return clean_abstract(' '.join(words[pos] for pos in sorted(words)))


def record_attempt(paper, provider, status):
    entry = {'provider': provider, 'status': status}
    if entry not in paper.abstract_recovery_attempts:
        paper.abstract_recovery_attempts.append(entry)


@dataclass
class RecoveryContext:
    """Shared limits and access refusals across optional pre-ranking and delivery phases."""
    attempted: set = field(default_factory=set)
    publisher_attempts: int = 0
    aps_attempts: int = 0
    blocked: set = field(default_factory=set)
    publisher_blocked: set = field(default_factory=set)
    metadata_blocked: set = field(default_factory=set)
    deadline: float | None = None

    def expired(self):
        return self.deadline is not None and monotonic() >= self.deadline


class DeadlineClient:
    def __init__(self, client, context):
        self.client, self.context = client, context

    def get(self, url, **kwargs):
        if self.context.deadline is not None:
            remaining = self.context.deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError('Abstract recovery admission deadline expired')
            connect, read = kwargs.get('timeout', (5, 20))
            kwargs['timeout'] = (min(connect, remaining), min(read, remaining))
        return self.client.get(url, **kwargs)


def recovery_shortlist(ranked, limit):
    from .selection import publication_group
    pools = [[p for p in ranked if not p.abstract and paper_doi(p) and publication_group(p) == group]
             for group in ('journals', 'preprints')]
    result = []
    for index in range(max(map(len, pools), default=0)):
        for pool in pools:
            if index < len(pool) and len(result) < limit:
                result.append(pool[index])
        if len(result) >= limit:
            break
    return result


def recover_researchsquare(paper, client, blocked):
    """Use the cited version for metadata; recommendation deduplication stays versionless."""
    import json
    from .aps_metadata import fetch
    doi = normalize_doi(paper.doi) or normalize_doi(paper.url)
    if not doi or not re.fullmatch(r'10\.21203/rs\.\d+\.rs-\d+/v[1-9]\d*', doi):
        paper.abstract_recovery_status = 'researchsquare_version_unverified'
        return
    paper.abstract_recovery_status = 'researchsquare_abstract_unavailable'
    for provider, base in [('Crossref', 'https://api.crossref.org/works/'),
                           ('OpenAlex', 'https://api.openalex.org/works/https://doi.org/')]:
        url = base + quote(doi, safe='')
        try:
            raw = fetch(client, url, provider, blocked)
            if raw is None:
                record_attempt(paper, provider, 'access_blocked' if provider in blocked else 'not_found')
                continue
            data = json.loads(raw)
            if provider == 'Crossref':
                data = data.get('message', {})
                found_doi, titles = data.get('DOI'), data.get('title') or []
                found_title = titles[0] if isinstance(titles, list) and titles else ''
                abstract = clean_abstract(data.get('abstract'))
            else:
                found_doi, found_title = data.get('doi'), data.get('title') or data.get('display_name') or ''
                abstract = inverted_abstract(data.get('abstract_inverted_index'))
            if normalize_doi(found_doi) != doi or title_key(clean_abstract(found_title)) != title_key(paper.title):
                record_attempt(paper, provider, 'doi_version_title_mismatch')
                logger.warning(f'{provider} Research Square DOI/version/title mismatch; metadata rejected')
                continue
            if len(abstract) >= 40 and not abstract.rstrip().endswith(('…', '...')):
                record_attempt(paper, provider, 'recovered')
                paper.abstract, paper.abstract_source = abstract, provider
                paper.abstract_source_url, paper.abstract_recovery_status = url, 'recovered'
                return
            record_attempt(paper, provider, 'abstract_absent_or_incomplete')
        except Exception as exc:
            record_attempt(paper, provider, 'request_failed_' + type(exc).__name__)
            logger.warning(f'{provider} Research Square abstract lookup failed ({type(exc).__name__})')
    logger.warning('Research Square exact-version abstract unavailable; retaining missing-abstract status')


def recover_abstracts(papers, config, context=None):
    if not config.get('enabled', False): return
    limit = config.get('max_papers', 50)
    if type(limit) is not int or limit < 0: raise ValueError('abstracts.max_papers must be a nonnegative integer')
    publisher_limit = config.get('publisher_max_papers', 10)
    if type(publisher_limit) is not int or not 0 <= publisher_limit <= 50:
        raise ValueError('abstracts.publisher_max_papers must be an integer from 0 to 50')
    aps_limit = config.get('aps_metadata_max_papers', 10)
    if type(aps_limit) is not int or not 0 <= aps_limit <= 50:
        raise ValueError('abstracts.aps_metadata_max_papers must be an integer from 0 to 50')
    context = context or RecoveryContext()
    for paper in papers:
        if not paper.abstract and not paper_doi(paper):
            paper.abstract_recovery_status = 'doi_unavailable'
    missing = [p for p in papers if not p.abstract and paper_doi(p) and id(p) not in context.attempted]
    remaining = max(0, limit - len(context.attempted))
    for paper in missing[remaining:]:
        paper.abstract_recovery_status = 'metadata_lookup_limit'
    if len(missing) > remaining:
        logger.warning(f'Abstract recovery limited to {remaining}/{len(missing)} missing DOI abstracts')
    with session(config.get('mailto')) as metadata_transport, publisher_session() as publisher_transport:
        client, publisher = DeadlineClient(metadata_transport, context), DeadlineClient(publisher_transport, context)
        blocked = context.blocked
        publisher_blocked = context.publisher_blocked
        metadata_blocked = context.metadata_blocked
        for paper in missing[:remaining]:
            if context.expired():
                paper.abstract_recovery_status = 'pre_rank_time_limit'
                continue
            context.attempted.add(id(paper))
            paper.abstract_recovery_status = 'metadata_abstract_unavailable'
            doi = paper_doi(paper)
            if doi.startswith('10.21203/rs.'):
                recover_researchsquare(paper, publisher, blocked)
                continue
            metadata_doi = normalize_doi(paper.doi) or normalize_doi(paper.url) or doi
            for provider, url in [('Crossref', 'https://api.crossref.org/works/' + quote(metadata_doi, safe='')),
                                  ('OpenAlex', 'https://api.openalex.org/works/https://doi.org/' + quote(metadata_doi, safe=''))]:
                if context.expired():
                    record_attempt(paper, provider, 'pre_rank_time_limit')
                    continue
                if provider in blocked:
                    record_attempt(paper, provider, 'access_blocked')
                    continue
                try:
                    response = client.get(url, timeout=(5, 20), allow_redirects=False)
                    if response.status_code == 404:
                        record_attempt(paper, provider, 'not_found')
                        continue
                    if response.status_code in (401, 403, 429):
                        blocked.add(provider)
                        record_attempt(paper, provider, 'http_' + str(response.status_code))
                        logger.warning(f'{provider} abstract lookup blocked HTTP {response.status_code}; no paid fallback')
                        continue
                    if 300 <= response.status_code < 400:
                        raise ValueError("Metadata redirect rejected")
                    response.raise_for_status()
                    data = response.json()
                    if provider == 'Crossref':
                        data = data.get('message', {})
                        found_doi = data.get('DOI')
                        titles = data.get('title') or []
                        found_title = titles[0] if isinstance(titles, list) and titles else ''
                        abstract = clean_abstract(data.get('abstract'))
                    else:
                        found_doi, found_title = data.get('doi'), data.get('title') or ''
                        abstract = inverted_abstract(data.get('abstract_inverted_index'))
                    if normalize_doi(found_doi) != normalize_doi(paper.doi or paper.url):
                        record_attempt(paper, provider, 'doi_version_mismatch')
                        continue
                    if found_title and title_key(clean_abstract(found_title)) != title_key(paper.title):
                        record_attempt(paper, provider, 'title_mismatch')
                        continue
                    incomplete = abstract.rstrip().endswith(('…', '...'))
                    if incomplete:
                        abstract = ''
                    absent_reason = 'correction_no_standalone_abstract' if data.get('type') in ('erratum', 'correction') else 'abstract_absent'
                    record_attempt(paper, provider, 'recovered' if abstract else ('abstract_incomplete' if incomplete else absent_reason))
                    if abstract:
                        paper.abstract, paper.abstract_source = abstract, provider
                        paper.abstract_source_url, paper.abstract_recovery_status = url, 'recovered'
                        break
                except Exception as exc:
                    record_attempt(paper, provider, 'request_failed_' + type(exc).__name__)
                    logger.warning(f'{provider} abstract lookup failed ({type(exc).__name__}); retaining missing-abstract status')
            if not paper.abstract and config.get('publisher_fallback', True) and publisher_url(paper):
                if urlsplit(publisher_url(paper)).hostname in publisher_blocked:
                    paper.abstract_recovery_status = 'publisher_access_blocked'
                elif context.expired():
                    paper.abstract_recovery_status = 'pre_rank_time_limit'
                elif context.publisher_attempts >= publisher_limit:
                    paper.abstract_recovery_status = 'publisher_lookup_limit'
                else:
                    context.publisher_attempts += 1
                    try:
                        abstract, url, status = recover_publisher(paper, publisher, publisher_blocked)
                        paper.abstract_recovery_status = status
                        if abstract:
                            paper.abstract, paper.abstract_source, paper.abstract_source_url = abstract, 'Publisher', url
                        else:
                            logger.warning(f'Publisher abstract lookup: {status}; retaining missing-abstract status')
                    except Exception as exc:
                        paper.abstract_recovery_status = 'publisher_request_failed'
                        logger.warning(f'Publisher abstract lookup failed ({type(exc).__name__}); retaining missing-abstract status')
                record_attempt(paper, 'Publisher', paper.abstract_recovery_status)
            if not paper.abstract and doi.startswith('10.1103/') and context.aps_attempts < aps_limit:
                if context.expired():
                    record_attempt(paper, 'APS alternatives', 'pre_rank_time_limit')
                    continue
                if not {'Semantic Scholar', 'arXiv'} <= metadata_blocked:
                    context.aps_attempts += 1
                for provider, recover in [('Semantic Scholar', semantic_scholar), ('arXiv', arxiv_manuscript)]:
                    if context.expired():
                        record_attempt(paper, provider, 'pre_rank_time_limit')
                        continue
                    if provider in metadata_blocked:
                        record_attempt(paper, provider, 'access_blocked')
                        continue
                    try:
                        result = recover(paper, publisher, metadata_blocked)
                        record_attempt(paper, provider, 'recovered' if result else ('access_blocked' if provider in metadata_blocked else 'no_verified_abstract'))
                        if result:
                            paper.abstract, paper.abstract_source, paper.abstract_source_url, paper.abstract_recovery_status = result
                            break
                        if provider in metadata_blocked:
                            logger.warning(f'{provider} metadata access blocked; no retries or identity substitution')
                    except Exception as exc:
                        record_attempt(paper, provider, 'request_failed_' + type(exc).__name__)
                        logger.warning(f'{provider} metadata lookup failed ({type(exc).__name__}); no unverified substitute')
