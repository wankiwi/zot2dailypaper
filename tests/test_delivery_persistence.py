"""SMTP acknowledgement and history failures use synthetic files/transports."""
import pytest
from omegaconf import OmegaConf

from tests.canned_responses import make_sample_paper
from zot2dailypaper.executor import Executor
from zot2dailypaper.state import State


def delivery(tmp_path, monkeypatch):
    path = tmp_path / 'history.json'
    config = OmegaConf.create({
        'output': {'email': {'enabled': True}, 'rss': {'enabled': False}},
        'email': {}, 'llm': {'enabled': False}, 'reranker': {},
        'state': {'enabled': True, 'path': str(path)},
        'executor': {'max_paper_num': 1, 'enrichment_workers': 1, 'send_empty': False}})
    accepted = []
    monkeypatch.setattr('zot2dailypaper.executor.sync_client', lambda *args: None)
    monkeypatch.setattr('zot2dailypaper.executor.render_email', lambda *args, **kwargs: 'synthetic')
    monkeypatch.setattr('zot2dailypaper.executor.send_email', lambda *args: accepted.append('fake-250'))

    def run(papers):
        value = Executor.__new__(Executor)
        value.config, value.retrievers = config, {}
        value.openai_client = value.llm_http_client = value.effective_interest_weights = None
        value._recommend = lambda *args: papers
        value.run()

    return path, accepted, run


def test_transient_post_acceptance_io_failure_retries_history_without_resending(tmp_path, monkeypatch):
    path, accepted, run = delivery(tmp_path, monkeypatch)
    original, attempts = State.save, []

    def save(state):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 2:
            raise OSError('Synthetic transient disk failure')
        original(state)

    monkeypatch.setattr(State, 'save', save)
    paper = make_sample_paper(doi='10.1000/ack-transient')
    run([paper])
    assert len(accepted) == 1 and len(attempts) == 3
    assert State(path).has(paper) and not State(path).pending('email')
    assert len(State(path).pending('rss')) == 1
    run([])
    assert len(accepted) == 1


def test_persistent_post_acceptance_failure_is_bounded_and_preserves_pending_history(tmp_path, monkeypatch):
    path, accepted, run = delivery(tmp_path, monkeypatch)
    original, attempts = State.save, []

    def save(state):
        attempts.append(len(attempts) + 1)
        if len(attempts) >= 2:
            raise OSError('Synthetic persistent disk failure')
        original(state)

    monkeypatch.setattr(State, 'save', save)
    with pytest.raises(RuntimeError, match='Pipeline completed with failures: email:'):
        run([make_sample_paper(doi='10.1000/ack-persistent')])
    assert len(accepted) == 1 and len(attempts) == 3
    assert len(State(path).pending('email')) == 1


def test_pending_history_failure_blocks_smtp_before_any_acceptance(tmp_path, monkeypatch):
    _, accepted, run = delivery(tmp_path, monkeypatch)

    def unavailable(state):
        raise OSError('Synthetic pre-send disk failure')

    monkeypatch.setattr(State, 'save', unavailable)
    with pytest.raises(OSError, match='pre-send'):
        run([make_sample_paper(doi='10.1000/ack-before')])
    assert accepted == []
