"""Run-scoped usage estimates; never an invoice, budget grant or delivery history.

Only dispatch boundaries create attempts. No prompts, response text, request IDs,
credentials, endpoint paths or exception messages are retained here.
"""
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from html import escape
import json
import re
from threading import Lock
from urllib.parse import urlsplit


def provider_name(endpoint):
    try:
        host = urlsplit(str(endpoint)).hostname
    except ValueError:
        host = None
    return {'api.siliconflow.cn': 'SiliconFlow', 'api.openai.com': 'OpenAI',
            'api.deepseek.com': 'DeepSeek'}.get(host, 'Other provider (endpoint withheld)')


def _label(value):
    # Model identifiers only, never arbitrary URLs, credentials or provider bodies.
    if (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]{0,127}', value)
            and not value.casefold().startswith(('sk-', 'bearer'))):
        return value
    return 'unknown'


@dataclass(frozen=True)
class PriceSchedule:
    provider: str
    model: str
    kind: str
    currency: str
    input_rate: Decimal
    output_rate: Decimal
    version: str
    verified_on: str
    valid_through: str
    # None means full input rate, without claiming an unverified cache discount.
    cache_read_rate: Decimal | None = None
    cache_write_rate: Decimal | None = None

    def __post_init__(self):
        if not re.fullmatch(r'[A-Z]{3}', self.currency):
            raise ValueError('Price currency must be an ISO currency code')
        for name in ('input_rate', 'output_rate', 'cache_read_rate', 'cache_write_rate'):
            value = getattr(self, name)
            if value is not None:
                value = Decimal(str(value))
                if not value.is_finite() or value < 0:
                    raise ValueError('Price rates must be finite and non-negative')
                object.__setattr__(self, name, value)


def summary_schedule(record):
    """Snapshot the SAME conservative rates used by the existing budget guard.

    Seed/public review rates use the peak input price for ALL input tokens. Cache
    discounts and off-peak tiers are deliberately not applied to this estimate.
    """
    identity = {key: record[key] for key in ('host', 'model', 'input_cny_per_million',
                                            'output_cny_per_million', 'verified_on', 'valid_through')}
    version = 'siliconflow-peak-v1-' + sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    return PriceSchedule('SiliconFlow', record['model'], 'chat', 'CNY',
                         record['input_cny_per_million'], record['output_cny_per_million'],
                         version, record['verified_on'], record['valid_through'])


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _money(amount):
    # Never round a small, positive known estimate to a displayed zero.
    return '<0.00000001' if 0 < amount < Decimal('0.00000001') else f'{amount:.8f}'


@dataclass
class Attempt:
    provider: str
    model: str
    kind: str
    retry: bool
    schedule: PriceSchedule | None
    failed: bool = False
    usage: dict = field(default_factory=dict)
    issue: str = 'usage unavailable'
    amount: Decimal | None = None

    def observe(self, response):
        self.usage, self.amount, self.issue = {}, None, 'usage unavailable'
        usage = _get(response, 'usage')
        if usage is None:
            return
        is_embedding = self.kind == 'embedding'
        inputs = _get(usage, 'prompt_tokens', _get(usage, 'input_tokens'))
        outputs = 0 if is_embedding else _get(usage, 'completion_tokens', _get(usage, 'output_tokens'))
        prompt_details = _get(usage, 'prompt_tokens_details', _get(usage, 'input_tokens_details'))
        output_details = _get(usage, 'completion_tokens_details', _get(usage, 'output_tokens_details'))
        values = {
            'input': inputs, 'output': outputs,
            'cache_read': _get(prompt_details, 'cached_tokens', _get(usage, 'prompt_cache_hit_tokens')),
            'cache_write': _get(prompt_details, 'cache_write_tokens', _get(usage, 'prompt_cache_write_tokens')),
            'reasoning': 0 if is_embedding else _get(output_details, 'reasoning_tokens'),
        }
        self.usage = {key: _count(value) for key, value in values.items()}
        total = _get(usage, 'total_tokens')
        known_input, known_output = self.usage['input'], self.usage['output']
        if (known_input is None or known_output is None
                or any(value is not None and _count(value) is None for value in values.values())
                or (total is not None and (_count(total) is None or total != known_input + known_output))
                or (self.usage['reasoning'] is not None and self.usage['reasoning'] > known_output)
                or sum(self.usage[key] or 0 for key in ('cache_read', 'cache_write')) > known_input):
            self.issue = 'usage invalid or incomplete'
            return
        schedule = self.schedule
        if schedule is None:
            self.issue = 'price schedule unavailable'
            return
        if _get(response, 'model') != self.model:
            self.issue = 'response model unverified'
            return
        if (schedule.provider, schedule.model, schedule.kind) != (self.provider, self.model, self.kind):
            self.issue = 'price schedule does not match provider/model/type'
            return
        cost = known_input * schedule.input_rate + known_output * schedule.output_rate
        for key, rate in (('cache_read', schedule.cache_read_rate), ('cache_write', schedule.cache_write_rate)):
            if rate is not None:
                count = self.usage[key]
                if count is None:
                    self.issue = 'cache usage split unavailable'
                    return
                cost += count * (rate - schedule.input_rate)
        # Reasoning is a subset of output, never an additional output charge.
        self.amount, self.issue = cost / Decimal(1000000), ''


