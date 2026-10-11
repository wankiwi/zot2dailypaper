"""Public-price checks use fixtures only, never live sources or paid APIs."""
from datetime import datetime
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from zot2dailypaper import budget, pricing
from zot2dailypaper.pricing import _fetch_public as transport


def price_page(input_rate='3', output_rate='9', cache_rate='0.30'):
    header = '<div>' + ''.join(f'<div>{s}</div>' for s in
        ['厂商', '模型', '输入价格（M Tokens）', '输出价格（M Tokens）', '缓存价格（M Tokens）']) + '</div>'
    cells = ''.join(f'<div>费用发生时段: {condition} ¥ {rate}</div>'
                    for condition, rates in [('2点～8点', ('1.50', '4.50', '0.15')),
                                              ('0点～2点/8点～24点', (input_rate, output_rate, cache_rate))]
                    for rate in rates)
    return header + f'<div id="pricing-row-text-1"><div><a title="{pricing.MODEL}">Model</a></div>{cells}</div>'


CONTRACT = f'''<div><div>enable_thinking boolean</div><p>Switches to non-thinking modes</p><code>false | true</code></div>
<div><div>max_tokens integer</div><p>excluding chain-of-thought</p></div>
<div><div>reasoning_effort string</div><p>{pricing.MODEL}</p></div>'''


def clock(monkeypatch, day='2026-10-11'):
    instant = datetime.fromisoformat(day + 'T10:00:00+00:00')
    monkeypatch.setattr(pricing, '_now', lambda: instant)
    monkeypatch.setattr(budget, 'utc_day', lambda: day)


def sources(monkeypatch, page=None, contract=CONTRACT):
    calls = []
    def fetch(client, url, deadline):
        assert client.trust_env is False
        calls.append(url)
        return (page if page is not None else price_page()).encode() if url == pricing.PRICE_URL else contract.encode()
    monkeypatch.setattr(pricing, '_fetch_public', fetch)
    return calls


def configured(config):
    config.llm.api.base_url = 'https://api.siliconflow.cn/v1'
    config.llm.generation_kwargs.model = pricing.MODEL
    return config.llm


def test_peak_prices_and_cache_are_distinct():
    assert pricing.parse_prices(price_page()) == (Decimal(3), Decimal(9), Decimal('.30'))


@pytest.mark.parametrize('replace', [
    ('M Tokens', 'K Tokens'), ('输入价格', '输出价格'), ('¥', '$'),
    (pricing.MODEL, pricing.MODEL + '-Pro'), ('0点～2点/8点～24点', '8点～24点'),
    ('9</div>', '-9</div>'), ('0.15</div>', '8</div>'),
])
def test_ambiguous_price_evidence_is_not_accepted(replace):
    with pytest.raises(pricing.PricingUnavailable):
        pricing.parse_prices(price_page().replace(*replace))


def test_duplicate_model_rows_are_rejected():
    with pytest.raises(pricing.PricingUnavailable):
        pricing.parse_prices(price_page() + price_page()[price_page().index('<div id="pricing-row-text-1">'):])


@pytest.mark.parametrize('text', ['<html>OK</html>', CONTRACT.replace('false | true', 'true'),
                                  CONTRACT.replace(pricing.MODEL, 'another-model'),
                                  CONTRACT.replace('excluding chain-of-thought', 'including chain-of-thought')])
def test_contract_changes_are_unverified(text):
    with pytest.raises(pricing.PricingUnavailable):
        pricing.verify_contract(text)


def test_current_cn_contract_terms_and_conflicting_repeated_fields():
    translated = CONTRACT.replace('Switches to non-thinking modes', '在推理模式与非推理模式之间切换。')
    translated = translated.replace('excluding chain-of-thought', '要生成的最大 token 数量，不包含思维链部分。')
    pricing.verify_contract(translated)
    with pytest.raises(pricing.PricingUnavailable):
        pricing.verify_contract(translated + CONTRACT.replace('false | true', 'true'))


