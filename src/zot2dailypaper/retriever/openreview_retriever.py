"""Public OpenReview API v2 submissions with explicit venue/subject/keyword gates."""
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
import os
import time
from urllib.parse import quote
from loguru import logger
from .base import BaseRetriever, register_retriever
from ..http import session
from ..protocol import Paper
from ..abstracts import clean_abstract
from ..preprint_interests import keyword_text, _terms
from ..metadata import names
from ..openreview_auth import API, login

VENUES = {'ICLR': 'ICLR.cc/{year}/Conference', 'NeurIPS': 'NeurIPS.cc/{year}/Conference',
          'ICML': 'ICML.cc/{year}/Conference', 'CoRL': 'robot-learning.org/CoRL/{year}/Conference', 'TMLR': 'TMLR'}
SUBJECTS = {
    'AI for Science': ('ai 4 physical sciences', 'machine learning for sciences', 'physical sciences',
                       'chemistry physics and earth sciences', 'ai for science'),
    'Scientific Machine Learning': ('scientific machine learning', 'physics informed', 'neural operator',
                                    'machine learning for sciences', 'ai 4 physical sciences', 'physical sciences',
                                    'chemistry physics and earth sciences'),
    'Machine Learning': ('machine learning', 'deep learning', 'reinforcement learning', 'representation learning',
                         'neural network', 'supervised learning', 'unsupervised learning'),
}

def value(content, key, default=None):
    result = content.get(key, default)
    return result.get('value', default) if isinstance(result, dict) else result

def no_exclusions(item):
    exclusions = item.get('nonreaders', [])
    return isinstance(exclusions, list) and not exclusions


def public_fields(content):
    # Fields without their own ACL inherit the public note/group ACL. Explicit
    # readers and exclusions override it; malformed exclusions fail closed.
    return {key: item for key, item in content.items()
            if not isinstance(item, dict) or (no_exclusions(item) and
                ('readers' not in item or
                 isinstance(item['readers'], list) and 'everyone' in item['readers']))}


def strings(item):
    return [item] if isinstance(item, str) else [v for v in item if isinstance(v, str)] if isinstance(item, list) else []

def contains(texts, terms):
    return any(' ' + keyword_text(term) + ' ' in ' ' + keyword_text(text.replace('_', ' ')) + ' ' for term in terms for text in texts)

