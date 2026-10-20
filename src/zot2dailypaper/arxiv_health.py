"""Diagnostic-only daily arXiv counts, separate from delivery and budget state."""
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path

from loguru import logger


def record_health(config, scope, counts, now):
    # Manual/debug checks must not advance the production daily warning streak.
    state = config.get('state', {})
    if os.environ.get('GITHUB_EVENT_NAME') != 'schedule' or not state.get('enabled', False) or config.executor.debug:
        return
    delivery_path = Path(state.get('path', 'data/recommendations.json'))
    path = delivery_path.with_name('arxiv_health.json')
    if path == delivery_path:
        logger.warning('arXiv health path conflicts with delivery history; zero-day streak unknown')
        return
    fingerprint = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    today = now.date()
    try:
        previous = None
        if path.exists():
            if path.stat().st_size > 4096:
                raise ValueError('oversized health state')
            previous = json.loads(path.read_text(encoding='utf-8'))
            if (previous.get('version') != 1 or not isinstance(previous.get('scope'), str)
                    or type(previous.get('zero_days')) is not int or previous['zero_days'] < 0
                    or type(previous.get('day_had_candidates')) is not bool):
                raise ValueError('invalid health state')
            last_day = date.fromisoformat(previous['last_day'])
            if last_day > today:
                raise ValueError('out-of-order health observation')
            if previous['scope'] != fingerprint:
                previous = None
        same_day = previous is not None and last_day == today
        had_candidates = counts['keyword_kept'] > 0 or (same_day and previous['day_had_candidates'])
        if had_candidates:
            streak = 0
        elif same_day:
            streak = max(1, previous['zero_days'])
        elif previous is not None and last_day == today - timedelta(days=1):
            streak = previous['zero_days'] + 1
        else:
            streak = 1
        payload = {'version': 1, 'scope': fingerprint, 'last_day': today.isoformat(),
                   'day_had_candidates': had_candidates, 'zero_days': streak, 'counts': counts}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')
        temporary.replace(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning(f'arXiv health history unavailable ({type(exc).__name__}); zero-day streak unknown; preserving existing history')
        return
    logger.info(f'arXiv daily health: zero_days={streak}, day={today}, counts={counts}')
    if streak >= 3:
        logger.warning(f'arXiv zero candidates on {streak} consecutive UTC days; counts={counts}; '
                       'inspect date window, announcement/indexing delays and category/keyword filters. '
                       'The empty response is valid; this warning does not fail retrieval.')