def test_repeat_success_does_not_refetch_or_advance_timestamp(monkeypatch):
    clock(monkeypatch)
    calls = sources(monkeypatch)
    original = dict(budget.VERIFIED_PRICING)
    first = pricing.review(original, '2026-10-11')
    clock(monkeypatch)  # Same run/day, repeated budget admission and email render.
    second = pricing.review(original, '2026-10-11')
    assert first == second and first[1] == 'verified'
    assert calls == [pricing.PRICE_URL, pricing.CONTRACT_URL]
    assert original == budget.VERIFIED_PRICING and original['verified_on'] == '2026-10-01'
    cached = json.loads(pricing.CACHE_PATH.read_text())
    assert cached['verified_at'] == first[0]['checked_at']
    assert cached['currency'] == 'CNY' and cached['tokens_per_unit'] == 1000000


def test_repeat_failure_preserves_dates_and_has_one_attempt(monkeypatch):
    clock(monkeypatch)
    calls = []
    def unavailable(*args):
        calls.append(1)
        raise pricing.PricingUnavailable('provider content must not reach email')
    monkeypatch.setattr(pricing, '_fetch_public', unavailable)
    first = pricing.review(budget.VERIFIED_PRICING, '2026-10-11')
    assert first == pricing.review(budget.VERIFIED_PRICING, '2026-10-11')
    assert first[1] == 'unavailable' and first[0]['verified_on'] == '2026-10-01'
    assert len(calls) == 1 and not pricing.CACHE_PATH.exists()
    assert 'provider content' not in budget.pricing_warning()


def test_http_200_without_price_evidence_is_not_verified(monkeypatch):
    clock(monkeypatch)
    sources(monkeypatch, '<html>success</html>')
    record, status, _ = pricing.review(budget.VERIFIED_PRICING, '2026-10-11')
    assert status == 'unavailable' and record['valid_through'] == '2026-10-08'


