"""Fail-closed daily CNY budget with a durable pre-call Git reservation.

The entire daily allowance is consumed by one run, including failed calls and
unused balance. This intentionally trades utilization for crash safety.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
import re
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4
from .llm import ModelRequests, request_policy
from .budget_calendar import (LOCAL_TIMEZONE, LedgerInvalid, window_at, migrated_ledger, legacy_digest,
                              validate_local, overlapping_legacy, next_unblocked_window)
from loguru import logger
from .pricing import review
from .api_costs import ApiCosts, summary_schedule

class BudgetUnavailable(RuntimeError):
    def __init__(self, message, *, reason='budget_guard_unavailable', day=None, retry_at=None, timezone_name='UTC'):
        super().__init__(message)
        self.reason = reason
        self.day, self.retry_at = day, retry_at
        self.timezone_name = timezone_name

# Reviewed public provider contract: explicit non-thinking, peak CNY tariff.
# Different endpoints/models fail closed; stale pricing warns and continues.
VERIFIED_PRICING = {
    'host': 'api.siliconflow.cn',
    'model': 'deepseek-ai/DeepSeek-V4-Flash',
    'input_cny_per_million': '3', 'output_cny_per_million': '9',
    'verified_on': '2026-10-01', 'valid_through': '2026-10-08',
    'pricing_source': 'https://www.siliconflow.cn/pricing',
    'model_contract': 'https://api-docs.siliconflow.cn/docs/api/chat-completions-post',
}
PROMPT_BYTES = 768
SYSTEM_BYTES = 256
FRAMING_TOKENS = 128
MAX_OUTPUT_TOKENS = 96


def utc_day():
    return datetime.now(timezone.utc).date().isoformat()


def budget_now():
    return datetime.now(timezone.utc)


def pricing_warning():
    if not VERIFIED_PRICING:
        return ''
    pricing, status, _ = review(VERIFIED_PRICING, utc_day())
    prefix = ('LLM 价格自动核验未完成；保留最后确认的保守估算和配置的每日额度，不暂停 LLM。 '
              'Automatic pricing verification unavailable. ') if status == 'unavailable' else ''
    if utc_day() > pricing['valid_through']:
        return ('LLM 价格复核已过期：继续按最后复核费率估算并执行配置的每日记账额度；'
                '若供应商涨价，实际费用可能超过估算及配置额度，请尽快复核价格。'
                ' Stale LLM pricing: estimates may understate actual charges. ' + prefix)
    return prefix


def budget_plan(config, *, with_pricing=False):
    budget = config.get('budget', {})
    if budget.get('timezone', LOCAL_TIMEZONE) != LOCAL_TIMEZONE:
        raise BudgetUnavailable('Only the approved Asia/Singapore budget timezone is supported', reason='budget_timezone_invalid')
    if budget.get('enabled', True) is not True:
        raise BudgetUnavailable('Budget guard disabled: paid calls prohibited, not unlimited')
    # CUSTOM_CONFIG supplies the amount. Never replace it with a fixed ceiling
    # or invent an allowance when the composed configuration omits it.
    try:
        cap = Decimal(str(budget.get('daily_cny')))
    except (InvalidOperation, TypeError, ValueError):
        raise BudgetUnavailable('Daily CNY budget must be an explicitly configured positive finite number') from None
    if not cap.is_finite() or cap <= 0:
        raise BudgetUnavailable('Daily CNY budget must be an explicitly configured positive finite number')
    if VERIFIED_PRICING is None:
        raise BudgetUnavailable('Exact provider pricing/non-thinking token bound is not verified; paid calls disabled')
    pricing = VERIFIED_PRICING
    try:
        base = urlsplit(str(config.api.base_url))
        port = base.port
    except ValueError as exc:
        raise BudgetUnavailable('Endpoint is not a valid verified HTTPS origin') from exc
    if base.scheme != 'https' or base.hostname != pricing['host'] or port not in (None, 443) or base.path.rstrip('/') != '/v1' or base.query or base.fragment or base.username or base.password:
        raise BudgetUnavailable('Endpoint does not match the verified pricing record')
    if config.generation_kwargs.get('model') != pricing['model']:
        raise BudgetUnavailable('Model does not match the verified pricing record')
    pricing, status, _ = review(VERIFIED_PRICING, utc_day())
    if status == 'verified':
        logger.info('LLM pricing automatically verified for {} on {}; conservative input/output CNY per million: {}/{}',
                    pricing['model'], pricing['checked_at'], pricing['input_cny_per_million'], pricing['output_cny_per_million'])
    warning = pricing_warning()
    if warning:
        logger.warning(warning)
    input_tokens = PROMPT_BYTES + SYSTEM_BYTES + FRAMING_TOKENS
    per_call = (Decimal(input_tokens) * Decimal(pricing['input_cny_per_million']) +
                Decimal(MAX_OUTPUT_TOKENS) * Decimal(pricing['output_cny_per_million'])) / Decimal(1000000)
    if not per_call.is_finite() or per_call <= 0 or per_call > cap:
        raise BudgetUnavailable('Invalid or insufficient verified budget')
    return (cap, per_call, dict(pricing)) if with_pricing else (cap, per_call)


def git(*args, **kwargs):
    try:
        result = subprocess.run(['git', *args], capture_output=True, timeout=60, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BudgetUnavailable('Budget ledger Git operation unavailable; no model call authorized') from exc
    if result.returncode:
        raise BudgetUnavailable('Budget ledger Git operation failed; no model call authorized')
    return result.stdout.strip()


def _push_ledger(parent, data, message):
    name, branch = 'llm_budget.json', 'paper-state'
    with TemporaryDirectory() as td:
        config = os.environ.copy()
        config.update(GIT_INDEX_FILE=str(Path(td)/'index'),
            GIT_AUTHOR_NAME='github-actions[bot]', GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
            GIT_COMMITTER_NAME='github-actions[bot]', GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com')
        git('read-tree', parent, env=config)
        blob = git('hash-object','-w','--stdin', input=json.dumps(data,sort_keys=True).encode()).decode()
        git('update-index','--add','--cacheinfo',f'100644,{blob},{name}',env=config)
        tree = git('write-tree',env=config).decode()
        commit = git('commit-tree',tree,'-p',parent,'-m',message,env=config).decode()
        try:
            pushed = subprocess.run(['git','push','origin',f'{commit}:refs/heads/{branch}'],capture_output=True,timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BudgetUnavailable('Budget reservation push ambiguous; no paid calls authorized') from exc
    return pushed.returncode == 0

def reserve_day(cap, day=None):
    """Compare-and-swap via fast-forward push; no force and no history rewrite."""
    day = day or utc_day()
    reservation_id = str(uuid4())  # Distinct invocations must never create identical grant commits.
    branch, name = 'paper-state', 'llm_budget.json'
    # Require existing delivery state: do not silently initialize another ledger.
    for _ in range(3):
        refs = git('ls-remote', '--heads', 'origin', f'refs/heads/{branch}').decode().split()
        if len(refs) != 2 or refs[1] != f'refs/heads/{branch}':
            raise BudgetUnavailable('Existing budget state branch is required')
        parent = refs[0]
        git('fetch', '--no-tags', '--no-write-fetch-head', 'origin', parent)
        names = git('ls-tree', '--name-only', parent, '--', name).decode().splitlines()
        if name not in names:
            raise BudgetUnavailable('Budget ledger is missing; explicit audited bootstrap required')
        try:
            data = json.loads(git('show', f'{parent}:{name}'))
        except (ValueError, TypeError) as exc:
            raise BudgetUnavailable('Invalid budget ledger; preserve and investigate') from exc
        if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1 or not isinstance(data.get('days'), dict):
            raise BudgetUnavailable('Invalid budget ledger; preserve and investigate')
        if day in data['days']:
            # Never infer that an older/crashed/rerun reservation was unused.
            # This is a normal allowance conflict, distinct from a broken guard.
            retry_at = (date.fromisoformat(day) + timedelta(days=1)).isoformat() + 'T00:00:00+00:00'
            raise BudgetUnavailable(f'Daily LLM allowance already reserved for UTC {day}; retaining original abstracts',
                                    reason='daily_budget_reserved', day=day, retry_at=retry_at)
        owner = {}
        for key, env, pattern in (('run_id', 'GITHUB_RUN_ID', r'\d{1,20}'),
                                  ('run_attempt', 'GITHUB_RUN_ATTEMPT', r'\d{1,6}'),
                                  ('head_sha', 'GITHUB_SHA', r'[0-9a-fA-F]{40}')):
            value = os.environ.get(env, '')
            if re.fullmatch(pattern, value):
                owner[key] = value
        data['days'][day] = {'reserved_cny': str(cap), 'policy': 'whole-day-no-refund',
                             'created_at': datetime.now(timezone.utc).isoformat(), 'reservation_id': reservation_id, **owner}
        if _push_ledger(parent, data, 'Reserve daily LLM budget before requests'):
            return day
        # A concurrent writer may have advanced the branch. Re-fetch and
        # inspect: never call on an ambiguous/failed reservation.
    raise BudgetUnavailable('Could not persist daily budget reservation; paid calls disabled')


class BudgetRequests(ModelRequests):
    def __init__(self, cap, per_call, day, *, failure_threshold=None, max_retry_wait=5,
                 expected_model=None, expected_endpoint=None, window=None, price_schedule=None):
        try:
            cap, per_call = Decimal(str(cap)), Decimal(str(per_call))
        except InvalidOperation:
            raise BudgetUnavailable('Invalid request reservation bounds') from None
        if not cap.is_finite() or not per_call.is_finite() or not 0 < per_call <= cap:
            raise BudgetUnavailable('Invalid request reservation bounds')
        super().__init__(failure_threshold=failure_threshold, max_retry_wait=max_retry_wait)
        self.remaining, self.per_call, self.day = cap, per_call, day
        self.expected_model, self.expected_endpoint = expected_model, expected_endpoint
        self.window = window
        self.timezone_name = window.timezone_name if window is not None else 'UTC'
        self.cap, self.price_schedule = cap, price_schedule
        self.attach_costs(ApiCosts())
        self._budget_lock = Lock()

    def attach_costs(self, costs):
        self.api_costs = costs
        costs.reserved(self.cap, self.day, self.timezone_name)

    def check_window(self):
        current = budget_now() if self.window is not None else None
        if ((self.window is not None and not self.window.start <= current < self.window.end)
                or (self.window is None and utc_day() != self.day)):
            raise BudgetUnavailable('Budget day changed; no new request until a fresh run reserves that day',
                                    reason='day_changed', day=self.day, timezone_name=self.timezone_name)

    def call(self, operation):
        # Serialize reservation and actual dispatch. Queued callers must observe
        # terminal aborts before sending, not merely before precharging a slot.
        with self._budget_lock:
            if getattr(self, '_aborted', False):
                raise BudgetUnavailable('Paid requests stopped for this reservation', reason=self._abort_reason,
                                        day=self.day, timezone_name=self.timezone_name)
            self.check_available()  # Cooldown/terminal checks precede the final UTC check and charge.
            self.check_window()
            if self.remaining < self.per_call:
                raise BudgetUnavailable('Daily LLM budget exhausted; retaining original abstract', reason='daily_budget_exhausted')
            self.remaining -= self.per_call
            try:
                return super().call(operation)
            except BudgetUnavailable as exc:
                self._aborted = True
                self._abort_reason = exc.reason
                self.remaining = Decimal(0)
                raise


def prepare_budget(config):
    policy = request_policy(config)
    # Return the exact admission snapshot; a second review could cross midnight
    # and return different rates from those that established the dispatch bound.
    cap, per_call, pricing = budget_plan(config, with_pricing=True)
    window = reserve_local_day(cap, config.get('budget', {}).get('timezone', LOCAL_TIMEZONE))
    return BudgetRequests(cap, per_call, window.day, failure_threshold=policy['failure_threshold'],
                          max_retry_wait=policy['max_retry_wait_seconds'],
                          expected_model=config.generation_kwargs.model, expected_endpoint=str(config.api.base_url), window=window,
                          price_schedule=summary_schedule(pricing))


def reserve_local_day(cap, timezone_name=LOCAL_TIMEZONE):
    """CAS cutover and local claim in one ledger; immutable old windows block overlaps.

    A winning v2 marker fences new v1 reservations because old readers reject
    version 2. Already-admitted old guards are covered by their retained UTC
    windows; those windows block all overlapping local dates, without refunds.
    """
    try:
        cap = Decimal(str(cap))
    except InvalidOperation:
        raise BudgetUnavailable('Invalid explicitly configured budget cap') from None
    if not cap.is_finite() or cap <= 0:
        raise BudgetUnavailable('Invalid explicitly configured budget cap')
    admitted = budget_now()
    reservation_id = str(uuid4())  # Stable across this call's CAS retries, unique across processes/reruns.
    try:
        window = window_at(admitted, timezone_name)
    except LedgerInvalid as exc:
        raise BudgetUnavailable(str(exc), reason='budget_timezone_invalid') from None
    for _ in range(3):
        refs = git('ls-remote', '--heads', 'origin', 'refs/heads/paper-state').decode().split()
        if len(refs) != 2 or refs[1] != 'refs/heads/paper-state':
            raise BudgetUnavailable('Existing budget state branch is required')
        parent = refs[0]
        git('fetch', '--no-tags', '--no-write-fetch-head', 'origin', parent)
        if b'llm_budget.json' not in git('ls-tree', '--name-only', parent, '--', 'llm_budget.json').splitlines():
            raise BudgetUnavailable('Budget ledger is missing; explicit audited bootstrap required')
        try:
            data = json.loads(git('show', f'{parent}:llm_budget.json'))
            snapshot_now = budget_now()
            migration = isinstance(data, dict) and type(data.get('version')) is int and data['version'] == 1
            if migration:
                data = migrated_ledger(data, parent, snapshot_now)
            validate_local(data, budget_now())
            blocked = overlapping_legacy(data['legacy_utc'], window)
            retry_at = next_unblocked_window(data, window) if blocked or window.key in data['claims'] else None
        except (ValueError, TypeError, KeyError) as exc:
            raise BudgetUnavailable('Invalid budget ledger or timestamps; preserve and investigate', reason='budget_ledger_invalid') from exc
        if blocked:
            if migration and not _push_ledger(parent, data, 'Migrate LLM budget clock preserving all UTC reservations'):
                continue  # An old/new writer won; re-read all claims before deciding.
            raise BudgetUnavailable('Legacy UTC reservation overlaps this Asia/Singapore budget day; no allowance reclaimed',
                                    reason='legacy_budget_overlap', day=window.day, retry_at=retry_at,
                                    timezone_name=LOCAL_TIMEZONE)
        if window.key in data['claims']:
            raise BudgetUnavailable(f'Daily LLM allowance already reserved for Asia/Singapore {window.day}',
                                    reason='daily_budget_reserved', day=window.day, retry_at=retry_at,
                                    timezone_name=LOCAL_TIMEZONE)
        owner = {}
        for key, env, pattern in (('run_id', 'GITHUB_RUN_ID', r'\d{1,20}'),
                                  ('run_attempt', 'GITHUB_RUN_ATTEMPT', r'\d{1,6}'),
                                  ('head_sha', 'GITHUB_SHA', r'[0-9a-fA-F]{40}')):
            value = os.environ.get(env, '')
            if re.fullmatch(pattern, value):
                owner[key] = value
        ready_at = budget_now()
        if not window.start <= ready_at < window.end or ready_at < snapshot_now:
            raise BudgetUnavailable('Budget window changed during reservation; no paid call authorized',
                                    reason='day_changed', day=window.day, timezone_name=LOCAL_TIMEZONE)
        data['claims'][window.key] = {'reserved_cny': str(cap), 'policy': 'whole-day-no-refund',
            'timezone': LOCAL_TIMEZONE, 'day': window.day, 'created_at': ready_at.isoformat(),
            'window_start_utc': window.start.isoformat(), 'window_end_utc': window.end.isoformat(),
            'reservation_id': reservation_id, **owner}
        data['claims_sha256'] = legacy_digest(data['claims'])
        # A slow Git operation may cross midnight. The returned guard still
        # belongs only to this admitted window and cannot roll into another one.
        if _push_ledger(parent, data, 'Reserve Asia/Singapore daily LLM budget before requests'):
            return window
    raise BudgetUnavailable('Could not persist local budget reservation; paid calls disabled')


def verify_paid_client(client, model, requests):
    """Bind production dispatch to the destination/model used by the reservation."""
    if requests.expected_model is not None and requests.expected_model != model:
        raise BudgetUnavailable('Request model differs from the reservation', reason='billing_unverified')
    if requests.expected_endpoint is not None:
        def origin(value):
            url = urlsplit(str(value))
            return (url.scheme, url.hostname, url.port or 443, url.path.rstrip('/'),
                    url.query, url.fragment, url.username, url.password)
        try:
            matches = origin(getattr(client, 'base_url', '')) == origin(requests.expected_endpoint)
        except ValueError:
            matches = False
        if not matches:
            raise BudgetUnavailable('Request endpoint differs from the reservation', reason='billing_unverified')


def audit_response(response, expected_model):
    """Unexpected metadata terminates queued calls; never refund uncertain billing."""
    if getattr(response, 'model', None) != expected_model:
        raise BudgetUnavailable('Provider response model does not match reserved model', reason='billing_unverified')
    usage = getattr(response, 'usage', None)
    if usage is None:
        raise BudgetUnavailable('Provider response has no billable usage metadata', reason='billing_unverified')
    counts = [getattr(usage, key, None) for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
    if any(type(count) is not int or count < 0 for count in counts):
        raise BudgetUnavailable('Provider response usage is invalid', reason='billing_unverified')
    prompt, completion, total = counts
    details = getattr(usage, 'completion_tokens_details', None)
    reasoning = getattr(details, 'reasoning_tokens', 0)
    if reasoning is None:
        reasoning = 0
    if type(reasoning) is not int or reasoning != 0 or prompt > PROMPT_BYTES + SYSTEM_BYTES + FRAMING_TOKENS or completion > MAX_OUTPUT_TOKENS or total != prompt + completion:
        raise BudgetUnavailable('Provider usage exceeded or contradicted the reserved bound', reason='billing_unverified')
    choices = getattr(response, 'choices', None)
    if not isinstance(choices, list) or len(choices) != 1 or getattr(choices[0], 'message', None) is None:
        raise BudgetUnavailable('Provider choices/message are invalid', reason='billing_unverified')
    if getattr(choices[0].message, 'reasoning_content', None):
        raise BudgetUnavailable('Provider unexpectedly emitted reasoning or extra choices', reason='billing_unverified')


def bootstrap_ledger():
    """Explicit one-time operation, never called by a paid execution path."""
    if git('rev-parse', '--is-shallow-repository') != b'false':
        raise BudgetUnavailable('Bootstrap requires a non-shallow clone with complete history')
    branch, name = 'paper-state', 'llm_budget.json'
    refs = git('ls-remote', '--heads', 'origin', f'refs/heads/{branch}').decode().split()
    if len(refs) != 2 or refs[1] != f'refs/heads/{branch}':
        raise BudgetUnavailable('Existing delivery state branch is required for bootstrap')
    parent = refs[0]
    git('fetch', '--no-tags', '--no-write-fetch-head', 'origin', parent)
    if git('ls-tree', '--name-only', parent, '--', name):
        raise BudgetUnavailable('Budget ledger already exists; bootstrap will not overwrite it')
    if git('log', '-1', '--format=%H', parent, '--', name):
        raise BudgetUnavailable('Missing ledger existed in history; recover it instead of reinitializing')
    data = {'version': 1, 'days': {}, 'initialized_at': datetime.now(timezone.utc).isoformat()}
    if not _push_ledger(parent, data, 'Initialize new LLM budget ledger explicitly'):
        raise BudgetUnavailable('Bootstrap push failed; inspect remote before retrying')


def no_retry_client(client, timeout=30):
    """Enforce the reservation's one-request contract at the paid boundary."""
    options = getattr(client, 'with_options', None)
    request_client = options(max_retries=0, timeout=timeout) if callable(options) else client
    retries = getattr(request_client, 'max_retries', None)
    if type(retries) is not int or retries != 0:
        raise BudgetUnavailable('Paid client must explicitly disable automatic retries')
    return request_client
