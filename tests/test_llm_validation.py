from decimal import Decimal
import json

import httpx
from omegaconf import OmegaConf
from openai import OpenAI, AuthenticationError
import pytest

from scripts import validate_llm as smoke
from zot2dailypaper.budget import BudgetRequests, utc_day
from tests.canned_responses import make_sample_paper


def setup_client(monkeypatch, handler):
    events, reservations = [], []
    config = OmegaConf.create({'api': {'base_url': 'https://api.siliconflow.cn/v1'},
                              'language': 'Chinese', 'input_mode': 'abstract',
                              'budget': {'enabled': True, 'daily_cny': 0.20},
                              'generation_kwargs': {'model': 'deepseek-ai/DeepSeek-V4-Flash'}})
    def reserve(_):
        reservations.append(True)
        return BudgetRequests(Decimal('.20'), Decimal('.00432'), utc_day())
    monkeypatch.setattr(smoke, 'prepare_budget', reserve)
    transport = httpx.Client(transport=httpx.MockTransport(handler), event_hooks={
        'request': [smoke.request_audit(events)], 'response': [smoke.response_audit(events)]})
    client = OpenAI(api_key='test-only', base_url=config.api.base_url, max_retries=0,
                    http_client=transport)
    return config, client, events, reservations


def test_one_summary_uses_guard_and_preserves_abstract(monkeypatch):
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'deepseek-ai/DeepSeek-V4-Flash'}]})
        payload = json.loads(request.content)
        assert payload['max_tokens'] == 96 and payload['enable_thinking'] is False
        return httpx.Response(200, json={'id': 'test', 'model': payload['model'],
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': '该研究改善了分子模拟的预测精度。'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}})
    config, client, events, reservations = setup_client(monkeypatch, handler)
    paper = make_sample_paper()
    with client:
        report = smoke.validate(config, paper, client, events)
    assert report['status'] == 'success' and report['paid_request_count'] == 1
    assert report['contains_chinese'] and report['original_abstract_preserved']
    assert report['usage'][0]['total_tokens'] == 120 and len(reservations) == 1


def test_invalid_key_stops_before_reservation(monkeypatch):
    config, client, events, reservations = setup_client(
        monkeypatch, lambda request: httpx.Response(401, json={'error': {'message': 'SECRET_DO_NOT_PRINT'}}))
    with client, pytest.raises(AuthenticationError):
        smoke.validate(config, make_sample_paper(), client, events)
    assert not reservations and not events


@pytest.mark.parametrize('mode', ['500', 'timeout'])
def test_paid_failures_never_retry_and_keep_abstract(monkeypatch, mode):
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'deepseek-ai/DeepSeek-V4-Flash'}]})
        if mode == 'timeout':
            raise httpx.ReadTimeout('ambiguous billing', request=request)
        return httpx.Response(500, json={'error': {'message': 'Do not retry'}})
    config, client, events, reservations = setup_client(monkeypatch, handler)
    with client:
        report = smoke.validate(config, make_sample_paper(), client, events)
    assert report['status'] == 'summary_failed' and report['paid_request_count'] == 1
    assert report['original_abstract_preserved'] and len(reservations) == 1


def test_workflow_does_not_provide_delivery_credentials():
    from pathlib import Path
    import yaml
    workflow = yaml.load((Path(__file__).parents[1] / '.github/workflows/test.yml').read_text(), Loader=yaml.BaseLoader)
    job = workflow['jobs']['llm-validation']
    assert 'schedule' not in workflow['on']
    assert job['permissions'] == {'contents': 'write'}
    steps = {step.get('name'): step for step in job['steps']}
    env = steps['Validate one Chinese summary without delivery']['env']
    assert set(env) == {'CUSTOM_CONFIG', 'OPENAI_API_KEY'}
    assert steps['Validate one Chinese summary without delivery']['run'].endswith('scripts/validate_llm.py')
