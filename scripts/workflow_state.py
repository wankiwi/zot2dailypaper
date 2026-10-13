"""Restore/save a data branch without switching the development checkout."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

BRANCH = 'paper-state'
FILES = {'recommendations.json': 'data/recommendations.json', 'journal_catalog.json': 'data/journal_catalog.json',
         'llm_pricing.json': 'data/llm_pricing.json', 'feed.xml': 'public/feed.xml'}
PRICING_FILES = {'llm_pricing.json': FILES['llm_pricing.json']}


def git(*args, **kwargs):
    return subprocess.run(['git', *args], check=True, capture_output=True, **kwargs).stdout.strip()


def remote_state():
    if not git('ls-remote', '--heads', 'origin', f'refs/heads/{BRANCH}'):
        return None
    git('fetch', '--no-tags', 'origin', f'refs/heads/{BRANCH}')
    return git('rev-parse', 'FETCH_HEAD').decode()


def restore():
    previous = remote_state()
    if previous:
        for name, destination in FILES.items():
            exists = subprocess.run(['git', 'cat-file', '-e', f'{previous}:{name}'], capture_output=True)
            if exists.returncode:
                continue
            content = git('show', f'{previous}:{name}')
            path = Path(destination)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content + b'\n')


def _commit_files(previous, existing, directory, message):
    config = os.environ.copy()
    config.update(GIT_INDEX_FILE=str(Path(directory) / 'index'),
                  GIT_AUTHOR_NAME='github-actions[bot]', GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
                  GIT_COMMITTER_NAME='github-actions[bot]', GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com')
    git('read-tree', previous if previous else '--empty', env=config)
    for name, path in existing.items():
        blob = git('hash-object', '-w', str(path)).decode()
        git('update-index', '--add', '--cacheinfo', f'100644,{blob},{name}', env=config)
    tree = git('write-tree', env=config).decode()
    if previous and tree == git('rev-parse', f'{previous}^{{tree}}').decode():
        return None
    parent = ['-p', previous] if previous else []
    return git('commit-tree', tree, *parent, '-m', message, env=config).decode()


def save():
    existing = {name: Path(path) for name, path in FILES.items() if Path(path).exists()}
    if not existing:
        return
    previous = remote_state()
    with TemporaryDirectory() as td:
        commit = _commit_files(previous, existing, td, 'Update recommendation history')
        if commit is None:
            return
        git('push', 'origin', f'{commit}:refs/heads/{BRANCH}')


def restore_pricing():
    previous = remote_state()
    if previous is None:
        return
    name, destination = next(iter(PRICING_FILES.items()))
    if name.encode() not in git('ls-tree', '--name-only', previous, '--', name).splitlines():
        return
    content = git('show', f'{previous}:{name}')
    with TemporaryDirectory() as td:
        _pricing_receipt(content, Path(td) / name)
    path = Path(destination)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Invalid local pricing receipt path')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content + b'\n')


def _pricing_receipt(content, path):
    from zot2dailypaper.pricing import _read_cache
    if len(content) > 4096:
        raise ValueError('Invalid pricing receipt; preserve existing state')
    path.write_bytes(content)
    verified = _read_cache(path)
    if verified is None:
        raise ValueError('Invalid pricing receipt; preserve existing state')
    return json.loads(content), verified[0]


def save_pricing():
    """CAS only the public receipt, preserving concurrent delivery/ledger writes.

    Any higher estimate observed by another writer remains a rate floor even
    when this run's newer official verification observes a lower tariff.
    """
    name, destination = next(iter(PRICING_FILES.items()))
    local = Path(destination)
    if not local.exists():
        return
    if local.is_symlink() or local.parent.is_symlink() or local.stat().st_size > 4096:
        raise ValueError('Invalid local pricing receipt; preserve existing state')
    content = local.read_bytes()
    with TemporaryDirectory() as td:
        directory = Path(td)
        local_receipt, local_rates = _pricing_receipt(content, directory / 'local.json')
        for _ in range(3):
            previous = remote_state()
            if previous is None:
                raise ValueError('Existing pricing state branch is required')
            receipt, rates = local_receipt, local_rates
            if name.encode() in git('ls-tree', '--name-only', previous, '--', name).splitlines():
                remote_receipt, remote_rates = _pricing_receipt(git('show', f'{previous}:{name}'), directory / 'remote.json')
                if datetime.fromisoformat(remote_receipt['verified_at']) > datetime.fromisoformat(receipt['verified_at']):
                    receipt = remote_receipt
                rates = tuple(max(a, b) for a, b in zip(local_rates, remote_rates))
            merged = dict(receipt, estimate_rates=list(map(str, rates)))
            path = directory / name
            path.write_text(json.dumps(merged, sort_keys=True) + '\n', encoding='utf-8')
            commit = _commit_files(previous, {name: path}, directory, 'Update verified LLM pricing receipt')
            if commit is None:
                return
            pushed = subprocess.run(['git', 'push', 'origin', f'{commit}:refs/heads/{BRANCH}'],
                                    capture_output=True, timeout=60)
            if pushed.returncode == 0:
                return
            # Re-read the winning snapshot; never force or replace other files.
    raise RuntimeError('Could not persist pricing receipt; existing state preserved')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    commands = {'restore': restore, 'save': save,
                'restore-pricing': restore_pricing, 'save-pricing': save_pricing}
    parser.add_argument('command', choices=commands)
    args = parser.parse_args()
    commands[args.command]()
