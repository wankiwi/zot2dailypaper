"""Full saved-batch D1/SMTP recovery with synthetic state, transports and local Git."""
import copy
import hashlib
import json
import subprocess
from types import SimpleNamespace
import shutil

from omegaconf import OmegaConf
import pytest

from scripts.replay_delivery import GitReplayStore, View
from scripts import replay_delivery as runner
from tests.canned_responses import make_sample_paper
from tests.test_recommendation_sync import D1, FAKE_TOKEN, SQLITE_CLIENTS
from zot2dailypaper import delivery_replay as module
from zot2dailypaper.identity import paper_id
from zot2dailypaper.recommendation_sync import RecommendationSync, SyncUnavailable
from zot2dailypaper.state import DeliveryUncertain, State


class Store:
    def __init__(self, path):
        self.path = path
        self.ledger = {'version': 1, 'replays': {}}
        self.version = 0
        self.commits = []

    def load(self):
        return View(str(self.version), State(self.path), copy.deepcopy(self.ledger))

    def commit(self, view, message):
        assert view.sha == str(self.version)
        view.state.save()
        self.ledger = copy.deepcopy(view.ledger)
        self.version += 1
        self.commits.append(message)
        return True


@pytest.fixture
def batch(tmp_path, monkeypatch):
    state = State(tmp_path / 'recommendations.json')
    papers = []
    for index in range(50):
        arxiv = index < 18
        paper = make_sample_paper(
            source='arxiv' if arxiv else 'journals', title=f'Saved paper {index}',
            url=f'http://arxiv.org/abs/2610.{10000+index}v1' if arxiv else f'https://example.org/paper/{index}',
            doi=None if arxiv else f'10.5555/saved-{index}',
            tldr=f'这是第{index}篇已保存的中文摘要。', tldr_status='generated', tldr_error=None,
            tldr_error_reason=None, tldr_attempts=1, summary_input_source='abstract',
            recommendation_group='preprints' if index < 20 else 'journals' if index < 45 else 'random')
        papers.append(paper)
    state.add(papers); state.save()
    snapshot = json.loads(state.path.read_text())
    digest = hashlib.sha256('\n'.join(sorted(snapshot['records'])).encode()).hexdigest()
    config = OmegaConf.create({'email': {'receiver': 'original@example.org', 'sender': 'sender@example.org'},
                              'llm': {'enabled': False}, 'reranker': {}})
    client = D1()
    sync = RecommendationSync(FAKE_TOKEN)
    events = []
    def verify(papers, state):
        events.append('d1')
        sync.sync(papers, state, client=client)
    monkeypatch.setattr(module, 'sync_client', lambda _: SimpleNamespace(sync=verify))
    sends = []
    def send(config, html):
        events.append('smtp')
        assert config.email.receiver == 'original@example.org'
        assert 'http://arxiv.org/abs/' not in html
        assert all(p.tldr in html for p in papers)
        assert client.db.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 50
        sends.append(html)
    monkeypatch.setattr(module, 'send_email', send)
    value = SimpleNamespace(state=state, snapshot=snapshot, digest=digest, config=config, client=client,
                            events=events, sends=sends, store=Store(state.path))
    yield value
    client.db.close(); SQLITE_CLIENTS.remove(client)


def run(batch, store=None, run='run:1'):
    return module.replay(batch.config, batch.snapshot, 'a' * 40, 50, batch.digest, store or batch.store, run)


def test_saved_50_end_to_end_sync_before_smtp_and_repeat_is_noop(batch):
    assert run(batch) == {'status': 'smtp_accepted', 'count': 50}
    assert batch.events == ['d1', 'smtp'] and len(batch.sends) == 1
    assert len(batch.client.calls) == 6
    assert not State(batch.state.path).pending('email')
    assert all(record['paper']['tldr_attempts'] == 1 for record in State(batch.state.path).records.values())
    marker = next(iter(batch.store.ledger['replays'].values()))
    assert marker['status'] == 'smtp_accepted'
    assert run(batch, run='different-run:1')['status'] == 'already_delivered'
    assert len(batch.sends) == 1 and batch.events == ['d1', 'smtp']


