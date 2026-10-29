"""Usage/currency accounting uses synthetic responses only; no network or state writes."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest

from zot2dailypaper.api_costs import ApiCosts, PriceSchedule, summary_schedule
from zot2dailypaper.budget import VERIFIED_PRICING
from zot2dailypaper.construct_email import email_plain_text, render_email


ENDPOINT = 'https://api.siliconflow.cn/v1'
MODEL = VERIFIED_PRICING['model']


def response(model=MODEL, **changes):
    usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
             'prompt_tokens_details': {'cached_tokens': 30, 'cache_write_tokens': 10},
             'completion_tokens_details': {'reasoning_tokens': 5}}
    usage.update(changes)
    return {'model': model, 'usage': usage}


def record(costs, result=None, *, schedule=None, endpoint=ENDPOINT, model=MODEL, kind='chat', retry=False):
    with costs.attempt(endpoint, model, kind, schedule=schedule, retry=retry) as event:
        event.observe(response() if result is None else result)
    return event


def test_measured_tokens_use_frozen_guard_prices_and_not_budget_reservation():
    costs = ApiCosts()
    costs.reserved('.30', '2026-10-29', 'Asia/Singapore')
    event = record(costs, schedule=summary_schedule(VERIFIED_PRICING))
    assert event.amount == Decimal('.00048')  # (100*3 + 20*9)/million, reasoning already included.
    html = render_email([], api_costs=costs, pricing_notice='')
    plain = email_plain_text(html)
    assert 'CNY 0.00048000' in plain and 'Budget reservation: CNY 0.30' in plain
    assert 'not estimated expenditure' in plain and 'not provider invoices' in plain
    assert 'cache_read=30' in plain and 'cache_write=10' in plain and 'reasoning=5' in plain
    assert 'siliconflow-peak-v1-' in plain and 'verified 2026-10-01' in plain
    assert 'full input rate' in plain and 'no cache discount applied' in plain


def test_optional_cache_tariffs_and_reasoning_do_not_double_count():
    schedule = PriceSchedule('OpenAI', 'synthetic', 'chat', 'USD', '2', '8', 'test-only-v1',
                             '2026-10-29', '2026-10-29', cache_read_rate='.5', cache_write_rate='2.5')
    costs = ApiCosts()
    event = record(costs, response(model='synthetic', prompt_tokens=1000, completion_tokens=200,
                   total_tokens=1200, prompt_tokens_details={'cached_tokens': 400, 'cache_write_tokens': 100},
                   completion_tokens_details={'reasoning_tokens': 50}), schedule=schedule,
                   endpoint='https://api.openai.com/v1', model='synthetic')
    assert event.amount == Decimal('.00305')


def test_missing_cache_splits_are_explicit_with_conservative_full_input_estimate():
    event = record(ApiCosts(), response(prompt_tokens_details=None, completion_tokens_details=None),
                   schedule=summary_schedule(VERIFIED_PRICING))
    assert event.amount == Decimal('.00048') and event.usage['cache_read'] is None
    assert event.usage['cache_write'] is None and event.usage['reasoning'] is None
    schedule = PriceSchedule('SiliconFlow', MODEL, 'chat', 'CNY', '3', '9', 'test-only-v2',
                             '2026-10-29', '2026-10-29', cache_read_rate='.3')
    unknown = record(ApiCosts(), response(prompt_tokens_details=None), schedule=schedule)
    assert unknown.amount is None and unknown.issue == 'cache usage split unavailable'


@pytest.mark.parametrize('changes', [
    {'prompt_tokens': -1}, {'prompt_tokens': True}, {'completion_tokens': '20'},
    {'total_tokens': 119}, {'total_tokens': False}, {'prompt_tokens': None},
    {'prompt_tokens': 10**13}, {'completion_tokens_details': {'reasoning_tokens': 21}},
    {'completion_tokens_details': {'reasoning_tokens': '5'}},
    {'prompt_tokens_details': {'cached_tokens': -1}},
    {'prompt_tokens_details': {'cached_tokens': 90, 'cache_write_tokens': 20}},
])
def test_invalid_or_partial_usage_is_unknown_never_zero(changes):
    costs = ApiCosts()
    event = record(costs, response(**changes), schedule=summary_schedule(VERIFIED_PRICING))
    assert event.amount is None
    plain = email_plain_text(render_email([], api_costs=costs, pricing_notice=''))
    assert 'INCOMPLETE' in plain and 'unknown cost, never counted as zero' in plain
    assert 'usage invalid or incomplete' in plain


def test_no_tariff_or_mismatching_model_never_uses_another_models_price():
    costs = ApiCosts()
    unpriced = record(costs)
    mismatch = record(costs, response(model='different'), schedule=summary_schedule(VERIFIED_PRICING))
    assert unpriced.amount is mismatch.amount is None
    plain = '\n'.join(costs.lines())
    assert 'price schedule unavailable' in plain and 'response model unverified' in plain
    assert 'Estimated expenditure: unknown' in plain and '2/2 attempts' in plain


def test_failed_attempts_with_usage_are_priced_once_and_timeouts_remain_unknown():
    costs = ApiCosts()
    class Failure(RuntimeError):
        body = response()
    with pytest.raises(Failure):
        with costs.attempt(ENDPOINT, MODEL, 'chat', schedule=summary_schedule(VERIFIED_PRICING)):
            raise Failure('PRIVATE_PROVIDER_TEXT')
    with pytest.raises(TimeoutError):
        with costs.attempt(ENDPOINT, MODEL, 'chat', schedule=summary_schedule(VERIFIED_PRICING), retry=True):
            raise TimeoutError('PRIVATE_ACCOUNT')
    record(costs, schedule=summary_schedule(VERIFIED_PRICING), retry=True)
    assert len(costs.attempts) == 3 and sum(e.amount or Decimal(0) for e in costs.attempts) == Decimal('.00096')
    plain = '\n'.join(costs.lines())
    assert '3 attempts, 2 retries, 2 failed, 1 unknown cost' in plain and 'CNY 0.00096000' in plain
    assert 'PRIVATE' not in plain


def test_currencies_provider_model_types_and_price_versions_stay_separate():
    costs = ApiCosts()
    record(costs, schedule=summary_schedule(VERIFIED_PRICING))
    second_price = dict(VERIFIED_PRICING, input_cny_per_million='6')
    record(costs, schedule=summary_schedule(second_price))
    usd = PriceSchedule('OpenAI', 'synthetic-embedding', 'embedding', 'USD', '.02', 0,
                        'synthetic-v1', '2026-10-29', '2026-10-29')
    record(costs, {'model': 'synthetic-embedding', 'usage': {'prompt_tokens': 100, 'total_tokens': 100}},
           schedule=usd, endpoint='https://api.openai.com/v1', model='synthetic-embedding', kind='embedding')
    plain = '\n'.join(costs.lines())
    assert 'CNY 0.00126000; USD 0.00000200' in plain
    assert 'SiliconFlow /' in plain and 'OpenAI / synthetic-embedding / embedding' in plain
    assert plain.count('Price schedule: siliconflow-peak-v1-') == 2
    assert 'no account balance, taxes or currency conversion' in plain
    assert summary_schedule(VERIFIED_PRICING).version != summary_schedule(second_price).version


def test_no_calls_disabled_llm_replay_and_unavailable_report_have_distinct_messages():
    costs = ApiCosts()
    costs.saved_summaries = 2
    plain = email_plain_text(render_email([], api_costs=costs, pricing_notice='', llm_enabled=False, delivery_only=True))
    assert 'No model API calls: new estimated API expenditure 0' in plain
    assert 'LLM summary generation disabled' in plain and 'Delivery-only replay' in plain
    assert '2 previously generated summaries reused: no new generation cost' in plain
    assert 'prior reservations are preserved' in plain
    unavailable = email_plain_text(render_email([], pricing_notice=''))
    assert 'usage report unavailable; estimated expenditure unknown' in unavailable
    assert 'No model API calls' not in unavailable


def test_zero_usage_is_different_from_missing_usage_and_tiny_cost_is_not_zero():
    costs = ApiCosts()
    zero = record(costs, response(prompt_tokens=0, completion_tokens=0, total_tokens=0,
                  prompt_tokens_details=None, completion_tokens_details=None), schedule=summary_schedule(VERIFIED_PRICING))
    missing = record(costs, {'model': MODEL, 'usage': None}, schedule=summary_schedule(VERIFIED_PRICING))
    assert zero.amount == 0 and missing.amount is None
    assert '1/2 attempts have unknown cost' in '\n'.join(costs.lines())
    tiny = PriceSchedule('SiliconFlow', MODEL, 'chat', 'CNY', '.00001', 0,
                         'synthetic-tiny', '2026-10-29', '2026-10-29')
    small = ApiCosts()
    record(small, schedule=tiny)
    assert 'CNY <0.00000001' in '\n'.join(small.lines())


def test_concurrent_attempts_do_not_duplicate_or_drop_usage():
    costs = ApiCosts()
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda _: record(costs, schedule=summary_schedule(VERIFIED_PRICING)), range(40)))
    assert len(costs.attempts) == 40
    assert 'CNY 0.01920000' in '\n'.join(costs.lines())


def test_credentials_profile_text_and_untrusted_fields_never_enter_report():
    costs = ApiCosts()
    record(costs, {'model': '<script>PRIVATE_PROFILE</script>', 'usage': None, 'secret': 'PRIVATE_RESPONSE'},
           endpoint='https://PRIVATE_USER:PRIVATE_PASSWORD@private.example/v1?key=PRIVATE_KEY',
           model='sk-PRIVATE_KEY')
    html = render_email([], api_costs=costs, pricing_notice='')
    assert 'PRIVATE' not in html and 'private.example' not in html
    assert 'Other provider (endpoint withheld) / unknown' in html
    # Attribute-style SDK objects are also supported without serializing them.
    response_object = SimpleNamespace(model=MODEL, usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120))
    assert record(ApiCosts(), response_object, schedule=summary_schedule(VERIFIED_PRICING)).amount == Decimal('.00048')
