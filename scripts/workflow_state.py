"""Restore/save a data branch without switching the development checkout."""
import argparse
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

BRANCH = 'paper-state'
FILES = {'recommendations.json': 'data/recommendations.json', 'journal_catalog.json': 'data/journal_catalog.json',
         'llm_pricing.json': 'data/llm_pricing.json', 'feed.xml': 'public/feed.xml'}


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


def save():
    existing = {name: Path(path) for name, path in FILES.items() if Path(path).exists()}
    if not existing:
        return
    previous = remote_state()
    with TemporaryDirectory() as td:
        config = os.environ.copy()
        config.update(GIT_INDEX_FILE=str(Path(td) / 'index'),
                      GIT_AUTHOR_NAME='github-actions[bot]', GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
                      GIT_COMMITTER_NAME='github-actions[bot]', GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com')
        git('read-tree', previous if previous else '--empty', env=config)
        for name, path in existing.items():
            blob = git('hash-object', '-w', str(path)).decode()
            git('update-index', '--add', '--cacheinfo', f'100644,{blob},{name}', env=config)
        tree = git('write-tree', env=config).decode()
        if previous and tree == git('rev-parse', f'{previous}^{{tree}}').decode():
            return
        parent = ['-p', previous] if previous else []
        commit = git('commit-tree', tree, *parent, '-m', 'Update recommendation history', env=config).decode()
        git('push', 'origin', f'{commit}:refs/heads/{BRANCH}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['restore', 'save'])
    args = parser.parse_args()
    (restore if args.command == 'restore' else save)()
