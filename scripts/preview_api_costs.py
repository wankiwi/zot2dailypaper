"""Render synthetic cost reports offline: no settings, credentials, calls or sends."""
import argparse
from pathlib import Path

from zot2dailypaper.api_costs import ApiCosts, PriceSchedule, summary_schedule
from zot2dailypaper.budget import VERIFIED_PRICING
from zot2dailypaper.construct_email import email_plain_text, render_email
from zot2dailypaper.protocol import Paper


CASES = ('measured', 'partial', 'mixed', 'disabled', 'replay')


def synthetic_costs(case):
    costs = ApiCosts()
    if case in ('disabled', 'replay'):
        costs.saved_summaries = 1 if case == 'replay' else 0
        return costs
    schedule = summary_schedule(VERIFIED_PRICING)
    costs.reserved('.30', '2026-10-29', 'Asia/Singapore')
    for retry in ([False, True] if case == 'partial' else [False]):
        with costs.attempt('https://api.siliconflow.cn/v1', VERIFIED_PRICING['model'], 'chat',
                           schedule=schedule, retry=retry) as event:
            if case == 'partial' and not retry:
                event.failed = True  # Synthetic timeout without usage, no actual dispatch.
            else:
                event.observe({'model': VERIFIED_PRICING['model'], 'usage': {
                    'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
                    'prompt_tokens_details': {'cached_tokens': 30, 'cache_write_tokens': 0},
                    'completion_tokens_details': {'reasoning_tokens': 0}}})
    if case == 'mixed':
        illustrative = PriceSchedule('OpenAI', 'synthetic-model', 'chat', 'USD', '2', '8',
                                     'synthetic-usd-demo-v1', '2026-10-29', '2026-10-29')
        with costs.attempt('https://api.openai.com/v1', 'synthetic-model', 'chat', schedule=illustrative) as event:
            event.observe({'model': 'synthetic-model', 'usage': {
                'prompt_tokens': 1000, 'completion_tokens': 200, 'total_tokens': 1200,
                'completion_tokens_details': {'reasoning_tokens': 50}}})
        with costs.attempt('https://api.openai.com/v1', 'synthetic-embedding', 'embedding') as event:
            event.observe({'model': 'synthetic-embedding', 'usage': {'prompt_tokens': 250, 'total_tokens': 250}})
    return costs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('docs/previews/api-costs'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        paper = Paper(source='arxiv', title=f'Synthetic API-cost preview: {case}', authors=['Example Author'],
                      abstract='Synthetic content for offline rendering only.', url='https://example.org/synthetic',
                      score=85, tldr='该合成示例仅用于预览每日邮件中的费用报告。' if case == 'replay' else None,
                      tldr_status='generated' if case == 'replay' else 'not_generated')
        html = render_email([paper], api_costs=synthetic_costs(case), llm_enabled=case not in ('disabled', 'replay'),
                            delivery_only=case == 'replay',
                            pricing_notice='Synthetic preview only: no API calls or email sends. All usage is invented; '
                            'the USD demo tariff is illustrative and does not configure or migrate any provider.')
        (args.output / f'{case}.html').write_text(html, encoding='utf-8')
        (args.output / f'{case}.txt').write_text(email_plain_text(html), encoding='utf-8')
    print(f'Wrote {len(CASES)} synthetic HTML/plaintext pairs to {args.output}; no calls or sends.')


if __name__ == '__main__':
    main()