@register_retriever('openreview')
class OpenReviewRetriever(BaseRetriever):
    def __init__(self, config):
        super().__init__(config)
        self.options = dict(self.retriever_config)
        self.options.update(self.interests)
        self.venues = _terms(self.options.get('venues'), 'openreview.venues')
        self.subjects = _terms(self.options.get('subject_areas'), 'openreview.subject_areas')
        self.keywords = _terms(self.options.get('keywords'), 'openreview.keywords')
        if set(self.venues) - VENUES.keys(): raise ValueError('Unsupported OpenReview venue; supported: ICLR, NeurIPS, ICML, TMLR, CoRL')
        if set(self.subjects) - SUBJECTS.keys(): raise ValueError('Unsupported OpenReview subject area; use AI for Science, Machine Learning, Scientific Machine Learning')
        self.days, self.max_pages = self.options.get('window_days', 7), self.options.get('max_pages', 100)
        if type(self.days) is not int or not 1 <= self.days <= 90 or type(self.max_pages) is not int or self.max_pages < 1:
            raise ValueError('OpenReview requires window_days 1-90 and positive max_pages')
        self.authenticated = False
        self.failures = []
        self.venue_ids = {}
        self.fallback_count = 0

    def _authenticate(self, client):
        """Official API v2 login; credentials/token stay only in memory."""
        self.authenticated = False
        headers = getattr(client, 'headers', None)
        if headers is not None:
            headers.pop('Authorization', None)
        username, password = os.getenv('OPENREVIEW_USERNAME'), os.getenv('OPENREVIEW_PASSWORD')
        if not username and not password:
            return
        if not username or not password:
            raise RuntimeError('Set both OPENREVIEW_USERNAME and OPENREVIEW_PASSWORD repository secrets')
        token = login(client, username, password)
        client.headers['Authorization'] = 'Bearer ' + token
        self.authenticated = True

    def _get(self, client, path, params):
        delay = 1 - (time.monotonic() - getattr(self, '_last_request', 0))
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()
        response = client.get(API + path, params=params, timeout=(10, 30), allow_redirects=False)
        if response.status_code in (401, 403, 429):
            raise RuntimeError(f'OpenReview API access blocked HTTP {response.status_code}; ' +
                ('rate limited: wait for the next run; respect provider limits' if response.status_code == 429 else
                 'configure OPENREVIEW_USERNAME and OPENREVIEW_PASSWORD secrets for official login, or verify account access if already configured'))
        if 300 <= response.status_code < 400:
            raise RuntimeError("OpenReview API redirected; authenticate through the official supported path")
        response.raise_for_status()
        return response.json()

    def _groups(self, client, venue, year):
        years = [year - 1, year, year + 1] if venue == 'ICLR' else [year - 1, year]
        for identity in ([VENUES[venue]] if venue == 'TMLR' else [VENUES[venue].format(year=y) for y in years]):
            data = self._get(client, '/groups', {'id': identity})
            groups = [g for g in data.get('groups', []) if g.get('id') == identity]
            for group in groups:
                content = public_fields(group.get('content', {}))
                invitation = value(content, 'submission_id')
                name = value(content, 'submission_name')
                if not invitation and isinstance(name, str): invitation = identity + '/-/' + name
                if not isinstance(invitation, str) or not invitation.startswith(identity + '/-/'):
                    raise ValueError(f'{identity}: missing supported submission invitation metadata')
                self.venue_ids[identity] = {
                    'under_review': value(content, 'under_review_venue_id'),
                    'decision_pending': value(content, 'decision_pending_venue_id'),
                    # API v2 accepted-paper queries use content.venueid=<venue>.
                    # Require pdate as separate acceptance/publication evidence.
                    'accepted': value(content, 'accepted_venue_id') or identity,
                }
                yield identity, invitation

    def _retrieve_raw_papers(self):
        until = datetime.now(timezone.utc)
        since = until.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=self.days)
        self.since, self.until = since, until
        self.failures, self.fallback_count = [], 0
        records, seen = [], set()
        with session() as client:
            self._authenticate(client)
            for venue in self.venues:
                try:
                    groups = list(self._groups(client, venue, until.year))
                    if not groups: raise RuntimeError('No public API v2 conference groups found')
                    for group, invitation in groups:
                        # Notes API supports mintcdate, not a documented mintmdate.
                        # Creation cutoffs can miss older submissions made public
                        # recently: page the verified invitation and filter locally.
                        offset, previous = 0, None
                        for _ in range(self.max_pages):
                            data = self._get(client, '/notes', {'invitation': invitation, 'limit': 1000, 'offset': offset,
                                'sort': 'tmdate:desc', 'count': 'true'})
                            notes = data.get('notes')
                            if not isinstance(notes, list): raise ValueError('Malformed OpenReview notes response')
                            fingerprint = tuple(n.get('id') for n in notes)
                            if notes and fingerprint == previous: raise RuntimeError('OpenReview pagination did not advance')
                            previous = fingerprint
                            for note in notes:
                                identity = note.get('id')
                                if not identity or identity in seen: continue
                                seen.add(identity)
                                records.append((venue, group, note))
                            offset += len(notes)
                            count = data.get('count')
                            if not notes:
                                if isinstance(count, int) and offset < count: raise RuntimeError('OpenReview pagination incomplete')
                                break
                            if isinstance(count, int) and offset >= count or len(notes) < 1000 and count is None: break
                        else: raise RuntimeError(f'{group}: OpenReview page limit reached; increase max_pages')
                except Exception as exc:
                    self.failures.append(f'{venue}: {exc}')
                    logger.warning(f'OpenReview {venue}: {exc}')
        return records

    def convert_to_paper(self, raw, diagnostics=None):
        def count(stage):
            if diagnostics is not None:
                diagnostics[stage] = diagnostics.get(stage, 0) + 1
        count("examined")
        venue, group, note = raw
        if not isinstance(note, Mapping) or not isinstance(note.get('content'), Mapping):
            raise ValueError('OpenReview note content must be a mapping')
        readers = note.get('readers', [] if self.authenticated else ['everyone'])
        if note.get('ddate') or not isinstance(readers, list) or 'everyone' not in readers or not no_exclusions(note): return None
        count('public_acl')
        content = public_fields(note['content'])
        if 'withdraw' in str(value(content, 'venue', '')).casefold(): return None
        title, abstract = clean_abstract(value(content, 'title', '')), clean_abstract(value(content, 'abstract', ''))
        if not title: return None
        count('valid_title_not_withdrawn')
        status_ids = self.venue_ids.get(group, {})
        venue_id = value(content, 'venueid')
        status, kind = 'unverified', 'preprint'
        if venue_id and venue_id == status_ids.get('under_review'):
            status = 'under_review'
        elif venue_id and venue_id == status_ids.get('decision_pending'):
            status = 'decision_pending'
        elif venue_id and venue_id == status_ids.get('accepted') and type(note.get('pdate')) is int and note['pdate'] > 0:
            status = 'published'
            kind = 'journal' if venue == 'TMLR' else 'conference'
        # odate is first public visibility. Older schemas may expose only cdate.
        timestamp = note['pdate'] if status == 'published' else note.get('odate') or note.get('cdate') or note.get('tcdate')
        if not timestamp: raise ValueError('OpenReview submission has no public/creation timestamp')
        published = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
        count('valid_date')
        if published < self.since:
            count('older_than_window')
            return None
        if published > self.until:
            count('future_date')
            return None
        count('within_window')
        author_keywords = [text for key in ('keywords', 'primary_keyword', 'secondary_keyword', 'free_keyword_1', 'free_keyword_2')
                           for text in strings(value(content, key))]
        if not contains([title, abstract, *author_keywords], self.keywords): return None
        count('keyword_match')
        areas = [text for key in ('primary_area', 'secondary_area', 'subject_areas', 'subject_area', 'area') for text in strings(value(content, key))]
        if contains(areas, [term for subject in self.subjects for term in SUBJECTS[subject]]):
            reason = 'subject field matched configured area'
        elif 'Machine Learning' in self.subjects:
            # These five verified main venues are ML venues, not a universal taxonomy.
            reason = 'Machine Learning venue-scope fallback (subject absent or venue-specific taxonomy)'
            self.fallback_count += 1
        elif not areas and contains([title, abstract, *author_keywords], [term for s in self.subjects for term in SUBJECTS[s]]):
            reason = 'subject text fallback (venue has no subject field)'
            self.fallback_count += 1
        else: return None
        count('subject_match')
        logger.debug(f'OpenReview {note["id"]}: {reason}')
        identity = quote(note['id'], safe='')
        authors = names(strings(value(content, 'authors', [])))
        anonymous = {'anonymous', 'anonymous author', 'anonymous authors', 'anon'}
        anonymized = bool(authors) and all(a.casefold() in anonymous for a in authors)
        authors = [] if anonymized else authors
        authors_status = ('anonymized' if anonymized else 'provided' if authors else
                          'not_public' if 'authors' in note['content'] and 'authors' not in content else 'not_provided')
        affiliation_keys = ('affiliations', 'author_affiliations')
        affiliations = names([text for key in affiliation_keys for text in strings(value(content, key, []))])
        affiliations_status = ('provided' if affiliations else 'not_public'
            if any(key in note['content'] and key not in content for key in affiliation_keys) else 'not_provided')
        metadata_url = API + '/notes?id=' + identity
        return Paper(source='openreview', title=title, abstract=abstract,
                     authors=authors, affiliations=affiliations or None,
                     authors_status=authors_status, affiliations_status=affiliations_status,
                     authors_source='OpenReview' if authors else None, authors_source_url=metadata_url if authors else None,
                     affiliations_source='OpenReview' if affiliations else None,
                     affiliations_source_url=metadata_url if affiliations else None,
                     url='https://openreview.net/forum?id=' + identity,
                     pdf_url='https://openreview.net/pdf?id=' + identity if value(content, 'pdf') else None,
                     published=published, subject_match_reason=reason, abstract_source='OpenReview' if abstract else None,
                     publication_kind=kind, publication_status=status, publication_venue=group,
                     journal='Transactions on Machine Learning Research' if kind == 'journal' else None)

    def retrieve_papers(self):
        # Keyword filtering includes author keywords, unlike the common title/abstract filter.
        if not self.options.get('enabled', True): return []
        result = []
        for raw in self._retrieve_raw_papers():
            try:
                paper = self.convert_to_paper(raw)
                if paper: result.append(paper)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                self.failures.append(f'{raw[0]}: malformed submission ({type(exc).__name__})')
        if self.fallback_count:
            logger.warning(f'OpenReview: {self.fallback_count} selections used documented subject fallback; inspect subject_match_reason')
        return result
