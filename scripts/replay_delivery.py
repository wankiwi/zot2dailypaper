"""Bounded recovery for the explicitly authorized failed daily batch."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from hydra import compose, initialize_config_dir
import httpx
from loguru import logger
import requests

from scripts.prepare_workflow import prepare
from scripts.workflow_state import _commit_files, git
from zot2dailypaper.delivery_replay import ReplayBlocked, replay
from zot2dailypaper.protocol import Paper
from zot2dailypaper.recommendation_sync import QUERY_URL
from zot2dailypaper.state import State


ROOT = Path(__file__).resolve().parents[1]
BRANCH = 'paper-state'
SNAPSHOT = '695e373bccdbe4ad7dd8e7219781686220926e2d'
DIGEST = '5b3ff4829f294e9a48c26cd14636d08554572ede530f11cc87b799421cae1018'
COUNT = 50
REPLAY_REF = 'refs/heads/delivery/replay-39507147167'


@dataclass
class View:
    sha: str
    state: State
    ledger: dict


class GitReplayStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def remote(self):
        git('fetch', '--no-tags', '--depth=1', 'origin', f'refs/heads/{BRANCH}')
        return git('rev-parse', 'FETCH_HEAD').decode()

    def load(self):
        sha = self.remote()
        directory = self.directory / sha
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'recommendations.json'
        path.write_bytes(git('show', f'{sha}:recommendations.json') + b'\n')
        exists = subprocess.run(['git', 'cat-file', '-e', f'{sha}:delivery_replays.json'], capture_output=True)
        ledger = json.loads(git('show', f'{sha}:delivery_replays.json')) if not exists.returncode else {'version': 1, 'replays': {}}
        if ledger.get('version') != 1 or not isinstance(ledger.get('replays'), dict):
            raise ReplayBlocked('Invalid replay ledger; inspect state before attempting SMTP')
        return View(sha, State(path), ledger)

    def commit(self, view, message):
        view.state.save()
        receipt = view.state.path.with_name('delivery_replays.json')
        receipt.write_text(json.dumps(view.ledger, sort_keys=True), encoding='utf-8')
        with TemporaryDirectory() as index:
            commit = _commit_files(view.sha, {'recommendations.json': view.state.path,
                                            'delivery_replays.json': receipt}, index, message)
        if commit is None:
            return True
        try:
            git('push', 'origin', f'{commit}:refs/heads/{BRANCH}')
            return True
        except subprocess.CalledProcessError:
            # A failed push may nevertheless have reached GitHub: inspect first.
            current = self.remote()
            known = subprocess.run(['git', 'merge-base', '--is-ancestor', commit, current], capture_output=True)
            if not known.returncode:
                return True
            if current != view.sha:
                return False  # Known concurrent CAS loss; safe to merge only receipts again.
            raise ReplayBlocked('paper-state commit was rejected; no automatic SMTP retry') from None


def configuration(root, environ):
    # Compose the original scheduled destination/policy, never a dispatch recipient override.
    values = dict(environ, GITHUB_EVENT_NAME='schedule', LLM_MODE='disabled', OUTPUT_CHANNEL='email',
                  WINDOW_DAYS='', RECEIVER_OVERRIDE='')
    prepare(root, values)
    with initialize_config_dir(config_dir=str(root / 'config'), version_base=None):
        config = compose(config_name='runtime')
    if config.email.receiver != environ['RECEIVER'] or config.llm.enabled:
        raise ReplayBlocked('Original recipient binding or no-model policy was not preserved')
    return config


def main():
    if (os.environ.get('GITHUB_REPOSITORY') != 'wankiwi/zot2dailypaper'
            or os.environ.get('GITHUB_REF') != REPLAY_REF or os.environ.get('GITHUB_EVENT_NAME') != 'push'):
        raise ReplayBlocked('Use only the authorized saved-batch replay branch')
    if any(name in os.environ for name in ('OPENAI_API_KEY', 'ZOTERO_KEY', 'ZOTERO_ID')):
        raise ReplayBlocked('Model/Zotero credentials must not be injected into replay')
    git('fetch', '--no-tags', '--depth=1', 'origin', SNAPSHOT)
    snapshot = json.loads(git('show', f'{SNAPSHOT}:recommendations.json'))
    config = configuration(ROOT, os.environ)

    def forbidden(*args, **kwargs):
        raise ReplayBlocked('Retrieval, model and Zotero operations are disabled for replay')

    httpx.Client.send = httpx.AsyncClient.send = forbidden
    Paper.generate_tldr = Paper.generate_affiliations = forbidden
    original = requests.Session.send

    def guarded(session, request, **kwargs):
        if request.method != 'POST' or request.url != QUERY_URL or kwargs.get('allow_redirects', True):
            raise ReplayBlocked('Only the original D1 citation-sync request is permitted during replay')
        return original(session, request, **kwargs)

    requests.Session.send = guarded
    run = os.environ['GITHUB_RUN_ID'] + ':' + os.environ['GITHUB_RUN_ATTEMPT']
    with TemporaryDirectory() as directory:
        result = replay(config, snapshot, SNAPSHOT, COUNT, DIGEST, GitReplayStore(directory), run)
    logger.info(f'Saved-batch recovery completed: status={result["status"]}, count={result["count"]}')


if __name__ == '__main__':
    main()
