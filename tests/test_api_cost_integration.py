"""Real SDK parsing + local pipeline/state; all HTTP and SMTP are synthetic."""
from decimal import Decimal
from contextlib import contextmanager
import json

import httpx
import numpy as np
from omegaconf import open_dict
from openai import OpenAI, InternalServerError, APITimeoutError
import pytest

from tests.canned_responses import make_sample_paper
from tests.test_pipeline import pipeline  # noqa: F401 -- shared fixture
from zot2dailypaper import budget
from zot2dailypaper.api_costs import summary_schedule
from zot2dailypaper.budget_calendar import window_at
from zot2dailypaper.construct_email import email_plain_text
from zot2dailypaper.executor import Executor
from zot2dailypaper.reranker.api import ApiReranker
from zot2dailypaper.state import State


ENDPOINT = 'https://api.siliconflow.cn/v1'
MODEL = budget.VERIFIED_PRICING['model']
API_SIMILARITY = ApiReranker.get_similarity_score


def body(**changes):
    result = {'id': 'synthetic', 'model': MODEL,
              'choices': [{'index': 0, 'finish_reason': 'stop',
                           'message': {'role': 'assistant', 'content': '该合成结果仅用于测试计费报告。'}}],
              'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
                        'completion_tokens_details': {'reasoning_tokens': 0}}}
    result.update(changes)
    return result


def guard():
    return budget.BudgetRequests('.30', '.00432', budget.utc_day(), expected_model=MODEL,
                                 expected_endpoint=ENDPOINT, price_schedule=summary_schedule(budget.VERIFIED_PRICING))


def chat_config(config):
    config.llm.enabled = True
    config.llm.api.base_url = ENDPOINT
    config.llm.generation_kwargs.model = MODEL
    config.llm.request.max_attempts = 2
    return config.llm


def sdk(handler, endpoint=ENDPOINT):
    return OpenAI(api_key='synthetic-test-key', base_url=endpoint, max_retries=0,
                  http_client=httpx.Client(transport=httpx.MockTransport(handler)))


