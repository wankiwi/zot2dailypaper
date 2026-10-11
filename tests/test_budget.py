"""Budget safety uses only synthetic prices, fake APIs and local bare Git repos."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import subprocess
from types import SimpleNamespace
import pytest
from zot2dailypaper import budget
from zot2dailypaper.protocol import Paper


def test_production_pricing_is_fail_closed(config,monkeypatch):
    monkeypatch.setattr(budget,"VERIFIED_PRICING",None)
    config.llm.budget.enabled=True
    with pytest.raises(budget.BudgetUnavailable,match='not verified'):
        budget.budget_plan(config.llm)


def test_verified_plan_rejects_unknown_endpoint_model_and_invalid_budget(config,monkeypatch):
    config.llm.budget.enabled=True
    monkeypatch.setattr(budget,'VERIFIED_PRICING',{'host':'test.example','model':'test-model','valid_through':'2099-01-01','input_cny_per_million':'3','output_cny_per_million':'9'})
    config.llm.api.base_url='https://test.example/v1';config.llm.generation_kwargs.model='test-model'
    cap,cost=budget.budget_plan(config.llm)
    assert cap==Decimal('0.30') and cost*50<=Decimal('0.30')
    config.llm.api.base_url='https://unknown.example/v1'
    with pytest.raises(budget.BudgetUnavailable,match='Endpoint'):budget.budget_plan(config.llm)
    config.llm.budget.daily_cny=0
    with pytest.raises(budget.BudgetUnavailable,match='positive'):budget.budget_plan(config.llm)


@pytest.mark.parametrize('amount', ['0.05', '0.21', '0.30', '0.40'])
def test_budget_amount_uses_config_without_a_fixed_ceiling(config, amount):
    config.llm.api.base_url = 'https://api.siliconflow.cn/v1'
    config.llm.generation_kwargs.model = 'deepseek-ai/DeepSeek-V4-Flash'
    config.llm.budget.daily_cny = amount
    cap, cost = budget.budget_plan(config.llm)
    assert cap == Decimal(amount) and cost == Decimal('.00432')


@pytest.mark.parametrize('amount', [None, 0, -.1, 'NaN', 'Infinity', True, 'invalid'])
def test_budget_amount_rejects_invalid_values_without_substitution(config, amount):
    config.llm.budget.daily_cny = amount
    with pytest.raises(budget.BudgetUnavailable, match='positive finite'):
        budget.budget_plan(config.llm)


def test_budget_amount_missing_fails_closed_without_default(config):
    del config.llm.budget.daily_cny
    with pytest.raises(budget.BudgetUnavailable, match='configured'):
        budget.budget_plan(config.llm)


def test_concurrent_calls_timeouts_and_day_rollover(monkeypatch):
    monkeypatch.setattr(budget,'utc_day',lambda:'2026-10-01')
    guard=budget.BudgetRequests(Decimal('.20'),Decimal('.004608'),'2026-10-01')
    calls=[]
    def attempt(i):
        def operation():
            calls.append(i)
            raise TimeoutError('ambiguous billing')
        try:guard.call(operation)
        except (TimeoutError,budget.BudgetUnavailable):pass
    with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(attempt,range(60)))
    assert len(calls)==43 and guard.remaining>=0
    before=guard.remaining
    monkeypatch.setattr(budget,'utc_day',lambda:'2026-10-02')
    with pytest.raises(budget.BudgetUnavailable,match='day changed'):guard.call(lambda:None)
    assert guard.remaining==before


def make_git(tmp_path, monkeypatch, initialize_ledger=True):
    remote=tmp_path/'origin.git';checkout=tmp_path/'checkout'
    def git(*args,cwd=None):return subprocess.run(['git',*args],cwd=cwd,check=True,capture_output=True).stdout
    git('init','--bare',str(remote));git('init','-b','main',str(checkout))
    git('config','user.name','Test',cwd=checkout);git('config','user.email','test@example.org',cwd=checkout)
    if initialize_ledger:
        (checkout/'llm_budget.json').write_text('{"version":1,"days":{}}')
    (checkout/'recommendations.json').write_text('{"version":1,"records":{"sent":"preserved"}}')
    git('add','recommendations.json',cwd=checkout)
    if initialize_ledger:git('add','llm_budget.json',cwd=checkout)
    git('commit','-m','Initial state',cwd=checkout)
    git('remote','add','origin',str(remote),cwd=checkout);git('push','origin','HEAD:paper-state',cwd=checkout)
    monkeypatch.chdir(checkout)
    return git,checkout,remote


def test_durable_claim_survives_crash_and_keeps_history(tmp_path,monkeypatch):
    git,checkout,remote=make_git(tmp_path,monkeypatch)
    index=git('write-tree')
    assert budget.reserve_day(Decimal('.20'),'2026-10-01')=='2026-10-01'
    # No post-call save or refund: simulate crash immediately after reservation.
    with pytest.raises(budget.BudgetUnavailable,match='already reserved'):
        budget.reserve_day(Decimal('.20'),'2026-10-01')
    head=git('rev-parse','refs/heads/paper-state',cwd=remote).decode().strip()
    assert b'preserved' in git('show',head+':recommendations.json',cwd=remote)
    assert git('write-tree')==index
    ledger=json.loads(git('show',head+':llm_budget.json',cwd=remote))
    assert ledger['days']['2026-10-01']['reserved_cny']=='0.20'


def test_budget_increase_keeps_existing_claim_and_history(tmp_path, monkeypatch):
    git, checkout, remote = make_git(tmp_path, monkeypatch)
    budget.reserve_day(Decimal('.20'), '2026-10-04')
    original_head = git('rev-parse', 'refs/heads/paper-state', cwd=remote).decode().strip()
    original_history = git('show', original_head + ':recommendations.json', cwd=remote)
    original_ledger = json.loads(git('show', original_head + ':llm_budget.json', cwd=remote))
    with pytest.raises(budget.BudgetUnavailable, match='already reserved'):
        budget.reserve_day(Decimal('.30'), '2026-10-04')
    assert git('rev-parse', 'refs/heads/paper-state', cwd=remote).decode().strip() == original_head
    budget.reserve_day(Decimal('.30'), '2026-10-05')
    head = git('rev-parse', 'refs/heads/paper-state', cwd=remote).decode().strip()
    ledger = json.loads(git('show', head + ':llm_budget.json', cwd=remote))
    assert ledger['days']['2026-10-04'] == original_ledger['days']['2026-10-04']
    assert ledger['days']['2026-10-05']['reserved_cny'] == '0.30'
    assert ledger['days']['2026-10-05']['policy'] == 'whole-day-no-refund'
    assert git('show', head + ':recommendations.json', cwd=remote) == original_history


def test_concurrent_repository_claims_only_one_wins(tmp_path,monkeypatch):
    git,checkout,remote=make_git(tmp_path,monkeypatch)
    # Two independent processes, like workflow reruns, share only the remote ref.
    second=tmp_path/'second';git('clone',str(remote),str(second))
    code="from zot2dailypaper.budget import reserve_day; from decimal import Decimal; reserve_day(Decimal('.20'),'2026-10-01')"
    import sys
    jobs=[subprocess.Popen([sys.executable,'-c',code],cwd=path,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for path in (checkout,second)]
    statuses=[]
    for job in jobs:job.communicate();statuses.append(job.returncode)
    assert statuses.count(0)==1


def test_failed_push_cannot_authorize(tmp_path,monkeypatch):
    git,checkout,remote=make_git(tmp_path,monkeypatch)
    hook=remote/'hooks/pre-receive';hook.write_text('#!/bin/sh\nexit 1\n');hook.chmod(0o755)
    with pytest.raises(budget.BudgetUnavailable,match='Could not persist'):
        budget.reserve_day(Decimal('.20'),'2026-10-01')


def test_corrupt_ledger_refuses_claim(tmp_path,monkeypatch):
    git,checkout,remote=make_git(tmp_path,monkeypatch)
    (checkout/'llm_budget.json').write_text('{"version":999}')
    git('add','llm_budget.json');git('commit','-m','Corrupt fixture');git('push','origin','HEAD:paper-state')
    with pytest.raises(budget.BudgetUnavailable,match='Invalid budget ledger'):
        budget.reserve_day(Decimal('.20'),'2026-10-01')


def test_unicode_prompt_bound_and_reasoning_stops_calls(config,monkeypatch):
    config.llm.budget.enabled=True
    guard=budget.BudgetRequests(Decimal('.20'),Decimal('.005'),budget.utc_day())
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='中文摘要。',reasoning_content='Unexpected'))])
    client=SimpleNamespace(max_retries=0,chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    p=Paper('journals','长标题'*1000,[],'分子材料摘要'*1000,'https://example.org/paper')
    p.generate_tldr(client,config.llm,guard)
    assert len(calls)==1 and p.tldr_status=='fallback' and guard.remaining==0
    request=calls[0]
    assert len(request['messages'][1]['content'].encode())<=budget.PROMPT_BYTES
    assert len(request['messages'][0]['content'].encode())<=budget.SYSTEM_BYTES
    assert 'Abstract:' in request['messages'][1]['content']
    assert request['n']==1 and request['max_tokens']==96 and request['extra_body']=={'enable_thinking':False}
    p.generate_tldr(client,config.llm,guard)
    assert len(calls)==1


def test_disabled_budget_does_not_mean_unlimited(config):
    config.llm.budget.enabled=False
    with pytest.raises(budget.BudgetUnavailable,match='paid calls prohibited'):
        budget.budget_plan(config.llm)


def test_executor_fail_closed_retains_abstract_without_client(config,monkeypatch,tmp_path):
    from zot2dailypaper.executor import Executor
    from zot2dailypaper.state import State
    from tests.canned_responses import make_stub_zotero_client,make_sample_paper
    from zot2dailypaper.reranker.api import ApiReranker
    import numpy as np
    monkeypatch.setattr('zot2dailypaper.executor.prepare_budget',budget.prepare_budget)
    monkeypatch.setattr('zot2dailypaper.executor.budget_plan',budget.budget_plan)
    monkeypatch.setattr('zot2dailypaper.executor.OpenAI',lambda **kw:pytest.fail('Blocked budget created client'))
    monkeypatch.setattr('zot2dailypaper.executor.zotero.Zotero',lambda *a,**kw:make_stub_zotero_client())
    monkeypatch.setattr(ApiReranker,'get_similarity_score',lambda self,a,b:np.ones((len(a),len(b))))
    config.llm.budget.enabled=True
    executor=Executor(config)
    p=make_sample_paper()
    monkeypatch.setattr(executor.retrievers['arxiv'],'retrieve_papers',lambda:[p])
    selected=executor._recommend(State(tmp_path/'state.json'),[],10,1)
    assert selected and selected[0].tldr==p.abstract
    assert selected[0].tldr_error=='budget_unavailable'

@pytest.mark.parametrize('problem',['model','missing_usage','invalid_usage','reasoning_tokens','reasoning_content'])
def test_response_violation_aborts_queued_calls(problem):
    from threading import Barrier
    from tests.canned_responses import make_chat_response
    barrier=Barrier(6);calls=[]
    guard=budget.BudgetRequests(Decimal('.20'),Decimal('.001'),budget.utc_day())
    def create(**kwargs):
        calls.append(kwargs)
        response=make_chat_response('中文摘要。',kwargs['model'])
        if problem=='model':response.model='wrong'
        elif problem=='missing_usage':response.usage=None
        elif problem=='invalid_usage':response.usage.completion_tokens=1000
        elif problem=='reasoning_tokens':response.usage.completion_tokens_details.reasoning_tokens=10
        else:response.choices[0].message.reasoning_content='Unexpected'
        return response
    client=SimpleNamespace(max_retries=0,chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    def worker(i):
        barrier.wait()
        p=Paper('journals',str(i),[],'Scientific abstract','https://example.org')
        p.generate_tldr(client,{'generation_kwargs':{'model':'test'}},guard)
        return p
    with ThreadPoolExecutor(max_workers=6) as pool:papers=list(pool.map(worker,range(6)))
    assert len(calls)==1 and guard.remaining==0
    assert all(p.tldr_error=='budget_unavailable' for p in papers)

def test_missing_ledger_never_bootstraps_during_claim(tmp_path,monkeypatch):
    git,checkout,remote=make_git(tmp_path,monkeypatch)
    # Remove from the test index while keeping the file, without filesystem deletion.
    git('update-index','--force-remove','llm_budget.json');git('commit','-m','Simulate missing ledger');git('push','origin','HEAD:paper-state')
    before=git('rev-parse','refs/heads/paper-state',cwd=remote)
    with pytest.raises(budget.BudgetUnavailable,match='explicit audited bootstrap'):
        budget.reserve_day(Decimal('.20'))
    assert git('rev-parse','refs/heads/paper-state',cwd=remote)==before

def test_endpoint_port_must_match_verified_origin(config,monkeypatch):
    monkeypatch.setattr(budget,'VERIFIED_PRICING',{'host':'test.example','model':'test-model','valid_through':'2099-01-01','input_cny_per_million':'3','output_cny_per_million':'9'})
    config.llm.api.base_url='https://test.example:444/v1';config.llm.generation_kwargs.model='test-model'
    with pytest.raises(budget.BudgetUnavailable,match='Endpoint'):budget.budget_plan(config.llm)


def test_explicit_bootstrap_is_separate_and_preserves_state(tmp_path,monkeypatch):
    git,checkout,remote=make_git(tmp_path,monkeypatch,initialize_ledger=False)
    with pytest.raises(budget.BudgetUnavailable,match='explicit audited bootstrap'):budget.reserve_day(Decimal('.20'))
    budget.bootstrap_ledger()
    head=git('rev-parse','refs/heads/paper-state',cwd=remote).decode().strip()
    assert b'preserved' in git('show',head+':recommendations.json',cwd=remote)
    with pytest.raises(budget.BudgetUnavailable,match='already exists'):budget.bootstrap_ledger()
    assert budget.reserve_day(Decimal('.20'))==budget.utc_day()


def test_deleted_historical_ledger_cannot_be_reinitialized(tmp_path,monkeypatch):
    git,checkout,remote=make_git(tmp_path,monkeypatch)
    git('update-index','--force-remove','llm_budget.json');git('commit','-m','Missing ledger fixture');git('push','origin','HEAD:paper-state')
    with pytest.raises(budget.BudgetUnavailable,match='existed in history'):budget.bootstrap_ledger()


def test_ledger_read_error_is_not_an_empty_balance(tmp_path,monkeypatch):
    git,checkout,remote=make_git(tmp_path,monkeypatch)
    original=budget.git
    def broken(*args,**kwargs):
        if args[0]=='show':raise budget.BudgetUnavailable('simulated read failure')
        return original(*args,**kwargs)
    monkeypatch.setattr(budget,'git',broken)
    with pytest.raises(budget.BudgetUnavailable,match='read failure'):budget.reserve_day(Decimal('.20'))


def test_reviewed_deepseek_peak_price_fits_45(config,monkeypatch):
    monkeypatch.setattr(budget,'utc_day',lambda:'2026-10-01')
    config.llm.api.base_url='https://api.siliconflow.cn/v1'
    config.llm.generation_kwargs.model='deepseek-ai/DeepSeek-V4-Flash'
    cap,cost=budget.budget_plan(config.llm)
    assert cost==Decimal('0.00432') and cost*45==Decimal('0.19440') and cost*45<=cap

@pytest.mark.parametrize('status',[429,500])
def test_direct_sdk_default_retries_are_disabled_at_paid_boundary(status):
    import httpx
    from openai import OpenAI
    from tests.canned_responses import make_sample_paper,make_budget_guard
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,json={'error':{'message':'Synthetic provider error','type':'server_error'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with OpenAI(api_key='fake-test-key',base_url='https://example.org/v1',http_client=transport) as client:
            assert client.max_retries==2
            guard=make_budget_guard()
            paper=make_sample_paper()
            paper.generate_tldr(client,{'generation_kwargs':{'model':'test-model'}},guard)
            assert len(calls)==1 and client.max_retries==2
            assert guard.remaining==Decimal('.199')
            assert paper.tldr_status=='fallback' and paper.tldr_error=='request_failed'


def test_unknown_retry_behavior_fails_before_dispatch():
    from tests.canned_responses import make_sample_paper,make_budget_guard
    calls=[]
    client=SimpleNamespace(max_retries=0,chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:calls.append(kw))))
    del client.max_retries
    paper=make_sample_paper()
    paper.generate_tldr(client,{'generation_kwargs':{'model':'test-model'}},make_budget_guard())
    assert not calls and paper.tldr_error=='budget_unavailable'


def test_bootstrap_rejects_shallow_history(tmp_path,monkeypatch):
    git,checkout,remote=make_git(tmp_path,monkeypatch,initialize_ledger=False)
    shallow=tmp_path/'shallow'
    git('clone','--depth','1','--branch','paper-state',remote.as_uri(),str(shallow))
    monkeypatch.chdir(shallow)
    assert git('rev-parse','--is-shallow-repository').strip()==b'true'
    with pytest.raises(budget.BudgetUnavailable,match='complete history'):
        budget.bootstrap_ledger()


def test_stale_pricing_warns_continues_and_renders_email(config,monkeypatch):
    from zot2dailypaper.construct_email import render_email,email_plain_text
    monkeypatch.setattr(budget,'utc_day',lambda:'2026-10-09')
    config.llm.api.base_url='https://api.siliconflow.cn/v1'
    config.llm.generation_kwargs.model='deepseek-ai/DeepSeek-V4-Flash'
    warnings=[]
    monkeypatch.setattr(budget.logger,'warning',warnings.append)
    cap,cost=budget.budget_plan(config.llm)
    assert cap==Decimal('.30') and cost==Decimal('.00432') and warnings
    html=render_email([])
    assert 'Stale LLM pricing' in html and 'Stale LLM pricing' in email_plain_text(html)
    assert '实际费用可能超过' in html
    monkeypatch.setattr(budget,'utc_day',lambda:'2026-10-08')
    # The reviewed seed is fresh on this date, but the automatic check failed.
    assert 'Stale LLM pricing' not in budget.pricing_warning()
    assert 'Automatic pricing verification unavailable' in budget.pricing_warning()


@pytest.mark.parametrize('daily_cny,expected', [(.20, 23), (.30, 34)])
def test_known_price_increase_reduces_allowed_calls(config,monkeypatch,daily_cny,expected):
    pricing=dict(budget.VERIFIED_PRICING,input_cny_per_million='6',output_cny_per_million='18')
    monkeypatch.setattr(budget,'VERIFIED_PRICING',pricing)
    config.llm.api.base_url='https://api.siliconflow.cn/v1'
    config.llm.generation_kwargs.model='deepseek-ai/DeepSeek-V4-Flash'
    config.llm.budget.daily_cny=daily_cny
    cap,cost=budget.budget_plan(config.llm)
    assert int(cap//cost)==expected
