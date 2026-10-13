"""History must preserve scientific/Unicode metadata independently of locale."""
import json
from pathlib import Path

import pytest

from tests.canned_responses import make_sample_paper
from zot2dailypaper.identity import paper_id
from zot2dailypaper.state import State, paper_dict, utcnow


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
