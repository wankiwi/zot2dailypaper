"""No network, live credentials, budget writes or paid calls."""
import json
from types import SimpleNamespace

import pytest

from scripts import validate_llm as smoke
from zot2dailypaper.budget import BudgetUnavailable
from zot2dailypaper.openai_pricing import QuoteUnavailable


def test_public_example_has_no_private_source():
    paper = smoke.public_example()
    assert paper.url == 'https://arxiv.org/abs/1706.03762v7'
    assert len(paper.abstract.split()) == 20
    assert not paper.authors and not paper.full_text
    assert 'Transformer' in paper.abstract


def test_safe_quote_machine_code_survives_budget_wrapper():
    try:
        try:
            raise QuoteUnavailable('exact_openai_price_row_missing')
        except QuoteUnavailable:
            raise BudgetUnavailable('Safe public message', reason='billing_unverified') from None
    except BudgetUnavailable as exc:
        report = smoke.safe_budget_failure(exc, [])
    assert report == {'status': 'blocked_before_paid', 'error_type': 'BudgetUnavailable',
                      'reason': 'billing_unverified', 'paid_request_count': 0,
                      'quote_error': 'exact_openai_price_row_missing'}


@pytest.mark.parametrize('context', [ValueError('SECRET_BODY'), QuoteUnavailable('unsafe body: SECRET_BODY')])
def test_untrusted_exception_bodies_are_not_reported(context):
    try:
        try:
            raise context
        except Exception:
            raise BudgetUnavailable('Safe public message', reason='billing_unverified') from None
    except BudgetUnavailable as exc:
        report = smoke.safe_budget_failure(exc, [])
    assert 'SECRET_BODY' not in json.dumps(report)
    assert 'quote_error' not in report


def test_usage_observer_retains_cache_and_reasoning_breakdown():
    events = [{'http_status': None}]
    response = SimpleNamespace(request=SimpleNamespace(method='POST',
        url=SimpleNamespace(path='/v1/chat/completions')), status_code=200, read=lambda:None,
        json=lambda:{'usage':{'prompt_tokens':60,'completion_tokens':20,'total_tokens':80,
                             'prompt_tokens_details':{'cached_tokens':10},
                             'completion_tokens_details':{'reasoning_tokens':0}}})
    smoke.response_audit(events)(response)
    assert events == [{'http_status':200,'prompt_tokens':60,'completion_tokens':20,'total_tokens':80,
                       'cached_input_tokens':10,'reasoning_tokens':0}]


def test_budget_preflight_blocks_before_any_credentialed_client(monkeypatch, capsys):
    from omegaconf import OmegaConf
    monkeypatch.setenv('CUSTOM_CONFIG', 'llm: {enabled: true}')
    def blocked(_):
        try:
            raise QuoteUnavailable('ecb_quote_format_changed')
        except QuoteUnavailable:
            raise BudgetUnavailable('Safe', reason='billing_unverified') from None
    monkeypatch.setattr(smoke, 'budget_plan', blocked)
    monkeypatch.setattr(smoke, 'OpenAI', lambda **_:pytest.fail('credentialed client created'))
    monkeypatch.setattr(smoke, 'state_snapshot', lambda:pytest.fail('delivery state read'))
    assert smoke.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report['quote_error'] == 'ecb_quote_format_changed'
    assert report['paid_request_count'] == 0
