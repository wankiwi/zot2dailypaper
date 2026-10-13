"""Read-only official OpenReview probe; print only statuses/counts, never content."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
import requests
from hydra import compose, initialize_config_dir
from loguru import logger
from zot2dailypaper.retriever.openreview_retriever import OpenReviewRetriever, API, no_exclusions
from zot2dailypaper.openreview_auth import OpenReviewLoginError, safe_error_category


class ProbeClient:
    """At most 22 requests, no retries/redirects, one request per second."""
    def __init__(self, max_requests=22):
        self.max_requests = max_requests
        self.client = requests.Session()
        self.headers = self.client.headers
        self.headers['User-Agent'] = 'zot2dailypaper/1.0'
        self.calls, self.last_start, self.last_status = 0, 0, None
        self.stage = 'login'
        self.error_category = None

    def request(self, method, url, **kwargs):
        if url not in {API + '/login', API + '/groups', API + '/notes'} or self.calls >= self.max_requests:
            raise RuntimeError('Probe request boundary exceeded')
        time.sleep(max(0, 1 - (time.monotonic() - self.last_start)))
        self.last_start = time.monotonic()
        self.calls += 1
        self.last_status = None
        response = self.client.request(method, url, **kwargs)
        self.last_status = response.status_code
        if response.status_code >= 400:
            self.error_category = safe_error_category(response)
        return response

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def close(self):
        self.client.close()


def validate(retriever, client):
    retriever._authenticate(client)
    if not retriever.authenticated:
        raise RuntimeError('Both OpenReview credentials are required for this validation')
    print(json.dumps({'stage': 'login', 'status': 'authenticated', 'http_status': client.last_status}))
    now = datetime.now(timezone.utc)
    retriever.until = now
    retriever.since = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=retriever.days)
    total_public = 0
    for venue in ['TMLR', 'ICLR', 'NeurIPS', 'ICML', 'CoRL']:
        client.stage = venue
        groups = list(retriever._groups(client, venue, now.year))
        if not groups:
            raise RuntimeError('No supported venue group found')
        note_count = public_count = eligible = malformed = 0
        for group, invitation in groups:
            data = retriever._get(client, '/notes', {'invitation': invitation, 'limit': 10, 'offset': 0, 'sort': 'tmdate:desc'})
            notes = data.get('notes')
            if not isinstance(notes, list):
                raise RuntimeError('Malformed notes response')
            public = [n for n in notes if isinstance(n, dict) and isinstance(n.get('readers'), list)
                      and 'everyone' in n['readers'] and no_exclusions(n) and not n.get('ddate')]
            note_count += len(notes)
            public_count += len(public)
            for note in public:
                try:
                    eligible += retriever.convert_to_paper((venue, group, note)) is not None
                except (ValueError, TypeError, KeyError, OverflowError):
                    malformed += 1
        total_public += public_count
        print(json.dumps({'venue': venue, 'http_status': client.last_status, 'group_count': len(groups), 'sample_count': note_count,
                          'public_count': public_count, 'eligible_in_configured_window': eligible, 'malformed_count': malformed}))
        if venue == 'TMLR' and not public_count:
            raise RuntimeError('Representative public-note check returned no public notes; expansion stopped')
    print(json.dumps({'status': 'success', 'request_count': client.calls, 'public_sample_count': total_public}))


def main():
    logger.disable('zot2dailypaper')
    with initialize_config_dir(config_dir=str(Path(__file__).resolve().parents[1] / 'config'), version_base=None):
        config = compose(config_name='interests')
    client = ProbeClient()
    try:
        validate(OpenReviewRetriever(config), client)
        return 0
    except Exception as exc:
        # Only the application-owned category is inspected; never output exceptions,
        # response bodies, note contents, identifiers, credentials or tokens.
        reason = ('MFA_requires_official_unattended_access'
                  if isinstance(exc, OpenReviewLoginError) and exc.category == 'MFA_required' else 'validation_blocked')
        print(json.dumps({'status': 'blocked', 'stage': client.stage, 'http_status': client.last_status,
                          'reason': reason, 'provider_error_category': client.error_category,
                          'error_type': type(exc).__name__, 'request_count': client.calls}))
        return 1
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
