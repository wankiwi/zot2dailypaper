"""Bounded public verification of the reviewed SiliconFlow CN pricing contract."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import Lock
from time import monotonic

import requests

HOST = 'api.siliconflow.cn'
MODEL = 'deepseek-ai/DeepSeek-V4-Flash'
PRICE_URL = 'https://www.siliconflow.cn/pricing'
CONTRACT_URL = 'https://api-docs.siliconflow.cn/docs/api/chat-completions-post'
CACHE_PATH = Path('data/llm_pricing.json')
MAX_BYTES = 1024 * 1024
_attempts = {}
_lock = Lock()


class PricingUnavailable(ValueError):
    pass


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def text(self):
        return ' '.join(' '.join(child.text() if isinstance(child, _Node) else child
                                 for child in self.children).split())


class _Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = _Node('root')
        self.stack, self.nodes = [self.root], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if len(self.nodes) > 50000 or len(self.stack) > 128:
            raise PricingUnavailable('source_structure_changed')
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'wbr'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack[-1].tag not in {'script', 'style'}:
            self.stack[-1].children.append(data)


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,6}(?:\.\d{1,12})?', value):
        raise PricingUnavailable('price_format_changed')
    return Decimal(value)


def _descendants(node):
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _descendants(child)


def parse_prices(text):
    """CN yuan per million tokens, taking the maximum over complete time tiers.

    Cache discounts and low-traffic timing never increase admitted requests.
    Unknown currencies, units, columns, models or conditions are not verified.
    """
    page = _Page(text)
    headers = ['厂商', '模型', '输入价格（M Tokens）', '输出价格（M Tokens）', '缓存价格（M Tokens）']
    if sum([child.text() for child in node.children if isinstance(child, _Node)] == headers
           for node in page.nodes) != 1:
        raise PricingUnavailable('price_units_or_columns_changed')
    rows = [node for node in page.nodes if node.attrs.get('id', '').startswith('pricing-row-text-')
            and any(child.tag == 'a' and child.attrs.get('title') == MODEL for child in _descendants(node))]
    if len(rows) != 1:
        raise PricingUnavailable('exact_model_row_unavailable')
    cells = [child for child in rows[0].children if isinstance(child, _Node) and child.tag == 'div'][1:]
    if len(cells) not in (3, 6):
        raise PricingUnavailable('price_tiers_changed')
    tiers, coverage = [], []
    for offset in range(0, len(cells), 3):
        values, conditions = [], []
        for cell in cells[offset:offset + 3]:
            match = re.fullmatch(r'(?:(费用发生时段: .+?) )?¥ (\d+(?:\.\d+)?)', cell.text())
            if not match:
                raise PricingUnavailable('price_currency_or_format_changed')
            conditions.append(match[1] or '')
            values.append(_number(match[2]))
        if len(set(conditions)) != 1 or not 0 <= values[2] <= values[0] or min(values[:2]) <= 0:
            raise PricingUnavailable('price_tiers_changed')
        condition = conditions[0]
        if condition:
            for period in condition.removeprefix('费用发生时段: ').split('/'):
                match = re.fullmatch(r'(\d{1,2})点～(\d{1,2})点', period)
                if not match or not 0 <= int(match[1]) < int(match[2]) <= 24:
                    raise PricingUnavailable('price_tiers_changed')
                coverage.append(tuple(map(int, match.groups())))
        elif len(cells) != 3:
            raise PricingUnavailable('price_tiers_changed')
        tiers.append(tuple(values))
    if coverage:
        position = 0
        for start, end in sorted(coverage):
            if start != position:
                raise PricingUnavailable('price_tiers_incomplete')
            position = end
        if position != 24:
            raise PricingUnavailable('price_tiers_incomplete')
    return tuple(max(tier[index] for tier in tiers) for index in range(3))


def verify_contract(text):
    page = _Page(text)
    fields = {}
    for node in page.nodes:
        children = [child for child in node.children if isinstance(child, _Node)]
        if children and children[0].text() in ('enable_thinking boolean', 'max_tokens integer', 'reasoning_effort string'):
            fields.setdefault(children[0].text(), []).append(node.text())
    thinking = fields.get('enable_thinking boolean', [])
    output = fields.get('max_tokens integer', [])
    models = fields.get('reasoning_effort string', [])
    if (not thinking or not output or not models
            or not all('false | true' in item and any(term in item for term in ('非推理模式', 'non-thinking modes')) for item in thinking)
            or not all(any(term in item for term in ('不包含思维链', '不包括思维链', 'excluding chain-of-thought')) for item in output)
            or not all(MODEL in item for item in models)):
        raise PricingUnavailable('non_thinking_contract_changed')


def _fetch_public(client, url, deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise PricingUnavailable('official_source_unavailable')
    with client.get(url, timeout=(min(3, remaining), min(8, remaining)), allow_redirects=False, stream=True,
                    headers={'Accept': 'text/html', 'User-Agent': 'zot2dailypaper-pricing-check/1'}) as response:
        if response.status_code != 200 or response.url != url or 'text/html' not in response.headers.get('Content-Type', ''):
            raise PricingUnavailable('official_source_unavailable')
        raw = bytearray()
        for chunk in response.iter_content(16384):
            raw.extend(chunk)
            if len(raw) > MAX_BYTES or monotonic() > deadline:
                raise PricingUnavailable('official_source_unavailable')
    return bytes(raw)


def _now():
    return datetime.now(timezone.utc)


def _read_cache():
    try:
        if CACHE_PATH.is_symlink() or CACHE_PATH.parent.is_symlink() or CACHE_PATH.stat().st_size > 4096:
            raise ValueError
        data = json.loads(CACHE_PATH.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or not isinstance(data.get('source_sha256'), dict)
                or not isinstance(data.get('estimate_rates'), list)):
            raise ValueError
        stamp = datetime.fromisoformat(data['verified_at'])
        if (type(data['version']) is not int or data['version'] != 1 or data['host'] != HOST or data['model'] != MODEL
                or data['currency'] != 'CNY' or data['tokens_per_unit'] != 1000000
                or stamp.tzinfo is None or stamp > _now()
                or set(data['source_sha256']) != {PRICE_URL, CONTRACT_URL}
                or any(not re.fullmatch(r'[0-9a-f]{64}', digest) for digest in data['source_sha256'].values())):
            raise ValueError
        rates = tuple(_number(value) for value in data['estimate_rates'])
        if len(rates) != 3 or min(rates[:2]) <= 0 or not 0 <= rates[2] <= rates[0]:
            raise ValueError
        return rates, stamp.astimezone(timezone.utc).date().isoformat()
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _save_cache(rates, observed, checked_at, hashes):
    if CACHE_PATH.is_symlink() or CACHE_PATH.parent.is_symlink():
        raise OSError('Unsafe pricing cache path')
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {'version': 1, 'host': HOST, 'model': MODEL, 'currency': 'CNY', 'tokens_per_unit': 1000000,
            'verified_at': checked_at, 'estimate_rates': list(map(str, rates)),
            'observed_peak_rates': list(map(str, observed)), 'source_sha256': hashes}
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=CACHE_PATH.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=True, sort_keys=True)
        temporary.replace(CACHE_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def review(record, day):
    """One attempt per process/day/record, including failures; seed dates never advance."""
    if record.get('host') != HOST or record.get('model') != MODEL:
        return dict(record), 'unsupported', ''
    key = (day, json.dumps(record, sort_keys=True), str(CACHE_PATH))
    with _lock:
        if key in _attempts:
            pricing, status, reason = _attempts[key]
            return dict(pricing), status, reason
        pricing = dict(record)
        cached = _read_cache()
        baseline = (_number(record['input_cny_per_million']), _number(record['output_cny_per_million']), Decimal(0))
        rates = tuple(max(a, b) for a, b in zip(baseline, cached[0])) if cached else baseline
        if cached:
            pricing.update(verified_on=cached[1], valid_through=cached[1])
        status, reason = 'unavailable', ''
        try:
            with requests.Session() as client:
                client.trust_env = False  # No .netrc, ambient proxy credentials or API keys.
                deadline = monotonic() + 30
                price = _fetch_public(client, PRICE_URL, deadline)
                contract = _fetch_public(client, CONTRACT_URL, deadline)
            observed = parse_prices(price.decode('utf-8'))
            verify_contract(contract.decode('utf-8'))
            rates = tuple(max(a, b) for a, b in zip(rates, observed))
            instant = _now()
            if instant.date().isoformat() != day:
                raise PricingUnavailable('verification_day_changed')
            stamp = instant.isoformat()
            hashes = {PRICE_URL: sha256(price).hexdigest(), CONTRACT_URL: sha256(contract).hexdigest()}
            _save_cache(rates, observed, stamp, hashes)
            pricing.update(verified_on=day, valid_through=day, checked_at=stamp, source_sha256=hashes)
            status = 'verified'
        except (PricingUnavailable, requests.RequestException, UnicodeError, OSError, ValueError):
            # Provider bodies and arbitrary exception text never enter logs/email.
            reason = 'official pricing, units or non-thinking contract could not be verified'
        pricing.update(input_cny_per_million=str(rates[0]), output_cny_per_million=str(rates[1]))
        if len(_attempts) >= 32:
            _attempts.clear()
        _attempts[key] = (dict(pricing), status, reason)
        return pricing, status, reason
