"""Large collaborations must not block a digest or lose saved creators."""
import copy
import json
from types import SimpleNamespace

import pytest

from tests.canned_responses import make_sample_paper
from tests.test_recommendation_sync import D1, FAKE_TOKEN, SQLITE_CLIENTS, history
from zot2dailypaper.construct_email import email_plain_text, render_email
from zot2dailypaper.executor import Executor
from zot2dailypaper.identity import paper_id
from zot2dailypaper.recommendation_sync import (
    CitationAuthorsLimit, RecommendationSync, SyncUnavailable, WORKER_ORIGIN,
    citation_payload, prepare_sync_batch,
)
from zot2dailypaper.state import State
from zot2dailypaper.zotero_action import confirmation_link


@pytest.fixture(autouse=True)
def close_local_databases():
    yield
    while SQLITE_CLIENTS:
        SQLITE_CLIENTS.pop().db.close()


@pytest.mark.parametrize('count', [100, 101, 399])
def test_creator_limit_preserves_every_author_and_never_emits_partial_payload(count):
    paper = make_sample_paper(authors=[f'Author {i}' for i in range(count)])
    before = copy.deepcopy(paper)
    supported = prepare_sync_batch([paper])
    if count <= 100:
        assert supported == [paper]
        assert json.loads(citation_payload(paper))['authors'] == paper.authors
    else:
        assert supported == []
        with pytest.raises(CitationAuthorsLimit):
            citation_payload(paper)
    assert paper == before


@pytest.mark.parametrize('override', [
    {'url': 'https://127.0.0.1/paper'},
    {'url': 'https://example.org/ unsafe'},
    {'title': ''},
    {'journal': 42},
    {'authors': [f'Author {i}' for i in range(398)] + [None]},
    {'authors': [f'Author {i}' for i in range(398)] + ['\ud800']},
])
def test_overflow_does_not_hide_invalid_metadata_even_after_author_100(override):
    values = {'authors': [f'Author {i}' for i in range(399)]}
    values.update(override)
    with pytest.raises(SyncUnavailable) as error:
        prepare_sync_batch([make_sample_paper(**values)])
    assert not isinstance(error.value, CitationAuthorsLimit)


def test_worker_link_is_replaced_by_explicit_notice_not_silent_creator_truncation():
    large = make_sample_paper(authors=[f'Author {i}' for i in range(399)])
    normal = make_sample_paper(doi='10.1000/normal', url='https://example.org/normal')
    html = render_email([large, normal], zotero_action_origin=WORKER_ORIGIN, pricing_notice='')
    plain = email_plain_text(html)
    assert confirmation_link(large, WORKER_ORIGIN) not in html
    assert confirmation_link(normal, WORKER_ORIGIN) in html
    assert large.url in html
    assert '399 authors exceed the 100-author limit' in plain
    assert 'No authors were removed from the saved recommendation' in plain
    assert 'Open Article to import the full citation with Zotero' in plain
    assert large.authors == [f'Author {i}' for i in range(399)]
    legacy = render_email([large], zotero_action_origin='https://old.example.org', pricing_notice='')
    assert confirmation_link(large, 'https://old.example.org') in legacy
    assert 'Zotero save unavailable' not in legacy


def test_all_large_collaborations_need_no_d1_calls_and_show_no_worker_links(tmp_path):
    papers = [make_sample_paper(doi=f'10.1000/large-{i}',
                               authors=[f'Author {j}' for j in range(399)]) for i in range(2)]
    state = State(tmp_path / 'all-large.json')
    state.add(papers)
    state.save()
    supported = prepare_sync_batch(papers)
    client = D1()
    RecommendationSync(FAKE_TOKEN).sync(supported, state, client=client)
    assert supported == [] and client.calls == []
    html = render_email(papers, zotero_action_origin=WORKER_ORIGIN, pricing_notice='')
    assert html.count('399 authors exceed the 100-author limit') == 2
    assert '/zotero/confirm?paper=' not in html
    assert all(len(p.authors) == 399 for p in State(state.path).pending('email'))


def test_oversized_identity_is_not_hidden_by_author_limit():
    paper = make_sample_paper(doi=None, url='https://example.org/' + 'a' * 2100,
                              authors=[f'Author {i}' for i in range(399)])
    with pytest.raises(SyncUnavailable, match='identity'):
        prepare_sync_batch([paper])


@pytest.mark.parametrize('invalid', [False, True])
def test_daily_50_preserves_large_collaboration_or_blocks_all_on_malformed_record(config, tmp_path, monkeypatch, invalid):
    state, papers = history(tmp_path, 50)
    for index, paper in enumerate(papers):
        paper.title = f'Unique daily paper {index:02d}'
    papers[48].authors = [f'Author {i}' for i in range(399)]
    if invalid:
        papers[49].url = 'https://localhost/paper'
    state = State(tmp_path / 'daily.json')
    state.add(papers)
    state.save()
    originals = {key: copy.deepcopy(value['paper']) for key, value in state.records.items()}
    config.state.enabled = True
    config.state.path = str(state.path)
    config.executor.max_paper_num = 50
    config.llm.enabled = False
    config.email.zotero_action_origin = WORKER_ORIGIN
    client = D1()
    sync = RecommendationSync(FAKE_TOKEN)
    events, sends = [], []

    def verify(pending, current_state):
        events.append('sync')
        assert len(pending) == 49
        sync.sync(pending, current_state, client=client)

    def send(_, html):
        events.append('smtp')
        assert client.db.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 49
        assert '399 authors exceed the 100-author limit' in email_plain_text(html)
        assert all(p.title in html for p in papers)
        assert html.count('class="paper-action"') == 149  # 50 PDF + 50 Article + 49 Worker links.
        sends.append(html)

    monkeypatch.setattr('zot2dailypaper.executor.sync_client', lambda _: SimpleNamespace(sync=verify))
    monkeypatch.setattr('zot2dailypaper.executor.send_email', send)
    executor = Executor(config)
    monkeypatch.setattr(executor, '_recommend', lambda *args: [])
    if invalid:
        with pytest.raises(RuntimeError, match='Invalid recommendation metadata'):
            executor.run()
        assert events == [] and client.calls == [] and sends == []
        assert len(State(state.path).pending('email')) == 50
    else:
        executor.run()
        assert events == ['sync', 'smtp'] and len(sends) == 1
        assert len(client.calls) == 6
        assert not State(state.path).pending('email')
        assert client.db.execute('SELECT COUNT(*) FROM papers WHERE canonical_identity=?',
                                 (paper_id(papers[48]),)).fetchone()[0] == 0
        executor.run()
        assert len(sends) == 1  # Delivery receipt prevents a duplicate email.
    assert {key: value['paper'] for key, value in State(state.path).records.items()} == originals
