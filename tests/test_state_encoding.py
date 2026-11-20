"""History must preserve scientific/Unicode metadata independently of locale."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.canned_responses import make_sample_paper
from zot2dailypaper.identity import paper_id
from zot2dailypaper.state import DeliveryUncertain, State, paper_dict, utcnow


def ascii_locale(monkeypatch):
    original_open = Path.open

    def local_open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        if 'b' not in mode and encoding in (None, 'locale'):
            encoding = 'ascii'
        return original_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, 'open', local_open)


def unicode_paper():
    return make_sample_paper(title='中文 🧪 ΔE', authors=['作者'],
                             doi='10.1000/locale', score=7.5, tldr='观察到温度效应。')


def test_history_save_roundtrip_in_non_utf8_locale(tmp_path, monkeypatch):
    ascii_locale(monkeypatch)
    paper = unicode_paper()
    state = State(tmp_path / 'state.json')
    state.add([paper])
    state.mark([paper], 'email')
    state.save()
    restored = State(state.path)
    assert not restored.pending('email')
    pending = restored.pending('rss')
    assert len(pending) == 1
    assert paper_dict(pending[0]) == paper_dict(paper)
    assert json.loads(state.path.read_bytes().decode('utf-8'))['records'][paper_id(paper)]['channels'] == {'email': True}


def test_existing_utf8_history_load_in_non_utf8_locale(tmp_path, monkeypatch):
    paper = unicode_paper()
    path = tmp_path / 'state.json'
    data = {'version': 1, 'records': {paper_id(paper): {
        'added': utcnow().isoformat(), 'paper': paper_dict(paper), 'channels': {'rss': True}}}}
    path.write_bytes(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    ascii_locale(monkeypatch)
    state = State(path)
    assert state.has(paper) and not state.pending('rss')
    assert paper_dict(state.pending('email')[0]) == paper_dict(paper)


def test_invalid_utf8_history_is_preserved_for_investigation(tmp_path):
    path = tmp_path / 'state.json'
    before = b'{"version":1,"records":{},"unknown":"\xff"}'
    path.write_bytes(before)
    with pytest.raises(UnicodeDecodeError):
        State(path)
    assert path.read_bytes() == before


def test_expired_uncertain_smtp_attempt_survives_until_reconciled(tmp_path):
    old = (utcnow() - timedelta(days=91)).isoformat()
    pending = make_sample_paper(title='Expired uncertain SMTP', doi='10.1000/uncertain')
    delivered = make_sample_paper(title='Expired delivered paper', doi='10.1000/delivered')
    state = State(tmp_path / 'history.json')
    state.add([pending, delivered])
    for record in state.records.values():
        record['added'] = old
    state.records[paper_id(pending)]['email_attempt'] = {'status': 'smtp_started', 'run': '1:1'}
    state.mark([delivered], 'email')
    state.save()

    restored = State(state.path)
    assert restored.has(pending) and not restored.has(delivered)
    with pytest.raises(DeliveryUncertain, match='Unresolved SMTP attempt'):
        restored.pending('email')
    restored.mark([pending], 'email')
    restored.save()
    assert not State(state.path).records


def test_identity_migration_preserves_related_dois_and_email_receipt(tmp_path):
    path = tmp_path / 'history.json'
    base = make_sample_paper(doi='10.21203/rs.3.rs-123/v1')
    revised = make_sample_paper(doi='10.21203/rs.3.rs-123/v2')
    base.related_dois = ['10.1000/first']
    revised.related_dois = ['10.1000/second']
    added = utcnow().isoformat()
    path.write_text(json.dumps({'version': 1, 'records': {
        'legacy-v1': {'paper': paper_dict(base), 'added': added,
                      'channels': {'rss': True}, 'email_attempt': {'status': 'smtp_started', 'run': '1:1'}},
        'legacy-v2': {'paper': paper_dict(revised), 'added': added, 'channels': {'email': True}},
    }}), encoding='utf-8')

    restored = State(path)
    assert not restored.pending('email') and not restored.pending('rss')
    assert 'email_attempt' not in restored.records[paper_id(base)]
    assert restored.has(make_sample_paper(title='Another title', doi='10.1000/second'))
    restored.save()
    assert State(path).has(make_sample_paper(title='New title', doi='10.1000/first'))


def test_legacy_pending_record_without_channel_map_can_store_receipt(tmp_path):
    paper = make_sample_paper()
    path = tmp_path / 'history.json'
    path.write_text(json.dumps({'version': 1, 'records': {paper_id(paper): {
        'added': utcnow().isoformat(), 'paper': paper_dict(paper)}}}), encoding='utf-8')
    restored = State(path)
    batch = restored.pending('email')
    assert len(batch) == 1
    restored.mark(batch, 'email')
    restored.save()
    assert not State(path).pending('email')


def test_sent_arxiv_version_remains_deduped_after_new_doi_and_title(tmp_path):
    old = make_sample_paper(title='Earlier title', doi=None, url='http://arxiv.org/abs/2611.00001v1')
    revised = make_sample_paper(title='Edited title', doi='10.48550/arXiv.2611.00001',
                               url='https://export.arxiv.org/abs/2611.00001v2')
    history = State(tmp_path / 'history.json')
    history.add([old])
    assert history.has(revised)
    history.mark([old], 'email')
    history.save()

    restored = State(history.path)
    assert restored.has(revised)
    assert not restored.pending('email')
    assert not restored.has(make_sample_paper(title='Another paper', url='https://arxiv.org/abs/2611.00002'))
