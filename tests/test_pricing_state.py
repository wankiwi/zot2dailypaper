"""Price-only workflow persistence with synthetic receipts and local bare Git."""
from decimal import Decimal
import json
from pathlib import Path
import subprocess

from omegaconf import OmegaConf
import pytest

from scripts import workflow_state
from tests.test_budget import make_git
from zot2dailypaper import budget, pricing


def receipt(rates=('6', '18', '0.6'), day='2026-10-11', digest='a'):
    return {'version': 1, 'host': pricing.HOST, 'model': pricing.MODEL,
            'currency': 'CNY', 'tokens_per_unit': 1000000,
            'verified_at': day + 'T10:00:00+00:00', 'estimate_rates': list(rates),
            'observed_peak_rates': list(rates),
            'source_sha256': {pricing.PRICE_URL: digest * 64, pricing.CONTRACT_URL: digest * 64}}


def publish(git, checkout, data):
    (checkout / 'llm_pricing.json').write_text(json.dumps(data), encoding='utf-8')
    git('add', 'llm_pricing.json')
    git('commit', '-m', 'Synthetic public pricing receipt')
    git('push', 'origin', 'HEAD:paper-state')


def snapshot(git, remote):
    head = git('rev-parse', 'refs/heads/paper-state', cwd=remote).decode().strip()
    return head, {name: git('show', f'{head}:{name}', cwd=remote)
                  for name in ('recommendations.json', 'llm_budget.json', 'llm_pricing.json')}


def test_price_only_restore_retains_known_higher_rate_when_official_check_fails(tmp_path, monkeypatch):
    git, checkout, remote = make_git(tmp_path, monkeypatch)
    publish(git, checkout, receipt())
    before = snapshot(git, remote)
    data = checkout / 'data'
    data.mkdir()
    recommendations = data / 'recommendations.json'
    recommendations.write_text('Local delivery state must not be restored or saved')
    workflow_state.restore_pricing()
    assert recommendations.read_text() == 'Local delivery state must not be restored or saved'
    assert (data / 'llm_pricing.json').exists()
    monkeypatch.setattr(pricing, 'CACHE_PATH', data / 'llm_pricing.json')
    config = OmegaConf.create({'api': {'base_url': 'https://api.siliconflow.cn/v1'},
                              'generation_kwargs': {'model': pricing.MODEL},
                              'budget': {'enabled': True, 'daily_cny': '.005'}})
    with pytest.raises(budget.BudgetUnavailable, match='insufficient'):
        budget.budget_plan(config)
    config.budget.daily_cny = '.30'
    cap, cost = budget.budget_plan(config)
    assert cap == Decimal('.30') and cost == Decimal('.00864')
    assert 'Automatic pricing verification unavailable' in budget.pricing_warning()
    assert snapshot(git, remote) == before


def test_price_only_save_retries_cas_and_preserves_concurrent_delivery_budget_and_higher_rates(tmp_path, monkeypatch):
    git, checkout, remote = make_git(tmp_path, monkeypatch)
    publish(git, checkout, receipt(('3', '9', '0.3')))
    second = tmp_path / 'second'
    git('clone', '--branch', 'paper-state', str(remote), str(second))
    git('config', 'user.name', 'Concurrent test', cwd=second)
    git('config', 'user.email', 'concurrent@example.org', cwd=second)
    data = checkout / 'data'
    data.mkdir()
    local = receipt(day='2026-10-12', digest='b')
    (data / 'llm_pricing.json').write_text(json.dumps(local), encoding='utf-8')
    (data / 'recommendations.json').write_text('stale local delivery snapshot')
    index = git('write-tree')
    original_run = subprocess.run
    winning_blobs = {}
    pushes = []

    def race(args, *a, **kw):
        if args[:3] == ['git', 'push', 'origin'] and kw.get('timeout') == 60:
            pushes.append(args)
            if len(pushes) == 1:
                (second / 'recommendations.json').write_text('{"version":1,"records":{"concurrent":"sent"}}')
                (second / 'llm_budget.json').write_text('{"version":1,"days":{},"concurrent_budget_marker":true}')
                (second / 'llm_pricing.json').write_text(json.dumps(receipt(('12', '36', '1.2'))), encoding='utf-8')
                git('add', '.', cwd=second)
                git('commit', '-m', 'Concurrent delivery budget and higher price receipt', cwd=second)
                git('push', 'origin', 'HEAD:paper-state', cwd=second)
                winning_blobs.update(snapshot(git, remote)[1])
        return original_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, 'run', race)
    workflow_state.save_pricing()
    after = snapshot(git, remote)[1]
    assert len(pushes) == 2
    assert after['recommendations.json'] == winning_blobs['recommendations.json']
    assert after['llm_budget.json'] == winning_blobs['llm_budget.json']
    merged = json.loads(after['llm_pricing.json'])
    assert merged['estimate_rates'] == ['12', '36', '1.2']
    assert merged['verified_at'] == local['verified_at'] and merged['source_sha256'] == local['source_sha256']
    assert git('write-tree') == index
    assert (data / 'recommendations.json').read_text() == 'stale local delivery snapshot'
    head = snapshot(git, remote)[0]
    workflow_state.save_pricing()
    assert snapshot(git, remote)[0] == head
    assert all('--force' not in arg for args in pushes for arg in args)


def test_price_only_save_does_not_overwrite_invalid_remote_receipt(tmp_path, monkeypatch):
    git, checkout, remote = make_git(tmp_path, monkeypatch)
    publish(git, checkout, {'invalid': 'preserve this remote receipt'})
    before = snapshot(git, remote)
    data = checkout / 'data'
    data.mkdir()
    (data / 'llm_pricing.json').write_text(json.dumps(receipt()), encoding='utf-8')
    with pytest.raises(ValueError, match='Invalid pricing receipt'):
        workflow_state.save_pricing()
    with pytest.raises(ValueError, match='Invalid pricing receipt'):
        workflow_state.restore_pricing()
    assert snapshot(git, remote) == before


def test_price_only_save_without_receipt_does_not_contact_remote(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workflow_state, 'remote_state', lambda: pytest.fail('No receipt requires no Git operation'))
    workflow_state.save_pricing()


def test_llm_validation_restores_and_persists_only_public_price_state():
    import yaml
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load((root / '.github/workflows/test.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    job = workflow['jobs']['llm-validation']
    assert job['concurrency'] == {'group': 'daily-papers', 'cancel-in-progress': 'false'}
    steps = job['steps']
    restore = next(i for i, step in enumerate(steps) if step.get('run', '').endswith('workflow_state.py restore-pricing'))
    validate = next(i for i, step in enumerate(steps) if step.get('run', '').endswith('scripts/validate_llm.py'))
    save = next(i for i, step in enumerate(steps) if step.get('run', '').endswith('workflow_state.py save-pricing'))
    assert restore < validate < save
    assert steps[save]['if'] == '${{ always() && !cancelled() }}'
    assert 'env' not in steps[restore] and 'env' not in steps[save]
