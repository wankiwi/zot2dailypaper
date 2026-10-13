"""Cross-source deduplication must retain OpenReview's public-field boundary."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from omegaconf import OmegaConf

from zot2dailypaper.executor import Executor
from zot2dailypaper.identity import deduplicate
from zot2dailypaper.protocol import Paper
from zot2dailypaper.reranker.base import BaseReranker
from zot2dailypaper.retriever.openreview_retriever import OpenReviewRetriever


TITLE = 'Atomistic learning benchmark'
NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def offline_config():
    return OmegaConf.create({
        'source': {'openreview': {'venues': ['TMLR'], 'subject_areas': ['Machine Learning'],
                                'keywords': ['atomistic'], 'window_days': 7, 'max_pages': 1}},
        'preprint_interests': None,
        'interest_profile': {'keywords': ['atomistic'], 'keyword_weight': 1, 'zotero_weight': 0},
        'reranker': {}, 'llm': {'enabled': False},
        'executor': {'exclude_existing': False, 'min_score': -10, 'min_score_scale': 'legacy',
                     'quotas': None},
        'abstracts': {'enabled': False}, 'metadata': {'enabled': False},
        'output': {'email': {'enabled': False}},
    })


def official_note(identity='blind', hidden='anonymous'):
    retriever = OpenReviewRetriever(offline_config())
    retriever.since, retriever.until = NOW - timedelta(days=7), NOW
    fields = {'title': {'value': TITLE}, 'abstract': {'value': 'An atomistic benchmark.'},
              'affiliations': {'value': ['Withheld Institute'], 'readers': ['TMLR/Authors']}}
    if hidden == 'anonymous':
        fields['authors'] = {'value': ['Anonymous']}
    elif hidden == 'private':
        fields['authors'] = {'value': ['Withheld Author'], 'readers': ['TMLR/Authors']}
    else:
        fields['authors'] = {'value': ['Public Author']}
        fields['affiliations'] = {'value': ['Public Institute']}
    note = {'id': identity, 'readers': ['everyone'],
            'odate': int((NOW - timedelta(days=1)).timestamp() * 1000), 'content': fields}
    if hidden == 'public':
        retriever.venue_ids['TMLR'] = {'accepted': 'TMLR'}
        fields['venueid'] = {'value': 'TMLR'}
        note['pdate'] = note['odate']
    paper = retriever.convert_to_paper(('TMLR', 'TMLR', note))
    assert paper is not None
    return paper


def public_arxiv():
    return Paper('arxiv', TITLE, ['Public Author'], 'An atomistic public abstract.',
                 'https://arxiv.org/abs/2610.12345v1', affiliations=['Public Institute'])


@pytest.mark.parametrize('hidden', ['anonymous', 'private'])
@pytest.mark.parametrize('blind_first', [True, False])
@pytest.mark.parametrize('public_source', ['arxiv', 'different_openreview_note'])
def test_title_only_matches_never_merge_openreview_notes(hidden, blind_first, public_source):
    blind = official_note(hidden=hidden)
    public = public_arxiv() if public_source == 'arxiv' else official_note('other', 'public')
    papers = [blind, public] if blind_first else [public, blind]
    merged = deduplicate(papers)
    assert len(merged) == 2
    assert not blind.authors and not blind.affiliations
    assert blind.authors_status == ('anonymized' if hidden == 'anonymous' else 'not_public')
    assert blind.affiliations_status == 'not_public'
    assert public.authors == ['Public Author'] and public.affiliations == ['Public Institute']
    if public_source == 'different_openreview_note':
        assert public.publication_status == 'published' and public.publication_kind == 'journal'


def test_same_official_note_identity_can_adopt_newly_public_metadata():
    blind, public = official_note(), official_note(hidden='public')
    merged = deduplicate([blind, public])
    assert len(merged) == 1 and merged[0].url.endswith('id=blind')
    assert merged[0].authors == ['Public Author']
    assert merged[0].affiliations == ['Public Institute']
    assert merged[0].authors_status == merged[0].affiliations_status == 'provided'


def test_non_openreview_title_deduplication_is_preserved():
    first, second = public_arxiv(), public_arxiv()
    second.url = 'https://publisher.example/article'
    assert deduplicate([first, second]) == [first]


@pytest.mark.parametrize('blind_position', [0, 1, 2])
def test_openreview_title_does_not_shadow_normal_title_deduplication(blind_position):
    first, second = public_arxiv(), public_arxiv()
    second.url = 'https://publisher.example/article'
    blind = official_note()
    papers = [first, second]
    papers.insert(blind_position, blind)
    merged = deduplicate(papers)
    assert len(merged) == 2 and any(p is blind for p in merged)
    assert not blind.authors and not blind.affiliations


class OfflineReranker(BaseReranker):
    def get_similarity_score(self, left, right):
        return np.full((len(left), len(right)), 0.5)


def test_executor_retains_blind_note_without_inferred_names(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Offline retrieval regression crossed an external boundary')
    monkeypatch.setattr('requests.sessions.Session.request', forbidden)
    monkeypatch.setattr('zot2dailypaper.executor.OpenAI', forbidden)
    monkeypatch.setattr('zot2dailypaper.executor.prepare_budget', forbidden)
    cfg = offline_config()
    runner = Executor.__new__(Executor)
    runner.config = cfg
    runner.include_path_patterns = runner.ignore_path_patterns = None
    runner.fetch_zotero_corpus = lambda: []
    runner.library_dois = runner.library_titles = runner.library_titles_without_doi = set()
    runner.reranker = OfflineReranker(cfg)
    blind = official_note()
    runner.retrievers = {
        'openreview': SimpleNamespace(retrieve_papers=lambda: [blind]),
        'arxiv': SimpleNamespace(retrieve_papers=lambda: [public_arxiv()]),
    }
    runner.openai_client = None
    state = SimpleNamespace(has=lambda _: False)
    errors = []
    ranked = runner._recommend(state, errors, maximum=10, workers=1)
    assert not errors and len(ranked) == 2
    retained = next(p for p in ranked if p.source == 'openreview')
    assert retained.url.endswith('id=blind') and not retained.authors and not retained.affiliations
    assert retained.authors_status == 'anonymized' and retained.affiliations_status == 'not_public'