@contextmanager
def embedding_sdk(monkeypatch, handler):
    clients = []
    def http_client(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
    def create(**kwargs):
        assert 'max_retries' not in kwargs  # Preserve the pre-feature SDK policy.
        client = OpenAI(**kwargs)
        assert client.max_retries == 2
        clients.append(client)
        return client
    monkeypatch.setattr('zot2dailypaper.reranker.api.DefaultHttpxClient', http_client)
    monkeypatch.setattr('zot2dailypaper.reranker.api.OpenAI', create)
    try:
        yield clients
    finally:
        for client in clients:
            client.close()


def test_sdk_retry_counts_each_dispatch_once_and_timeout_is_not_zero(config, monkeypatch):
    monkeypatch.setattr('zot2dailypaper.protocol.sleep', lambda _: None)
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            raise httpx.ReadTimeout('PRIVATE_PROVIDER_TEXT', request=request)
        return httpx.Response(200, json=body())
    request_guard, paper = guard(), make_sample_paper()
    with sdk(handler) as client:
        paper.generate_tldr(client, chat_config(config), request_guard)
    assert paper.has_ai_summary and paper.tldr_attempts == len(calls) == 2
    assert request_guard.remaining == Decimal('.30') - 2 * Decimal('.00432')
    costs = request_guard.api_costs
    assert len(costs.attempts) == 2 and costs.attempts[0].amount is None
    assert costs.attempts[1].amount == Decimal('.00048')
    plain = '\n'.join(costs.lines())
    assert '2 attempts, 1 retries, 1 failed, 1 unknown cost' in plain
    assert 'INCOMPLETE' in plain and 'PRIVATE' not in plain


def test_invalid_summary_still_reports_measured_spend(config):
    result = body()
    result['choices'][0]['finish_reason'] = 'length'
    request_guard, paper = guard(), make_sample_paper()
    with sdk(lambda _: httpx.Response(200, json=result)) as client:
        paper.generate_tldr(client, chat_config(config), request_guard)
    assert not paper.has_ai_summary and paper.tldr_error_reason == 'output_truncated'
    assert request_guard.api_costs.attempts[0].amount == Decimal('.00048')
    assert request_guard.api_costs.attempts[0].failed


def test_failing_http_response_usage_is_measured_even_when_sdk_extracts_nested_error(config):
    result = body()
    result['error'] = {'message': 'PRIVATE_PROVIDER_BODY'}
    request_guard, paper = guard(), make_sample_paper()
    config.llm.request.max_attempts = 1
    cfg = chat_config(config)
    cfg.request.max_attempts = 1
    with sdk(lambda _: httpx.Response(503, json=result)) as client:
        paper.generate_tldr(client, cfg, request_guard)
    event = request_guard.api_costs.attempts[0]
    assert event.failed and event.amount == Decimal('.00048')
    assert 'PRIVATE' not in '\n'.join(request_guard.api_costs.lines())


@pytest.mark.parametrize('changes', [{'usage': None}, {'model': 'unexpected-model'},
                                     {'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 9}}])
def test_unknown_billing_stops_guard_without_claiming_zero(config, changes):
    request_guard, paper, calls = guard(), make_sample_paper(), []
    def handler(request):
        calls.append(True)
        return httpx.Response(200, json=body(**changes))
    with sdk(handler) as client:
        paper.generate_tldr(client, chat_config(config), request_guard)
        make_sample_paper().generate_tldr(client, config.llm, request_guard)
    assert len(calls) == 1 and paper.tldr_error_reason == 'billing_unverified'
    assert request_guard.remaining == 0 and len(request_guard.api_costs.attempts) == 1
    assert request_guard.api_costs.attempts[0].amount is None
    assert 'unknown cost' in '\n'.join(request_guard.api_costs.lines())


def test_blocked_guard_no_input_and_disabled_llm_do_not_create_phantom_calls(config):
    request_guard = guard()
    request_guard.remaining = Decimal(0)
    with sdk(lambda _: pytest.fail('A blocked guard must not dispatch')) as client:
        make_sample_paper().generate_tldr(client, chat_config(config), request_guard)
        make_sample_paper(abstract='').generate_tldr(client, config.llm, request_guard)
        config.llm.enabled = False
        make_sample_paper().generate_tldr(client, config.llm, request_guard)
    assert request_guard.api_costs.attempts == []
    assert 'No model API calls' in '\n'.join(request_guard.api_costs.lines())


def test_embedding_calls_cache_hits_and_missing_tariff_are_reported(config, monkeypatch):
    config.reranker.api.base_url = 'https://api.openai.com/v1'
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json={'object': 'list', 'model': payload['model'],
            'data': [{'object': 'embedding', 'embedding': [1, 0, 0], 'index': i}
                     for i in range(len(payload['input']))],
            'usage': {'prompt_tokens': 10, 'total_tokens': 10}})
    with embedding_sdk(monkeypatch, handler):
        reranker = ApiReranker(config)
        first = reranker.get_similarity_score(['first'], ['second'])
        repeated = reranker.get_similarity_score(['first'], ['second'])
    np.testing.assert_equal(first, repeated)
    assert len(calls) == len(reranker.api_costs.attempts) == 1
    event = reranker.api_costs.attempts[0]
    assert event.usage['input'] == 10 and event.amount is None
    plain = '\n'.join(reranker.api_costs.lines(llm_enabled=False))
    assert 'OpenAI / text-embedding-3-large / embedding' in plain
    assert 'price schedule unavailable' in plain and 'LLM summary generation disabled' in plain


@pytest.mark.parametrize('failure', ['http503', 'timeout'])
def test_embedding_sdk_keeps_default_retries_and_counts_every_failed_dispatch(config, monkeypatch, failure):
    calls = []
    monkeypatch.setattr('openai._base_client.time.sleep', lambda _: None)
    def handler(request):
        calls.append(True)
        if failure == 'timeout':
            raise httpx.ReadTimeout('PRIVATE_BODY', request=request)
        return httpx.Response(503, json={'error': {'message': 'PRIVATE_BODY'}})
    with embedding_sdk(monkeypatch, handler):
        reranker = ApiReranker(config)
        with pytest.raises(APITimeoutError if failure == 'timeout' else InternalServerError):
            reranker.get_similarity_score(['first'], ['second'])
    assert len(calls) == len(reranker.api_costs.attempts) == 3
    assert [event.retry for event in reranker.api_costs.attempts] == [False, True, True]
    assert all(event.failed for event in reranker.api_costs.attempts)
    assert 'PRIVATE' not in '\n'.join(reranker.api_costs.lines())


def test_embedding_retry_recovers_before_ranking_with_usage_for_failed_and_successful_calls(config, monkeypatch):
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(True)
        if len(calls) == 1:
            return httpx.Response(503, headers={'retry-after-ms': '1'}, json={
                'error': {'message': 'PRIVATE_BODY'}, 'model': payload['model'],
                'usage': {'prompt_tokens': 5, 'total_tokens': 5}})
        return httpx.Response(200, json={'object': 'list', 'model': payload['model'],
            'data': [{'object': 'embedding', 'embedding': [1, 0, 0], 'index': i}
                     for i in range(len(payload['input']))],
            'usage': {'prompt_tokens': 10, 'total_tokens': 10}})
    with embedding_sdk(monkeypatch, handler):
        reranker = ApiReranker(config)
        np.testing.assert_equal(reranker.get_similarity_score(['first'], ['second']), [[1]])
        np.testing.assert_equal(reranker.get_similarity_score(['first'], ['second']), [[1]])
    assert len(calls) == len(reranker.api_costs.attempts) == 2
    first, final = reranker.api_costs.attempts
    assert first.failed and first.usage['input'] == 5 and final.retry and not final.failed
    assert final.usage['input'] == 10
    assert all(event.amount is None for event in (first, final))  # No reviewed embedding tariff.


def test_embedding_provider_no_retry_header_still_stops_immediately(config, monkeypatch):
    calls = []
    def handler(request):
        calls.append(True)
        return httpx.Response(503, headers={'x-should-retry': 'false'}, json={'error': {'message': 'PRIVATE_BODY'}})
    with embedding_sdk(monkeypatch, handler):
        reranker = ApiReranker(config)
        with pytest.raises(InternalServerError):
            reranker.get_similarity_score(['first'], ['second'])
    assert len(calls) == len(reranker.api_costs.attempts) == 1


def test_embedding_stream_read_timeout_preserves_sdk_recovery_and_unknown_usage(config, monkeypatch):
    calls = []
    monkeypatch.setattr('openai._base_client.time.sleep', lambda _: None)
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout('PRIVATE_PARTIAL_BODY')
            yield b''
    def handler(request):
        calls.append(True)
        if len(calls) == 1:
            return httpx.Response(200, stream=BrokenBody())
        payload = json.loads(request.content)
        return httpx.Response(200, json={'object': 'list', 'model': payload['model'],
            'data': [{'object': 'embedding', 'embedding': [1, 0, 0], 'index': i}
                     for i in range(len(payload['input']))],
            'usage': {'prompt_tokens': 10, 'total_tokens': 10}})
    with embedding_sdk(monkeypatch, handler):
        reranker = ApiReranker(config)
        np.testing.assert_equal(reranker.get_similarity_score(['first'], ['second']), [[1]])
    assert len(calls) == len(reranker.api_costs.attempts) == 2
    first, final = reranker.api_costs.attempts
    assert first.failed and first.usage == {} and first.amount is None
    assert final.retry and not final.failed and final.usage['input'] == 10
    assert 'PRIVATE' not in '\n'.join(reranker.api_costs.lines())


def test_existing_daily_delivery_contains_cost_and_preserves_recipients_state_dedup(pipeline, monkeypatch):
    with open_dict(pipeline):
        chat_config(pipeline)
        pipeline.output.email.enabled = True
        pipeline.output.rss.enabled = False
        pipeline.state.enabled = True
    recipient = pipeline.email.receiver
    calls, sent = [], []
    def handler(request):
        calls.append(True)
        return httpx.Response(200, json=body())
    monkeypatch.setattr('zot2dailypaper.executor.prepare_budget', lambda _: guard())
    monkeypatch.setattr('zot2dailypaper.executor.OpenAI', lambda **kwargs: sdk(handler))
    monkeypatch.setattr('zot2dailypaper.executor.send_email', lambda config, html: sent.append((config.email.receiver, html)))
    papers = [make_sample_paper(title=f'Synthetic {i}', doi=f'10.5555/cost-{i}', url=f'https://example.org/{i}') for i in range(2)]
    executor = Executor(pipeline)
    monkeypatch.setattr(executor.retrievers['arxiv'], 'retrieve_papers', lambda: papers)
    executor.run()
    assert len(calls) == 2 and len(sent) == 1 and sent[0][0] == recipient
    plain = email_plain_text(sent[0][1])
    assert 'CNY 0.00096000' in plain and 'Budget reservation: CNY 0.30' in plain
    assert not State(pipeline.state.path).pending('email')
    repeat = Executor(pipeline)
    monkeypatch.setattr(repeat.retrievers['arxiv'], 'retrieve_papers', lambda: papers)
    repeat.run()
    assert len(calls) == 2 and len(sent) == 1
    # A previously generated pending summary must not carry old spend into this run.
    saved = make_sample_paper(title='Saved summary', doi='10.5555/cost-saved', tldr='已保存的合成摘要。', tldr_status='generated')
    state = State(pipeline.state.path)
    state.add([saved]); state.save()
    resume = Executor(pipeline)
    monkeypatch.setattr(resume.retrievers['arxiv'], 'retrieve_papers', lambda: [])
    resume.run()
    resumed = email_plain_text(sent[1][1])
    assert len(calls) == 2 and len(sent) == 2 and sent[1][0] == recipient
    assert 'No model API calls: new estimated API expenditure 0' in resumed
    assert '1 previously generated summaries reused: no new generation cost' in resumed
    assert not State(pipeline.state.path).pending('email')


@pytest.mark.parametrize('llm_enabled', [True, False])
def test_one_daily_report_retains_embedding_usage_when_summary_guard_attaches(pipeline, monkeypatch, llm_enabled):
    with open_dict(pipeline):
        chat_config(pipeline)
        pipeline.llm.enabled = llm_enabled
        pipeline.output.email.enabled = True
        pipeline.output.rss.enabled = False
        pipeline.state.enabled = True
        pipeline.reranker.api.base_url = 'https://api.openai.com/v1'
    embedding_calls, chats, sent = [], [], []
    def embeddings(request):
        payload = json.loads(request.content)
        embedding_calls.append(True)
        return httpx.Response(200, json={'object': 'list', 'model': payload['model'],
            'data': [{'object': 'embedding', 'embedding': [1, 0, 0], 'index': i}
                     for i in range(len(payload['input']))],
            'usage': {'prompt_tokens': 10, 'total_tokens': 10}})
    def summary(request):
        chats.append(True)
        return httpx.Response(200, json=body())
    with embedding_sdk(monkeypatch, embeddings):
        monkeypatch.setattr(ApiReranker, 'get_similarity_score', API_SIMILARITY)
        monkeypatch.setattr('zot2dailypaper.executor.OpenAI', lambda **kwargs: sdk(summary))
        monkeypatch.setattr('zot2dailypaper.executor.prepare_budget', lambda _: guard())
        monkeypatch.setattr('zot2dailypaper.executor.send_email', lambda config, html: sent.append(html))
        executor = Executor(pipeline)
        monkeypatch.setattr(executor.retrievers['arxiv'], 'retrieve_papers', lambda: [make_sample_paper()])
        executor.run()
    assert embedding_calls and len(chats) == int(llm_enabled)
    assert len(executor.api_costs.attempts) == len(embedding_calls) + len(chats)
    assert executor.reranker.api_costs is executor.api_costs
    if llm_enabled:
        assert executor.model_requests.api_costs is executor.api_costs
    plain = email_plain_text(sent[0])
    assert 'OpenAI / text-embedding-3-large / embedding' in plain and 'INCOMPLETE' in plain
    assert 'No model API calls' not in plain
    if llm_enabled:
        assert 'CNY 0.00048000' in plain
    else:
        assert 'LLM summary generation disabled' in plain


def test_pipeline_embedding_failure_does_not_log_private_provider_body(pipeline, monkeypatch):
    with open_dict(pipeline):
        pipeline.output.email.enabled = True
        pipeline.output.rss.enabled = False
        pipeline.state.enabled = True
    messages = []
    monkeypatch.setattr('openai._base_client.time.sleep', lambda _: None)
    monkeypatch.setattr('zot2dailypaper.executor.logger.error', lambda message: messages.append(message))
    monkeypatch.setattr('zot2dailypaper.executor.send_email', lambda *a: pytest.fail('No recommendations to deliver'))
    def handler(request):
        return httpx.Response(503, json={'error': {'message': 'PRIVATE_PROFILE_AND_ACCOUNT'}})
    with embedding_sdk(monkeypatch, handler):
        monkeypatch.setattr(ApiReranker, 'get_similarity_score', API_SIMILARITY)
        executor = Executor(pipeline)
        monkeypatch.setattr(executor.retrievers['arxiv'], 'retrieve_papers', lambda: [make_sample_paper()])
        with pytest.raises(RuntimeError, match='Embedding API unavailable') as failure:
            executor.run()
    assert messages and 'PRIVATE' not in str(failure.value) + ' '.join(messages)
    assert len(executor.api_costs.attempts) == 3
    assert 'PRIVATE' not in '\n'.join(executor.api_costs.lines())


def test_budget_report_prices_match_admission_snapshot_without_remote_writes(config, monkeypatch):
    chat_config(config)
    record = dict(budget.VERIFIED_PRICING, input_cny_per_million='6', output_cny_per_million='18')
    reviews = []
    def review(*args):
        reviews.append(True)
        # A warning/review after admission may observe another price/day.
        observed = dict(record, input_cny_per_million='30') if len(reviews) > 1 else record
        return observed, 'unavailable', ''
    monkeypatch.setattr(budget, 'review', review)
    monkeypatch.setattr(budget, 'git', lambda *a, **kw: pytest.fail('Reporting must not touch remote budget state'))
    monkeypatch.setattr(budget, 'reserve_local_day', lambda cap, zone: window_at(budget.budget_now()))
    request_guard = budget.prepare_budget(config.llm)
    assert request_guard.per_call == Decimal('.00864')
    record['input_cny_per_million'] = '30'  # Later mutation cannot reprice admitted calls.
    with sdk(lambda _: httpx.Response(200, json=body())) as client:
        make_sample_paper().generate_tldr(client, config.llm, request_guard)
    assert request_guard.api_costs.attempts[0].amount == Decimal('.00096')
    assert request_guard.price_schedule.input_rate == 6