class ApiCosts:
    def __init__(self):
        self._lock = Lock()
        self.attempts = []
        self.reservation = None
        self.saved_summaries = 0

    def reserved(self, amount, day, timezone_name):
        # Read the winning guard's grant. Do not read/write/reconcile remote state.
        self.reservation = (str(Decimal(str(amount))), day, timezone_name)

    @contextmanager
    def attempt(self, endpoint, model, kind, *, schedule=None, retry=False):
        event = Attempt(provider_name(endpoint), _label(model), kind, bool(retry), schedule)
        with self._lock:
            self.attempts.append(event)
        try:
            yield event
        except BaseException as exc:
            event.failed = True
            if not event.usage:
                # Some failing responses include usage; retain only numeric fields.
                event.observe(getattr(exc, 'body', None))
                if not event.usage:
                    # The SDK may expose only the nested `error` as body, while
                    # billable usage sits beside it in the full HTTP JSON.
                    decode = getattr(getattr(exc, 'response', None), 'json', None)
                    if callable(decode):
                        try:
                            event.observe(decode())
                        except (ValueError, TypeError):
                            pass
            raise

    def lines(self, *, llm_enabled=True, delivery_only=False):
        lines = ['API 费用 / API cost — 本次运行 / this run',
                 '按供应商返回用量和价格表估算；不是供应商账单。 Usage-priced estimates, not provider invoices; '
                 'no account balance, taxes or currency conversion. Model API usage only; prior runs are excluded.']
        with self._lock:
            events = list(self.attempts)
        if delivery_only:
            lines.append('Delivery-only replay: no new model API calls or summary generation.')
        if not llm_enabled:
            lines.append('LLM summary generation disabled; other API types are reported independently.')
        totals = defaultdict(Decimal)
        groups = defaultdict(list)
        for event in events:
            groups[(event.provider, event.model, event.kind, event.schedule)].append(event)
            if event.amount is not None:
                totals[event.schedule.currency] += event.amount
        unknown = sum(event.amount is None for event in events)
        if not events:
            lines.append('No model API calls: new estimated API expenditure 0 (no currency charge).')
        else:
            amounts = '; '.join(f'{currency} {_money(amount)}' for currency, amount in sorted(totals.items()))
            lines.append(('Known estimated subtotal: ' + amounts if amounts else 'Estimated expenditure: unknown')
                         + (f'; INCOMPLETE — {unknown}/{len(events)} attempts have unknown cost, never counted as zero.'
                            if unknown else '; all dispatched attempts priced.'))
        for (provider, model, kind, schedule), attempts in groups.items():
            amounts = [event.amount for event in attempts if event.amount is not None]
            cost = f'{schedule.currency} {_money(sum(amounts))}' if amounts else 'unknown'
            lines.append(f'{provider} / {model} / {kind}: known estimate {cost}; '
                         f'{len(attempts)} attempts, {sum(e.retry for e in attempts)} retries, '
                         f'{sum(e.failed for e in attempts)} failed, {sum(e.amount is None for e in attempts)} unknown cost.')
            counts = []
            for key in ('input', 'output', 'cache_read', 'cache_write', 'reasoning'):
                known = [event.usage.get(key) for event in attempts if event.usage.get(key) is not None]
                missing = len(attempts) - len(known)
                count = str(sum(known)) if known else 'unknown'
                counts.append(f'{key}={count}' + (f' (+{missing} unknown attempts)' if missing and known else ''))
            lines.append('Tokens: ' + '; '.join(counts) + '. Reasoning is included in output, not added again; '
                         'cache reads/writes are input subsets, not extra input.')
            if schedule:
                stale = ' (stale)' if datetime.now(timezone.utc).date().isoformat() > schedule.valid_through else ''
                lines.append(f'Price schedule: {schedule.version}; {schedule.currency}/million tokens '
                             f'input={schedule.input_rate}, output={schedule.output_rate}, '
                             f'cache_read={schedule.cache_read_rate if schedule.cache_read_rate is not None else "full input rate"}, '
                             f'cache_write={schedule.cache_write_rate if schedule.cache_write_rate is not None else "full input rate"}; '
                             f'verified {schedule.verified_on}, valid through {schedule.valid_through}{stale}. '
                             'Full input rate means no cache discount applied; estimates may differ from charges.')
            else:
                lines.append('Price schedule: unavailable; currency and cost unknown.')
            issues = sorted({event.issue for event in attempts if event.issue})
            if issues:
                lines.append('Incomplete accounting: ' + '; '.join(issues) + '.')
        if self.reservation:
            amount, day, zone = self.reservation
            lines.append(f'Budget reservation: CNY {amount} ({zone} {day}, whole-day-no-refund). '
                         'This is the reserved allowance, not estimated expenditure or a remaining account balance.')
        else:
            lines.append('No new summary budget reservation in this run; prior reservations are preserved, '
                         'and their expenditure is unknown here.')
        if self.saved_summaries:
            lines.append(f'{self.saved_summaries} previously generated summaries reused: no new generation cost; '
                         'their historical cost is not charged again or reconstructed.')
        return lines

    def html(self, **options):
        lines = self.lines(**options)
        paragraphs = '<p style="margin:8px 0;font-size:15px;"><strong>' + escape(lines[0]) + '</strong></p>'
        paragraphs += ''.join('<p style="margin:8px 0;">' + escape(line) + '</p>' for line in lines[1:])
        return ('<tr><td style="font-family:Aptos,Calibri,Arial,Helvetica,sans-serif;padding:16px;'
                'background:#edf4fa;color:#24364b;font-size:13px;line-height:1.7;overflow-wrap:anywhere;">'
                + paragraphs + '</td></tr>')
