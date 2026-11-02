"""Official summary migration: fake providers/public quotes and local bare Git only."""
from datetime import datetime,timezone
from decimal import Decimal
from types import SimpleNamespace
import copy
import json
import pytest
from zot2dailypaper import budget,openai_pricing as prices
from zot2dailypaper.api_costs import ApiCosts,summary_schedule
from tests.canned_responses import make_chat_response,make_sample_paper
from tests.test_budget import make_git

PRICE='''Prices per 1M tokens.
| Model | Input | Output | Cached input | 30m write | 12h write |
| --- | --- | --- | --- | --- | --- |
| gpt-6-luna | $0.10 | $0.50 | $0.01 | $0.125 | $0.25 |

| Model | Input | Output | Cached input | 30m write | 12h write |
| --- | --- | --- | --- | --- | --- |
| gpt-6-luna | $0.20 | $1.00 | $0.02 | $0.25 | $0.50 |
'''
FX='''<Envelope xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"><Cube><Cube time="2026-10-30"><Cube currency="USD" rate="1.1172"/><Cube currency="CNY" rate="7.4326"/></Cube></Cube></Envelope>'''
DAY='2026-11-02'
ENDPOINT='https://api.openai.com/v1'

@pytest.fixture(autouse=True)
def public_quotes_offline(monkeypatch):
    prices._quotes.clear()
    def unavailable(url):raise prices.QuoteUnavailable('offline_fixture')
    monkeypatch.setattr(prices,'_fetch',unavailable)
    monkeypatch.setattr(budget,'utc_day',lambda:DAY)
    monkeypatch.setattr(budget,'budget_now',lambda:datetime(2026,11,2,10,tzinfo=timezone.utc))


def official(config):
    cfg=copy.deepcopy(config.llm)
    cfg.enabled=True;cfg.api.base_url=ENDPOINT;cfg.generation_kwargs.model=prices.MODEL
    cfg.budget.enabled=True;cfg.budget.daily_cny=.30;cfg.budget.timezone='Asia/Singapore'
    return cfg


def guard(cfg):
    cap,per,quote=budget.budget_plan(cfg,with_pricing=True)
    return budget.BudgetRequests(cap,per,DAY,expected_model=prices.MODEL,
                                expected_endpoint=ENDPOINT,price_schedule=summary_schedule(quote))