def test_smtp_uncertainty_preserves_intent_and_blocks_another_attempt(batch, monkeypatch):
    attempts = []
    def uncertain(*args):
        attempts.append(1)
        assert next(iter(batch.store.ledger['replays'].values()))['status'] == 'smtp_started'
        raise OSError('synthetic connection lost after DATA')
    monkeypatch.setattr(module, 'send_email', uncertain)
    with pytest.raises(module.ReplayBlocked, match='outcome requires inspection'):
        run(batch)
    state = State(batch.state.path)
    assert sum(not r['channels'].get('email') for r in state.records.values()) == 50
    with pytest.raises(DeliveryUncertain, match='Unresolved SMTP attempt'):
        state.pending('email')
    with pytest.raises(module.ReplayBlocked, match='previous SMTP attempt'):
        run(batch, run='another:1')
    assert len(attempts) == 1 and batch.events == ['d1']


def test_transient_local_receipt_io_failure_retries_only_receipt(batch, monkeypatch):
    original = batch.store.commit
    failures = []
    def commit(view, message):
        if 'acceptance' in message and not failures:
            failures.append(1)
            raise OSError('synthetic local write failure')
        return original(view, message)
    monkeypatch.setattr(batch.store, 'commit', commit)
    assert run(batch)['status'] == 'smtp_accepted'
    assert len(batch.sends) == 1 and failures == [1]


def test_persistent_post_acceptance_failure_never_retries_smtp(batch, monkeypatch):
    original = batch.store.commit
    def commit(view, message):
        if 'acceptance' in message:
            raise OSError('synthetic persistent disk failure')
        return original(view, message)
    monkeypatch.setattr(batch.store, 'commit', commit)
    with pytest.raises(module.ReplayBlocked, match='SMTP accepted; local receipt write failed twice'):
        run(batch)
    assert len(batch.sends) == 1
    with pytest.raises(module.ReplayBlocked, match='previous SMTP attempt'):
        run(batch, run='new-run:1')
    assert len(batch.sends) == 1


def test_concurrent_receipt_cas_loss_preserves_unrelated_new_record_without_resending(batch, monkeypatch):
    original = batch.store.commit
    changes = []
    other = make_sample_paper(title='Concurrent state change', url='https://example.org/concurrent', doi='10.5555/concurrent')
    def commit(view, message):
        if 'acceptance' in message and not changes:
            current = State(batch.store.path); current.add([other]); current.save()
            batch.store.version += 1
            changes.append(1)
            return False
        return original(view, message)
    monkeypatch.setattr(batch.store, 'commit', commit)
    assert run(batch)['status'] == 'smtp_accepted'
    assert len(batch.sends) == 1 and changes == [1]
    assert [paper_id(p) for p in State(batch.store.path).pending('email')] == [paper_id(other)]


def test_competing_intent_cannot_be_overwritten_or_sent(batch, monkeypatch):
    def changed(view, message):
        marker = next(iter(view.ledger['replays'].values()))
        marker['run'] = 'competing-run:1'
        batch.store.ledger = copy.deepcopy(view.ledger)
        batch.store.version += 1
        return False
    monkeypatch.setattr(batch.store, 'commit', changed)
    with pytest.raises(module.ReplayBlocked, match='prior SMTP attempt appeared'):
        run(batch)
    assert batch.sends == [] and next(iter(batch.store.ledger['replays'].values()))['run'] == 'competing-run:1'


def test_d1_failure_does_not_record_smtp_intent_or_send(batch, monkeypatch):
    def fail(*args):
        raise SyncUnavailable('synthetic D1 failure')
    monkeypatch.setattr(module, 'sync_client', lambda _: SimpleNamespace(sync=fail))
    with pytest.raises(SyncUnavailable): run(batch)
    assert batch.sends == [] and batch.store.commits == []


