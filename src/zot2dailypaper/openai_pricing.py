"""Public-only OpenAI/ECB quote. Expired or ambiguous estimates fail closed.

No API credentials, paid endpoints, alternate hosts, redirects or retries.
Rates are conservative ceilings, not a bank settlement rate or provider invoice.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from hashlib import sha256
import json
import re
from threading import Lock
from xml.etree import ElementTree as ET
import requests

HOST = 'api.openai.com'
MODEL = 'gpt-6-luna'
PRICE_URL = 'https://developers.openai.com/api/docs/pricing.md'
FX_URL = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
# Independently checked official pages on this date. Reserve the highest
# published Luna input/cache-write and output prices, including Fast mode.
SEED = {'price_date':'2026-11-02','input_usd_per_million':'0.50',
        'output_usd_per_million':'1.00','fx_date':'2026-10-30',
        'usd_per_eur':'1.1172','cny_per_eur':'7.4326'}
MAX_AGE_DAYS = 7
SHORT_CONTEXT_TOKEN_CEILING = 272000
_lock = Lock()
_quotes = {}


class QuoteUnavailable(ValueError):
    pass


def _number(value):
    try:
        result=Decimal(str(value))
    except InvalidOperation:
        raise QuoteUnavailable('invalid_public_quote') from None
    if not result.is_finite() or result <= 0:
        raise QuoteUnavailable('invalid_public_quote')
    return result


def parse_prices(text):
    # Accept a complete known Markdown table, never guess column positions or
    # accept a discount-only row. Take maxima across all published modes.
    if not re.search(r'Prices per 1M tokens\.',text):
        raise QuoteUnavailable('openai_price_units_changed')
    columns=['input','output','cached input','30m write','12h write']
    expected=['model',*columns]
    expanded=['model',*[f'{context} context {column}'
                        for context in ('short','long') for column in columns]]
    header=None
    tier=None
    rates=[]; expanded_tiers=set()
    headings={'### Standard pricing data':'standard',
              '### Batch pricing data':'batch','#### Batch':'batch',
              '#### Flex':'flex','### Fast pricing data':'fast'}
    for line in text.splitlines():
        stripped=line.strip()
        if stripped.startswith('#'):
            tier=headings.get(stripped)
        if not line.strip().startswith('|'):
            header=None
            continue
        cells=[x.strip().strip('`') for x in line.strip().strip('|').split('|')]
        normalized=[x.casefold() for x in cells]
        if cells and normalized[0]=='model':
            header=normalized if normalized in (expected,expanded) else None
            continue
        if cells and cells[0]==MODEL:
            if header is None:
                raise QuoteUnavailable('openai_price_columns_changed')
            if len(cells)!=len(header) or not all(re.fullmatch(r'\$\d+(?:\.\d+)?',x) for x in cells[1:]):
                raise QuoteUnavailable('openai_price_format_changed')
            values=list(map(lambda x:_number(x[1:]),cells[1:]))
            for start in range(0,len(values),5):
                inputs,outputs,cached,write30,write12=values[start:start+5]
                if not cached<=inputs<=write30<=write12:
                    raise QuoteUnavailable('openai_cache_price_order_changed')
            if header==expanded:
                if tier not in ('standard','batch','flex','fast') or tier in expanded_tiers:
                    raise QuoteUnavailable('openai_price_tier_changed')
                if any(long<short for short,long in zip(values[:5],values[5:])):
                    raise QuoteUnavailable('openai_context_price_order_changed')
                expanded_tiers.add(tier)
            # The admitted request is <=1152 input tokens. budget_plan also
            # enforces the reviewed short-context boundary before using this
            # quote. Validate both groups; never mix long and short columns.
            inputs,outputs,cached,write30,write12=values[:5]
            rates.append((max(inputs,write30,write12),outputs))
    if not rates:
        raise QuoteUnavailable('exact_openai_price_row_missing')
    if expanded_tiers and expanded_tiers!={'standard','batch','flex','fast'}:
        raise QuoteUnavailable('openai_price_tiers_incomplete')
    return {'input_usd_per_million':str(max(x[0] for x in rates)),
            'output_usd_per_million':str(max(x[1] for x in rates))}


def parse_fx(text):
    try:
        root=ET.fromstring(text)
        days=[node for node in root.iter() if 'time' in node.attrib]
        if len(days)!=1:
            raise ValueError
        day=days[0]
        currencies={}
        for node in day:
            key=node.get('currency')
            if key in currencies:
                raise ValueError
            currencies[key]=node.get('rate')
        date.fromisoformat(day.attrib['time'])
        return {'fx_date':day.attrib['time'],'usd_per_eur':str(_number(currencies['USD'])),
                'cny_per_eur':str(_number(currencies['CNY']))}
    except (ET.ParseError,KeyError,TypeError,ValueError):
        raise QuoteUnavailable('ecb_quote_format_changed') from None


def _fetch(url):
    # Strictly public unauthenticated GET; bounded body, no route substitution.
    with requests.get(url,timeout=(3,8),allow_redirects=False,stream=True,
                      headers={'Accept':'text/markdown,text/xml','User-Agent':'zot2dailypaper-price-check/1'}) as r:
        if r.status_code!=200 or r.url!=url:
            raise QuoteUnavailable('public_quote_unavailable')
        chunks=[];size=0
        for chunk in r.iter_content(65536):
            size+=len(chunk)
            if size>1048576:
                raise QuoteUnavailable('public_quote_too_large')
            chunks.append(chunk)
        return b''.join(chunks).decode('utf-8',errors='strict')


def _fresh(day,today):
    try:
        elapsed=(date.fromisoformat(today)-date.fromisoformat(day)).days
    except (ValueError,TypeError):
        raise QuoteUnavailable('invalid_quote_date') from None
    if not 0<=elapsed<=MAX_AGE_DAYS:
        raise QuoteUnavailable('public_quote_expired_or_future')


def quote(today=None):
    today=today or datetime.now(timezone.utc).date().isoformat()
    with _lock:
        if today in _quotes:
            return dict(_quotes[today])
        value=dict(SEED); refreshed=[]
        for kind,url,parse in [('price',PRICE_URL,parse_prices),('fx',FX_URL,parse_fx)]:
            try:
                text=_fetch(url)
            except (QuoteUnavailable,requests.RequestException,UnicodeError):
                continue # Only a still-fresh independently checked seed remains.
            # A reachable official source whose schema/units/date changed is not
            # covered by the seed. Stop rather than guess or ignore the change.
            part=parse(text)
            if kind=='price':part['price_date']=today
            _fresh(part.get('fx_date',part.get('price_date')),today)
            value.update(part);refreshed.append(kind)
        _fresh(value['price_date'],today);_fresh(value['fx_date'],today)
        spot=_number(value['cny_per_eur'])/_number(value['usd_per_eur'])
        # 10% FX headroom, rounded upward to a tenth CNY/USD. Never report it as
        # the actual card/bank rate; no refund of a reservation on later changes.
        fx=(spot*Decimal('1.10')*10).to_integral_value(rounding=ROUND_CEILING)/10
        input_usd=_number(value['input_usd_per_million'])
        output_usd=_number(value['output_usd_per_million'])
        identity={**value,'cny_per_usd_ceiling':str(fx)}
        result={'host':HOST,'model':MODEL,'input_cny_per_million':str(input_usd*fx),
                'output_cny_per_million':str(output_usd*fx),'verified_on':value['price_date'],
                'valid_through':min((date.fromisoformat(value[key])+timedelta(days=MAX_AGE_DAYS)).isoformat()
                                    for key in ('price_date','fx_date')),
                'pricing_source':PRICE_URL,'fx_source':FX_URL,'fx_asof':value['fx_date'],
                'quote_version':'openai-cny-ceiling-v1-'+sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:12],
                'conversion_note':f"USD ceiling per million input/cache-write={input_usd}, output={output_usd}; "
                    f"CNY/USD reservation rate={fx} (ECB reference {value['fx_date']} plus 10% and rounding up); "
                    'conservative published-mode ceiling, not the actual settlement rate; no cache discount assumed.',
                'public_refresh':','.join(refreshed) or 'fresh reviewed seed'}
        _quotes[today]=result
        return dict(result)