@pytest.mark.parametrize('cap,calls_allowed', [('.20', 23), ('.30', 34)])
def test_price_increase_reduces_calls_without_increasing_cap(config, monkeypatch, cap, calls_allowed):
    clock(monkeypatch)
    sources(monkeypatch, price_page('6', '18', '0.6'))
    cfg = configured(config)
    cfg.budget.daily_cny = cap
    allowance, per_call = budget.budget_plan(cfg)
    assert allowance == Decimal(cap) and int(allowance // per_call) == calls_allowed
    assert not budget.pricing_warning()


def test_failure_in_next_run_keeps_last_higher_rates_and_warns(config, monkeypatch):
    clock(monkeypatch)
    sources(monkeypatch, price_page('6', '18', '0.6'))
    cfg = configured(config)
    budget.budget_plan(cfg)
    cached_before = pricing.CACHE_PATH.read_bytes()
    pricing._attempts.clear()
    clock(monkeypatch, '2026-10-12')
    def unavailable(*args):
        raise pricing.PricingUnavailable('offline')
    monkeypatch.setattr(pricing, '_fetch_public', unavailable)
    cap, cost = budget.budget_plan(cfg)
    assert cap == Decimal('.30') and cost == Decimal('.00864')
    assert 'Stale LLM pricing' in budget.pricing_warning()
    assert pricing.CACHE_PATH.read_bytes() == cached_before


def test_price_decrease_does_not_raise_allowed_spend(config, monkeypatch):
    clock(monkeypatch)
    sources(monkeypatch, price_page('6', '18', '0.6'))
    cfg = configured(config)
    budget.budget_plan(cfg)
    pricing._attempts.clear()
    clock(monkeypatch, '2026-10-12')
    sources(monkeypatch)
    cap, cost = budget.budget_plan(cfg)
    assert cap == Decimal('.30') and cost == Decimal('.00864')
    cache = json.loads(pricing.CACHE_PATH.read_text())
    assert cache['observed_peak_rates'] == ['3', '9', '0.30']
    assert cache['estimate_rates'] == ['6', '18', '0.6']


@pytest.mark.parametrize('changed', [{'model': 'other-model'}, {'tokens_per_unit': 1000},
                                      {'verified_at': '2099-01-01T00:00:00+00:00'},
                                      {'estimate_rates': ['NaN', '9', '0.3']}])
def test_untrusted_cache_cannot_lower_or_refresh_estimates(monkeypatch, changed):
    clock(monkeypatch)
    sources(monkeypatch)
    pricing.review(budget.VERIFIED_PRICING, '2026-10-11')
    cache = json.loads(pricing.CACHE_PATH.read_text())
    cache.update(changed)
    pricing.CACHE_PATH.write_text(json.dumps(cache))
    pricing._attempts.clear()
    def unavailable(*args):
        raise pricing.PricingUnavailable('offline')
    monkeypatch.setattr(pricing, '_fetch_public', unavailable)
    record, status, _ = pricing.review(budget.VERIFIED_PRICING, '2026-10-11')
    assert status == 'unavailable' and record['verified_on'] == '2026-10-01'
    assert record['input_cny_per_million'] == '3'


class Response:
    status_code = 200
    url = pricing.PRICE_URL
    headers = {'Content-Type': 'text/html; charset=utf-8'}
    raw = b'<html>public</html>'
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def iter_content(self, size): yield self.raw


@pytest.mark.parametrize('attribute,value', [('status_code', 503), ('url', 'https://evil.example/'),
                                             ('headers', {'Content-Type': 'application/json'}),
                                             ('raw', b'x' * (pricing.MAX_BYTES + 1))],
                         ids=['http-error', 'redirect', 'wrong-content-type', 'oversize-body'])
def test_transport_rejects_redirects_errors_non_html_and_oversize(attribute, value):
    response = Response()
    setattr(response, attribute, value)
    client = SimpleNamespace(get=lambda *args, **kwargs: response)
    with pytest.raises(pricing.PricingUnavailable):
        transport(client, pricing.PRICE_URL, pricing.monotonic() + 30)


def test_transport_has_no_authentication_retries_or_redirects():
    response = Response()
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response
    assert transport(SimpleNamespace(get=get), pricing.PRICE_URL, pricing.monotonic() + 30) == response.raw
    assert len(calls) == 1 and calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['timeout'][0] <= 3 and calls[0][1]['timeout'][1] <= 8
    assert not set(calls[0][1]['headers']) & {'Authorization', 'Cookie'}
    assert response.closed


def test_expired_deadline_never_opens_connection():
    def forbidden(*args, **kwargs):
        pytest.fail('Expired check attempted a connection')
    with pytest.raises(pricing.PricingUnavailable):
        transport(SimpleNamespace(get=forbidden), pricing.PRICE_URL, pricing.monotonic() - 1)


def test_invalid_contract_never_advances_prior_receipt(monkeypatch):
    clock(monkeypatch)
    sources(monkeypatch)
    pricing.review(budget.VERIFIED_PRICING, '2026-10-11')
    previous = pricing.CACHE_PATH.read_bytes()
    pricing._attempts.clear()
    clock(monkeypatch, '2026-10-12')
    sources(monkeypatch, price_page('6', '18', '0.6'), '<html>OK</html>')
    record, status, _ = pricing.review(budget.VERIFIED_PRICING, '2026-10-12')
    assert status == 'unavailable' and record['verified_on'] == '2026-10-11'
    assert pricing.CACHE_PATH.read_bytes() == previous


def test_bad_cache_shape_is_ignored_without_disclosing_contents(monkeypatch):
    clock(monkeypatch)
    pricing.CACHE_PATH.write_text(json.dumps({'source_sha256': [pricing.PRICE_URL, pricing.CONTRACT_URL],
                                             'estimate_rates': ['0.1', '0.1', '0'],
                                             'private_text': 'do-not-log-cache-body'}))
    record, status, reason = pricing.review(budget.VERIFIED_PRICING, '2026-10-11')
    assert status == 'unavailable' and record['input_cny_per_million'] == '3'
    assert 'do-not-log' not in reason