@pytest.mark.parametrize('change', ['summary', 'partial', 'count', 'digest'])
def test_changed_or_partially_delivered_target_batch_is_blocked(batch, change):
    state = State(batch.state.path)
    if change == 'summary':
        next(iter(state.records.values()))['paper']['tldr'] = 'Changed saved summary'
        state.save()
    elif change == 'partial':
        next(iter(state.records.values()))['channels']['email'] = True
        state.save()
    elif change == 'count':
        batch.snapshot['records'].pop(next(iter(batch.snapshot['records'])))
    else:
        batch.digest = '0' * 64
    with pytest.raises(module.ReplayBlocked): run(batch)
    assert batch.events == [] and batch.sends == [] and batch.store.commits == []


def test_unrelated_pending_records_are_preserved_and_never_sent(batch):
    state = State(batch.state.path)
    other = make_sample_paper(title='Unrelated pending batch', url='https://example.org/unrelated', doi='10.5555/unrelated')
    state.add([other]); state.save()
    run(batch)
    pending = State(state.path).pending('email')
    assert [paper_id(p) for p in pending] == [paper_id(other)]
    assert 'Unrelated pending batch' not in batch.sends[0]


def test_real_git_receipt_preserves_budget_and_concurrent_unrelated_state(batch, tmp_path, monkeypatch):
    remote, checkout = tmp_path / 'remote.git', tmp_path / 'checkout'
    def git(*args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True).stdout.strip()
    git('init', '--bare', str(remote)); git('init', '-b', 'main', str(checkout))
    git('config', 'user.name', 'Test', cwd=checkout); git('config', 'user.email', 'test@example.org', cwd=checkout)
    git('remote', 'add', 'origin', str(remote), cwd=checkout)
    (checkout / 'README').write_text('Working branch')
    git('add', 'README', cwd=checkout); git('commit', '-m', 'Main', cwd=checkout)
    git('switch', '-c', 'paper-state', cwd=checkout)
    (checkout / 'recommendations.json').write_text(json.dumps(batch.snapshot))
    (checkout / 'llm_budget.json').write_text('{"preserve":"reserved-day"}')
    git('add', 'recommendations.json', 'llm_budget.json', cwd=checkout)
    git('commit', '-m', 'Saved batch', cwd=checkout); git('push', 'origin', 'paper-state', cwd=checkout)
    git('switch', 'main', cwd=checkout)
    monkeypatch.chdir(checkout)
    store = GitReplayStore(tmp_path / 'replay-temp')
    assert run(batch, store)['status'] == 'smtp_accepted'
    actual = store.load()
    assert not actual.state.pending('email')
    assert git('show', f'{actual.sha}:llm_budget.json', cwd=checkout) == b'{"preserve":"reserved-day"}'
    assert git('branch', '--show-current', cwd=checkout) == b'main'
    assert git('status', '--porcelain', cwd=checkout) == b''
    assert len(batch.sends) == 1


def test_replay_configuration_forces_original_secret_recipient_and_disables_models(tmp_path, monkeypatch):
    shutil.copytree(runner.ROOT / 'config', tmp_path / 'config', ignore=shutil.ignore_patterns('runtime.yaml', 'private.yaml'))
    monkeypatch.setenv('RECEIVER', 'original@example.org')
    config = runner.configuration(tmp_path, {'RECEIVER': 'original@example.org', 'RECEIVER_OVERRIDE': 'wrong@example.org',
        'CUSTOM_CONFIG': 'email: {receiver: stale@example.org}\nllm: {enabled: true, budget: {daily_cny: 0.30}}'})
    assert config.email.receiver == 'original@example.org'
    assert not config.llm.enabled and config.llm.budget.enabled and config.llm.budget.daily_cny == .30