def client(create):
    return SimpleNamespace(max_retries=0,base_url=ENDPOINT,
                           chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_exact_official_prices_fx_and_conservative_conversion():
    assert prices.parse_prices(PRICE)=={'input_usd_per_million':'0.50','output_usd_per_million':'1.00'}
    assert prices.parse_fx(FX)['usd_per_eur']=='1.1172'
    record=prices.quote(DAY)
    assert record['input_cny_per_million']=='3.700'
    assert record['output_cny_per_million']=='7.400'
    assert '7.4' in record['conversion_note'] and '2026-10-30' in record['conversion_note']
    assert record['valid_through']=='2026-11-06'


def test_live_quote_maxima_and_fresh_fx_refresh(monkeypatch):
    monkeypatch.setattr(prices,'_fetch',lambda url:PRICE if url==prices.PRICE_URL else FX.replace('2026-10-30','2026-11-02'))
    record=prices.quote(DAY)
    assert record['public_refresh']=='price,fx'
    assert record['valid_through']=='2026-11-09'
    assert record['fx_asof']==DAY


@pytest.mark.parametrize('day',['2026-10-29','2026-11-10'])
def test_future_or_expired_seed_fails_closed(day):
    with pytest.raises(prices.QuoteUnavailable):prices.quote(day)


@pytest.mark.parametrize('bad',[PRICE.replace('1M tokens','1K tokens'),PRICE.replace('$0.10','¥0.10'),PRICE.replace('gpt-6-luna','another-model'),PRICE.replace('12h write','unknown')])
def test_reachable_changed_public_source_never_silently_uses_old_seed(monkeypatch,bad):
    monkeypatch.setattr(prices,'_fetch',lambda url:bad if url==prices.PRICE_URL else FX)
    with pytest.raises(prices.QuoteUnavailable):prices.quote(DAY)


@pytest.mark.parametrize('bad',[FX.replace('2026-10-30','2026-11-03'),FX.replace('2026-10-30','2026-10-01'),FX.replace('currency="USD"','currency="EUR"'),FX.replace('1.1172','NaN')])
def test_bad_reachable_fx_fails_closed(monkeypatch,bad):
    monkeypatch.setattr(prices,'_fetch',lambda url:PRICE if url==prices.PRICE_URL else bad)
    with pytest.raises(prices.QuoteUnavailable):prices.quote(DAY)


def test_public_quote_unavailability_converted_to_budget_block(config,monkeypatch):
    cfg=official(config)
    monkeypatch.setattr(prices,'quote',lambda *x:(_ for _ in ()).throw(prices.QuoteUnavailable('expired')))
    with pytest.raises(budget.BudgetUnavailable,match='price/FX') as error:budget.budget_plan(cfg)
    assert error.value.reason=='billing_unverified'


@pytest.mark.parametrize('endpoint',['http://api.openai.com/v1','https://api.openai.com.evil.test/v1','https://api.openai.com/v1?x=1','https://user@api.openai.com/v1','https://api.openai.com:444/v1','https://api.openai.com/v2'])
def test_origin_not_just_url_substring(config,endpoint):
    cfg=official(config);cfg.api.base_url=endpoint
    with pytest.raises(budget.BudgetUnavailable):budget.budget_plan(cfg)


def test_model_budget_token_bound_and_no_guard_disable(config):
    cfg=official(config);cap,per=budget.budget_plan(cfg)
    assert cap==Decimal('.30') and per==Decimal('.0049728')
    assert per*50<cap and per*61>cap
    cfg.budget.enabled=False
    with pytest.raises(budget.BudgetUnavailable):budget.budget_plan(cfg)
    cfg.budget.enabled=True;cfg.generation_kwargs.model='gpt-4o-mini'
    with pytest.raises(budget.BudgetUnavailable,match='Model'):budget.budget_plan(cfg)


def test_official_client_body_and_usage_report(config):
    cfg=official(config);cfg.generation_kwargs.max_tokens=99999
    calls=[]
    def create(**kwargs):calls.append(kwargs);return make_chat_response('该研究发现了可验证的分子输运机制。',prices.MODEL)
    g=guard(cfg);paper=make_sample_paper()
    assert paper.generate_tldr(client(create),cfg,g).startswith('该研究')
    assert paper.has_ai_summary and len(calls)==1
    call=calls[0]
    assert call['max_completion_tokens']==96 and call['reasoning_effort']=='none'
    assert call['service_tier']=='default' and call['store'] is False and call['n']==1
    assert not set(call)&{'extra_body','max_tokens','tools','functions'}
    assert len(call['messages'][1]['content'].encode())<=budget.PROMPT_BYTES
    event=g.api_costs.attempts[0]
    assert event.provider=='OpenAI' and event.usage['input']==64 and event.amount is not None
    report='\n'.join(g.api_costs.lines())
    assert '保守折算' in report and 'CNY/USD reservation rate=7.4' in report
    assert 'no account balance, taxes or currency conversion' not in report
    assert 'SiliconFlow' not in report and budget.pricing_warning(cfg)==''


@pytest.mark.parametrize('failure',['no_usage','wrong_model','unexpected_tier','reasoning'])
def test_invalid_response_stops_guard_without_refunding(config,failure):
    cfg=official(config);g=guard(cfg);calls=[]
    def create(**kwargs):
        calls.append(kwargs);r=make_chat_response('该研究发现了分子机制。',prices.MODEL)
        if failure=='no_usage':r.usage=None
        if failure=='wrong_model':r.model='another-model'
        if failure=='unexpected_tier':r.service_tier='priority'
        if failure=='reasoning':r.usage.completion_tokens_details.reasoning_tokens=1
        return r
    paper=make_sample_paper();original=paper.abstract
    assert paper.generate_tldr(client(create),cfg,g)==original
    assert not paper.has_ai_summary and paper.tldr_error_reason=='billing_unverified'
    assert g.remaining==0 and len(calls)==1
    second=make_sample_paper();second.generate_tldr(client(create),cfg,g)
    assert len(calls)==1
    if failure=='no_usage':assert g.api_costs.attempts[0].amount is None


def test_truncation_and_timeout_retries_keep_original_and_charge_slots(config,monkeypatch):
    cfg=official(config);g=guard(cfg);paper=make_sample_paper()
    def truncated(**kwargs):
        r=make_chat_response('该研究尚未完成',prices.MODEL);r.choices[0].finish_reason='length';return r
    paper.generate_tldr(client(truncated),cfg,g)
    assert paper.tldr==paper.abstract and not paper.has_ai_summary
    assert g.remaining==g.cap-g.per_call
    assert g.api_costs.attempts[0].amount is not None
    calls=[];g=guard(cfg);cfg.request.max_attempts=2
    def timeout(**kwargs):calls.append(kwargs);raise TimeoutError('synthetic ambiguous billing')
    monkeypatch.setattr('zot2dailypaper.protocol.sleep',lambda *x:None)
    make_sample_paper().generate_tldr(client(timeout),cfg,g)
    assert len(calls)==2 and g.remaining==g.cap-2*g.per_call
    assert all(event.amount is None for event in g.api_costs.attempts)


def test_original_daily_ledger_is_shared_and_crash_claim_preserved(config,tmp_path,monkeypatch):
    git,checkout,remote=make_git(tmp_path,monkeypatch)
    cfg=official(config)
    # This is the same local-day ledger used before migration; no new allowance
    # namespace, bootstrap, refund, or provider-specific second budget exists.
    g=budget.prepare_budget(cfg)
    assert g.timezone_name=='Asia/Singapore' and g.cap==Decimal('.30')
    head=git('rev-parse','refs/heads/paper-state',cwd=remote).decode().strip()
    ledger=json.loads(git('show',head+':llm_budget.json',cwd=remote))
    assert len(ledger['claims'])==1
    assert next(iter(ledger['claims'].values()))['reserved_cny']=='0.3'
    with pytest.raises(budget.BudgetUnavailable,match='already reserved'):budget.prepare_budget(cfg)
    legacy=copy.deepcopy(cfg);legacy.api.base_url='https://api.siliconflow.cn/v1';legacy.generation_kwargs.model='deepseek-ai/DeepSeek-V4-Flash'
    with pytest.raises(budget.BudgetUnavailable,match='already reserved'):budget.prepare_budget(legacy)
    assert git('rev-parse','refs/heads/paper-state',cwd=remote).decode().strip()==head
    assert b'preserved' in git('show',head+':recommendations.json',cwd=remote)


def test_quote_expiry_inside_still_valid_singapore_budget_window_stops_calls(config,monkeypatch):
    from dataclasses import replace
    cfg=official(config);record=prices.quote(DAY)
    schedule=replace(summary_schedule(record),valid_through='2026-11-06')
    window=SimpleNamespace(start=datetime(2026,11,6,16,tzinfo=timezone.utc),
                           end=datetime(2026,11,7,16,tzinfo=timezone.utc),
                           day='2026-11-07',timezone_name='Asia/Singapore')
    g=budget.BudgetRequests('.30','.0049728',window.day,window=window,price_schedule=schedule)
    monkeypatch.setattr(budget,'utc_day',lambda:'2026-11-07')
    monkeypatch.setattr(budget,'budget_now',lambda:datetime(2026,11,7,0,1,tzinfo=timezone.utc))
    calls=[]
    with pytest.raises(budget.BudgetUnavailable,match='quote expired'):
        g.call(lambda:calls.append(1))
    assert not calls and g.remaining==Decimal('.30')


def test_installed_official_sdk_serializes_request_without_network(config):
    import httpx
    from openai import OpenAI
    cfg=official(config);g=guard(cfg);seen=[]
    def local_transport(request):
        seen.append(request)
        assert str(request.url)=='https://api.openai.com/v1/chat/completions'
        body=json.loads(request.content)
        assert body['reasoning_effort']=='none' and body['max_completion_tokens']==96
        assert body['store'] is False and body['service_tier']=='default'
        assert 'enable_thinking' not in body and 'extra_body' not in body
        return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':1,
            'model':prices.MODEL,'service_tier':'default',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'该研究提出了可验证的计算方法。'}}],
            'usage':{'prompt_tokens':64,'completion_tokens':16,'total_tokens':80,
                     'completion_tokens_details':{'reasoning_tokens':0},'prompt_tokens_details':{'cached_tokens':0}}})
    with httpx.Client(transport=httpx.MockTransport(local_transport)) as http:
        api=OpenAI(api_key='synthetic-never-a-real-key',base_url=ENDPOINT,max_retries=0,http_client=http)
        paper=make_sample_paper();paper.generate_tldr(api,cfg,g)
        assert paper.has_ai_summary and len(seen)==1
        assert g.api_costs.attempts[0].amount is not None
