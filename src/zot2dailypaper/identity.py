"""Stable identifiers shared by retrievers, deduplication and output channels."""
import hashlib
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    value = unquote(value).strip()
    if value.startswith(('https://', 'http://')):
        # Tracking query strings and fragments are not part of a DOI.
        value = urlsplit(value).path
    match = re.search(r'10\.\d{4,9}/[^\s<>"\']+', value, re.I)
    return match.group(0).rstrip('.,;').lower() if match else None


def title_key(title: str) -> str:
    return ' '.join(re.findall(r'\w+', title.casefold()))


def canonical_doi(value):
    doi = normalize_doi(value)
    # Research Square versions share one recommendation identity.
    if doi and doi.startswith('10.21203/rs.'):
        doi = re.sub(r'/v\d+$', '', doi)
    if doi and doi.startswith(('10.26434/chemrxiv.', '10.26434/chemrxiv-')):
        doi = re.sub(r'[./-]v[1-9]\d*$', '', doi)
    return doi


def paper_doi(paper):
    return canonical_doi(paper.doi) or canonical_doi(paper.url)


def crossref_equivalent_dois(item):
    """Only explicit preprint/publication relations; never corrections/references."""
    result=[]
    relation=item.get('relation') or {}
    if not isinstance(relation, dict):
        return result
    for kind in ('is-preprint-of','has-preprint'):
        links = relation.get(kind) or []
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict):
                continue
            doi=canonical_doi(link.get('id')) if link.get('id-type')=='doi' else None
            if doi and doi != canonical_doi(item.get('DOI')) and doi not in result:result.append(doi)
    return result


def paper_dois(paper):
    return {doi for value in [paper.doi,paper.url,*getattr(paper, 'related_dois', [])] if (doi := canonical_doi(value))}


def paper_id(paper) -> str:
    doi = paper_doi(paper)
    if doi:
        return 'doi:' + doi
    url = paper.url.strip()
    parts = urlsplit(url)
    if parts.hostname in ('arxiv.org', 'www.arxiv.org', 'export.arxiv.org'):
        match = re.fullmatch(r'/(?:abs|pdf|html)/(.+?)(?:v\d+)?(?:\.pdf)?', parts.path)
        if match:
            return 'arxiv:' + match.group(1)
    if url:
        tracking = {'fbclid', 'gclid', 'mc_cid', 'mc_eid'}
        query = urlencode([(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                           if not key.lower().startswith('utm_') and key.lower() not in tracking])
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), query, ''))
    return 'title:' + hashlib.sha256(title_key(paper.title).encode()).hexdigest()


def deduplicate(papers):
    """Merge matching DOI/URL identities and conservative normalized titles."""
    result, keys, titles = [], {}, {}
    for paper in papers:
        key, title = paper_id(paper), title_key(paper.title)
        matches = []
        for identity in [key, *('doi:'+d for d in sorted(paper_dois(paper)))]:
            match = keys.get(identity)
            if match is not None and all(match is not p for p in matches):matches.append(match)
        previous = matches[0] if matches else None
        if len(matches) > 1:
            combined = paper_dois(paper) | set().union(*(paper_dois(p) for p in matches))
            previous.related_dois = sorted(combined - {paper_doi(previous)})
            for other in matches[1:]:
                deduplicate([previous, other])
                result.remove(other)
                for identity, value in list(keys.items()):
                    if value is other:keys[identity]=previous
                for name, value in list(titles.items()):
                    if value is other:titles[name]=previous

        if previous is None and title:
            match = titles.get(title)
            # An OpenReview title never establishes identity with another note
            # or source: a title merge can disclose names withheld by its ACLs.
            if match is not None and 'openreview' in (getattr(match, 'source', None), getattr(paper, 'source', None)):
                match = None
            # Different DOIs can legitimately share a generic article title.
            if match is not None and (not paper_doi(match) or not paper_doi(paper) or paper_doi(match) == paper_doi(paper)):
                previous = match
        if previous is not None:
            aliases = paper_dois(previous) | paper_dois(paper)
            different_versions = bool(paper_doi(previous) and paper_doi(paper) and normalize_doi(previous.doi or previous.url) != normalize_doi(paper.doi or paper.url))
            if different_versions:
                # Linked preprint and publication share recommendation identity, not text.
                newer_chemrxiv = False
                if paper.source == previous.source == 'chemrxiv' and paper_doi(paper) == paper_doi(previous):
                    versions = [re.search(r'[./-]v([1-9]\d*)$', normalize_doi(p.doi) or '') for p in (paper, previous)]
                    newer_chemrxiv = (int(versions[0][1]) if versions[0] else 0) > (int(versions[1][1]) if versions[1] else 0)
                if (paper.journal and not previous.journal) or newer_chemrxiv:
                    previous.__dict__.update(paper.__dict__)
                previous.related_dois = sorted(aliases - {paper_doi(previous)})
                for alias in aliases:keys['doi:'+alias]=previous
                keys[key]=previous
                continue
            # Prefer the published record while preserving useful preprint text.
            if paper.journal and not previous.journal:
                for field in ('doi', 'journal', 'issns', 'published', 'url', 'source', 'publication_kind', 'publication_status', 'publication_venue'):
                    setattr(previous, field, getattr(paper, field))
            for field in ('abstract', 'full_text', 'pdf_url', 'doi'):
                if not getattr(previous, field) and getattr(paper, field):
                    setattr(previous, field, getattr(paper, field))
                    if field == 'abstract':
                        previous.abstract_source = paper.abstract_source
            if title_key(previous.title) == title:
                for field in ('authors', 'affiliations'):
                    if not getattr(previous, field) and getattr(paper, field):
                        setattr(previous, field, list(getattr(paper, field)))
                        for suffix in ('_status', '_source', '_source_url'):
                            setattr(previous, field + suffix, getattr(paper, field + suffix))
            keys[key] = previous
            keys[paper_id(previous)] = previous
            previous.related_dois = sorted(aliases - {paper_doi(previous)})
            for alias in aliases:keys['doi:'+alias]=previous
        else:
            result.append(paper)
            keys[key] = paper
            for alias in paper_dois(paper):keys['doi:'+alias]=paper
            if title and getattr(paper, 'source', None) != 'openreview':
                titles[title] = paper
    return result
