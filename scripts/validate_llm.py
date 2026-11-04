"""One explicitly requested, budget-guarded summary; no email or delivery writes."""
import json
import os
from pathlib import Path
import re

import httpx
from omegaconf import OmegaConf
from openai import OpenAI, APIStatusError

from zot2dailypaper.budget import BudgetUnavailable, budget_plan, prepare_budget, git
from zot2dailypaper.openai_pricing import QuoteUnavailable
from zot2dailypaper.protocol import Paper


def public_example():
    # A fixed public abstract excerpt, independent of private recommendations.
    # Twenty words from https://arxiv.org/abs/1706.03762v7, checked 2026-11-04.
    return Paper(source='arxiv', title='Attention Is All You Need', authors=[],
                 abstract='We propose a new simple network architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely.',
                 url='https://arxiv.org/abs/1706.03762v7')


def safe_budget_failure(exc, events):
    report = {'status': 'blocked_before_paid', 'error_type': type(exc).__name__,
              'reason': exc.reason, 'paid_request_count': len(events)}
    # Only emit one of this module's machine codes; never exception bodies from
    # providers, credentials, private configuration, or returned document text.
    context = exc.__context__
    if isinstance(context, QuoteUnavailable) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', str(context)):
        report['quote_error'] = str(context)
    return report


def request_audit(events):
    def observe(request):
        if request.method == 'POST' and request.url.path == '/v1/chat/completions':
            events.append({'http_status': None})
    return observe


def response_audit(events):
    def observe(response):
        if response.request.method != 'POST' or response.request.url.path != '/v1/chat/completions':
            return
        event = events[-1]
        event['http_status'] = response.status_code
        response.read()
        try:
            usage = response.json().get('usage', {})
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                if type(usage.get(key)) is int:
                    event[key] = usage[key]
            for target, group, key in (
                    ('cached_input_tokens', 'prompt_tokens_details', 'cached_tokens'),
                    ('reasoning_tokens', 'completion_tokens_details', 'reasoning_tokens')):
                details = usage.get(group) or {}
                if isinstance(details, dict) and type(details.get(key)) is int:
                    event[target] = details[key]
        except (ValueError, TypeError, AttributeError):
            pass
    return observe


def validate(config, paper, client, events):
    config = OmegaConf.merge(config, {'request': {'max_attempts': 1}})  # Explicit smoke validation authorizes one paid attempt only.
    budget_plan(config)  # Verify destination before transmitting even a model-list request.
    if config.get('language') != 'Chinese' or config.get('input_mode', 'abstract') != 'abstract':
        raise ValueError('Validation requires Chinese abstract mode')
    original = paper.abstract
    if not original:
        raise ValueError('A public paper with an original abstract is required')
    models = client.models.list()
    if config.generation_kwargs.model not in {model.id for model in models.data}:
        return {'status': 'model_not_listed', 'paid_request_count': 0}
    guard = prepare_budget(config)  # Durable whole-day reservation, with no refund/reset.
    paper.generate_tldr(client, config, requests=guard)
    chinese = bool(re.search(r'[\u4e00-\u9fff]', paper.tldr or ''))
    generated = paper.tldr_status == 'generated'
    sentences = len([part for part in re.split(r'[。！？!?]+', paper.tldr or '') if part.strip()])
    preserved = paper.abstract == original
    return {'status': 'success' if generated and chinese and sentences == 1 and preserved else 'summary_failed',
            'summary_status': paper.tldr_status, 'summary_error': paper.tldr_error,
            'summary_error_reason': paper.tldr_error_reason, 'summary_attempts': paper.tldr_attempts,
            'contains_chinese': chinese if generated else False,
            'sentence_count': sentences if generated else 0,
            'summary_characters': len(paper.tldr or '') if generated else 0,
            'original_abstract_preserved': preserved, 'paid_request_count': len(events),
            'usage': events, 'reserved_day': guard.day,
            'cost_report': list(guard.api_costs.lines())}


def state_snapshot():
    ref = git('ls-remote', '--heads', 'origin', 'refs/heads/paper-state').decode().split()
    if len(ref) != 2 or ref[1] != 'refs/heads/paper-state':
        raise ValueError('Existing delivery state required')
    git('fetch', '--no-tags', '--no-write-fetch-head', 'origin', ref[0])
    blob = git('rev-parse', f'{ref[0]}:recommendations.json').decode()
    return blob  # Verify only the blob identity; do not read private delivery contents.


def main():
    events = []
    report = {'status': 'validation_failed'}
    try:
        root = Path(__file__).resolve().parents[1]
        base = OmegaConf.load(root / 'config/base.yaml').llm
        supplied = OmegaConf.create(os.environ['CUSTOM_CONFIG']).llm
        config = OmegaConf.merge(base, supplied)
        budget_plan(config)
        before = state_snapshot()
        paper = public_example()
        with httpx.Client(follow_redirects=False, event_hooks={
                'request': [request_audit(events)], 'response': [response_audit(events)]}) as transport:
            with OpenAI(api_key=config.api.key, base_url=config.api.base_url,
                        max_retries=0, timeout=60, http_client=transport) as client:
                report = validate(config, paper, client, events)
        # This fixed example is public and unrelated to personal rankings.
        report['public_example_url'] = paper.url
        if report['status'] == 'success':
            report['generated_summary'] = paper.tldr
        after = state_snapshot()
        report['delivery_state_unchanged'] = before == after
        if before != after:
            report['status'] = 'delivery_state_changed_concurrently'
    except BudgetUnavailable as exc:
        report = safe_budget_failure(exc, events)
    except APIStatusError as exc:
        report = {'status': 'provider_rejected', 'http_status': exc.status_code,
                  'paid_request_count': len(events)}
    except Exception as exc:
        report = {'status': 'validation_failed', 'error_type': type(exc).__name__,
                  'paid_request_count': len(events)}
    print(json.dumps(report, ensure_ascii=False))  # Only fixed public example output, safe codes and usage.
    return 0 if report['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
