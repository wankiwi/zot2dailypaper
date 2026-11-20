"""A DOI bridge must not erase the arXiv dedup key before delivery history."""
from hashlib import sha256
import json

import pytest

from zot2dailypaper.delivery_replay import batch_from_snapshot, current_batch
from zot2dailypaper.identity import deduplicate, paper_id
from zot2dailypaper.protocol import Paper
from zot2dailypaper.state import State


def arxiv(version, title, doi=None):
    return Paper('arxiv', title, [], 'Abstract', f'https://arxiv.org/abs/2611.01234v{version}',
                 doi=doi)


@pytest.fixture
def bridge():
    return [
        arxiv(2, 'Revised preprint title', '10.1234/published'),
        Paper('journals', 'Journal title', [], 'Published abstract', 'https://example.org/123',
              doi='10.1234/published', journal='Journal'),
    ]


@pytest.mark.parametrize('reverse', [False, True])
def test_doi_bridge_preserves_arxiv_match_against_delivered_v1(tmp_path, bridge, reverse):
    path = tmp_path / 'history.json'
    history = State(path)
    v1 = arxiv(1, 'Initial preprint title')
    history.add([v1])
    history.mark([v1], 'email')
    history.save()
    [candidate] = deduplicate(bridge[::-1] if reverse else bridge)
    assert candidate.source == 'journals'
    assert paper_id(candidate) == 'doi:10.1234/published'
    assert State(path).has(candidate)


@pytest.mark.parametrize('reverse', [False, True])
def test_doi_bridge_alias_survives_reload_without_mutating_replay(tmp_path, bridge, reverse):
    path = tmp_path / 'history.json'
    history = State(path)
    [candidate] = deduplicate(bridge[::-1] if reverse else bridge)
    history.add([candidate])
    history.save()
    snapshot = json.loads(path.read_text())
    [key] = snapshot['records']
    assert key == 'doi:10.1234/published'
    assert snapshot['records'][key]['arxiv_aliases'] == ['arxiv:2611.01234']
    assert 'arxiv_aliases' not in snapshot['records'][key]['paper']
    ledger = State(path)
    assert ledger.has(arxiv(1, 'Initial preprint title'))
    targets = batch_from_snapshot(snapshot, 1, sha256(key.encode()).hexdigest())
    [restored] = current_batch(ledger, targets)
    assert paper_id(restored) == key
