"""Deliver one immutable saved batch, without retrieval, ranking or model calls."""
from datetime import datetime, timezone
import hashlib
import re

from loguru import logger

from .construct_email import render_email
from .identity import paper_id
from .recommendation_sync import citation_payload, sync_client
from .state import load_paper, paper_dict
from .utils import send_email
from .api_costs import ApiCosts


class ReplayBlocked(RuntimeError):
    """A fixed message requiring inspection before any further SMTP attempt."""


def batch_from_snapshot(snapshot, count, digest):
    if (snapshot.get('version') != 1 or not isinstance(snapshot.get('records'), dict)
            or type(count) is not int or not 1 <= count <= 1000
            or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ReplayBlocked('Invalid saved-batch specification; no email sent')
    targets = {}
    for key, record in snapshot['records'].items():
        if record.get('channels', {}).get('email'):
            continue
        paper = load_paper(record['paper'])
        if paper_id(paper) != key:
            raise ReplayBlocked('Saved identity changed; no email sent')
        targets[key] = {'paper': paper_dict(paper), 'added': record['added']}
    actual = hashlib.sha256('\n'.join(sorted(targets)).encode()).hexdigest()
    if len(targets) != count or actual != digest:
        raise ReplayBlocked('Saved-batch count or identity digest mismatch; no email sent')
    return targets


def current_batch(state, targets, *, pending=True, allow_attempt=None):
    papers, delivered, attempts = [], 0, []
    for key, saved in targets.items():
        record = state.records.get(key)
        if record is None or record['added'] != saved['added'] or record['paper'] != saved['paper']:
            raise ReplayBlocked('Saved batch changed or disappeared; no additional email sent')
        delivered += bool(record.get('channels', {}).get('email'))
        if record.get('email_attempt') and not record.get('channels', {}).get('email'):
            attempts.append(record['email_attempt'])
        papers.append(load_paper(record['paper']))
    if pending and delivered:
        if delivered == len(targets):
            return []
        raise ReplayBlocked('Only part of the target batch is pending; inspect delivery history before retrying')
    if pending and attempts and (not allow_attempt or any(a.get('run') != allow_attempt for a in attempts)):
        raise ReplayBlocked('A previous SMTP attempt exists for this batch; inspect its outcome, do not resend')
    return papers


def replay(config, snapshot, snapshot_sha, count, digest, store, run_token):
    if not re.fullmatch(r'[0-9a-f]{40}', snapshot_sha):
        raise ReplayBlocked('An immutable source snapshot is required; no email sent')
    if config.llm.get('enabled', True):
        raise ReplayBlocked('Delivery-only replay requires llm.enabled=false')
    targets = batch_from_snapshot(snapshot, count, digest)
    key = snapshot_sha + ':' + digest
    view = store.load()
    papers = current_batch(view.state, targets)
    if not papers:
        logger.info(f'Delivery replay: all {count} target recommendations already marked delivered; no SMTP call')
        return {'status': 'already_delivered', 'count': count}
    if key in view.ledger['replays']:
        raise ReplayBlocked('A previous SMTP attempt exists for this batch; inspect its outcome, do not resend')
    # Validate every citation and render before D1 writes or the durable SMTP intent.
    for paper in papers:
        citation_payload(paper)
    costs = ApiCosts()
    costs.saved_summaries = sum(p.has_ai_summary for p in papers)
    html = render_email(papers, affiliation_max_chars=config.email.get('affiliation_max_chars', 180),
                        zotero_action_origin=config.email.get('zotero_action_origin'),
                        ranking_strategy=config.get('reranker', {}).get('strategy'),
                        pricing_notice='', api_costs=costs, llm_enabled=False,
                        delivery_only=True)  # No model calls or pricing lookup.
    metadata_sync = sync_client(config.email)
    if metadata_sync is not None:
        metadata_sync.sync(papers, view.state)
        logger.info(f'Verified {count} recommendation citations in D1 for saved-batch replay')
    logger.info(f'Delivery-only replay: {count} saved recommendations, '
                f'{sum(p.has_ai_summary for p in papers)} saved AI summaries; no retrieval or model calls')
    for _ in range(3):
        view = store.load()
        if not current_batch(view.state, targets):
            logger.info('Target batch became delivered before SMTP; no replay email sent')
            return {'status': 'already_delivered', 'count': count}
        if key in view.ledger['replays']:
            raise ReplayBlocked('A prior SMTP attempt appeared; inspect its outcome, do not resend')
        view.ledger['replays'][key] = {'status': 'smtp_started', 'run': run_token, 'count': count,
                                      'snapshot': snapshot_sha, 'identity_digest': digest,
                                      'started_at': datetime.now(timezone.utc).isoformat()}
        for identity in targets:
            view.state.records[identity]['email_attempt'] = {'status': 'smtp_started', 'run': run_token}
        if store.commit(view, 'Record saved-batch SMTP intent before replay'):
            break
    else:
        raise ReplayBlocked('Delivery state kept changing before SMTP; no email sent')
    # Recheck the actual durable marker and pending state immediately before sending.
    view = store.load()
    if not current_batch(view.state, targets, allow_attempt=run_token):
        return {'status': 'already_delivered', 'count': count}
    marker = view.ledger['replays'].get(key, {})
    if marker.get('status') != 'smtp_started' or marker.get('run') != run_token:
        raise ReplayBlocked('SMTP intent could not be verified; no email sent')
    try:
        send_email(config, html)  # Exactly one transport call; never retry SMTP here.
    except Exception:
        raise ReplayBlocked('SMTP attempt outcome requires inspection; durable intent preserved, do not retry') from None
    logger.info(f'SMTP accepted {count} saved recommendations')
    # Only retry CAS receipt persistence, preserving concurrent state/budget writes.
    io_retries = 0
    for _ in range(3):
        view = store.load()
        accepted_papers = current_batch(view.state, targets, pending=False)
        marker = view.ledger['replays'].get(key, {})
        if marker.get('run') != run_token or marker.get('status') not in ('smtp_started', 'smtp_accepted'):
            raise ReplayBlocked('SMTP accepted; receipt marker changed. Do not resend this batch')
        view.state.mark(accepted_papers, 'email')
        marker.update(status='smtp_accepted', accepted_at=datetime.now(timezone.utc).isoformat())
        try:
            committed = store.commit(view, 'Persist saved-batch SMTP acceptance and delivery receipt')
        except OSError:
            if io_retries:
                raise ReplayBlocked('SMTP accepted; local receipt write failed twice. Do not resend') from None
            io_retries += 1
            logger.warning('SMTP accepted; retrying only local receipt persistence after an I/O failure')
            continue
        if committed:
            logger.info(f'Delivery replay receipt persisted: {count} email marks; no summaries regenerated')
            return {'status': 'smtp_accepted', 'count': count}
    raise ReplayBlocked('SMTP accepted but receipt commit remained conflicted; do not resend this batch')