def test_replay_workflow_only_triggers_the_authorized_branch_with_minimal_credentials():
    import yaml
    text = (runner.ROOT / '.github/workflows/replay-delivery.yml').read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert workflow['on'] == {'push': {'branches': ['delivery/replay-39507147167']}}
    assert workflow['concurrency'] == {'group': 'daily-papers', 'cancel-in-progress': 'false'}
    assert 'secrets.OPENAI_API_KEY' not in text and 'secrets.ZOTERO_KEY' not in text and 'secrets.ZOTERO_ID' not in text
    assert 'zot2dailypaper.main' not in text and 'workflow_state.py save' not in text
    assert 'recipient' not in workflow['on']


def test_replay_entrypoint_cannot_run_from_main_or_unrelated_branch(monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'wankiwi/zot2dailypaper')
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/main')
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'push')
    with pytest.raises(module.ReplayBlocked, match='authorized saved-batch'):
        runner.main()


def test_replay_entrypoint_renders_without_pricing_network_or_cache(batch, monkeypatch):
    import httpx
    import requests
    from zot2dailypaper import construct_email
    from zot2dailypaper.protocol import Paper

    def forbidden(*args, **kwargs):
        pytest.fail('Saved-batch entrypoint attempted a pricing, retrieval or model operation')

    # Keep runner.main's process-wide safety guards scoped to this test.
    for owner, name in ((httpx.Client, 'send'), (httpx.AsyncClient, 'send'),
                        (Paper, 'generate_tldr'), (Paper, 'generate_affiliations'),
                        (requests.Session, 'send')):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    monkeypatch.setattr(construct_email, 'pricing_warning', forbidden)
    monkeypatch.setattr(requests.Session, 'send', forbidden)
    monkeypatch.setattr(runner, 'DIGEST', batch.digest)
    monkeypatch.setattr(runner, 'configuration', lambda root, environ: batch.config)
    monkeypatch.setattr(runner, 'GitReplayStore', lambda directory: batch.store)

    def saved_snapshot(*args):
        if args == ('fetch', '--no-tags', '--depth=1', 'origin', runner.SNAPSHOT):
            return b''
        assert args == ('show', f'{runner.SNAPSHOT}:recommendations.json')
        return json.dumps(batch.snapshot).encode()

    monkeypatch.setattr(runner, 'git', saved_snapshot)
    for name in ('OPENAI_API_KEY', 'ZOTERO_KEY', 'ZOTERO_ID'):
        monkeypatch.delenv(name, raising=False)
    for name, value in {'GITHUB_REPOSITORY': 'wankiwi/zot2dailypaper', 'GITHUB_REF': runner.REPLAY_REF,
                        'GITHUB_EVENT_NAME': 'push', 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}.items():
        monkeypatch.setenv(name, value)
    runner.main()
    assert batch.events == ['d1', 'smtp'] and len(batch.sends) == 1
    assert not State(batch.state.path).pending('email')
    assert next(iter(batch.store.ledger['replays'].values()))['run'] == '123:1'


def test_daily_executor_stops_before_retrieval_models_d1_or_smtp_on_uncertain_replay(batch, monkeypatch):
    from zot2dailypaper.executor import Executor
    state = State(batch.state.path)
    next(iter(state.records.values()))['email_attempt'] = {'status': 'smtp_started', 'run': 'uncertain:1'}
    state.save()
    batch.config.output = {'email': {'enabled': True}, 'rss': {'enabled': False}}
    batch.config.state = {'enabled': True, 'path': str(state.path)}
    batch.config.executor = {'max_paper_num': 50}
    value = Executor.__new__(Executor)
    value.config, value.retrievers = batch.config, {}
    value.openai_client = value.llm_http_client = value.effective_interest_weights = None
    def forbidden(*args):
        pytest.fail('Uncertain delivery reached retrieval, model or SMTP stage')
    value._recommend = forbidden
    monkeypatch.setattr('zot2dailypaper.executor.sync_client', lambda _: None)
    monkeypatch.setattr('zot2dailypaper.executor.send_email', forbidden)
    with pytest.raises(DeliveryUncertain, match='Unresolved SMTP attempt'):
        value.run()
    assert batch.events == [] and batch.sends == []
