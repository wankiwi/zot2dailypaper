"""Reviewed raw official Markdown, no network or production state.
Source: https://developers.openai.com/api/docs/pricing.md (2026-11-04).
Full-source SHA256: 842ac0dce2324251d274026d197a81473e72ff40eef1194bdd4e6b32833d8f55
Evidence: Actions run 41776433387; headings/rows retained exactly.
"""
from decimal import Decimal
import copy
import pytest
from zot2dailypaper import budget, openai_pricing as prices

RAW = "Prices per 1M tokens.\n\n### Standard pricing data\n\n| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n| gpt-6-luna | $0.10 | $0.50 | $0.01 | $0.125 | $0.25 | $0.20 | $0.75 | $0.02 | $0.25 | $0.50 |\n\n### Batch pricing data\n\n#### Batch\n\n| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n| gpt-6-luna | $0.05 | $0.25 | $0.005 | $0.0625 | $0.125 | $0.10 | $0.375 | $0.01 | $0.125 | $0.25 |\n\n#### Flex\n\n| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n| gpt-6-luna | $0.05 | $0.25 | $0.005 | $0.0625 | $0.125 | $0.10 | $0.375 | $0.01 | $0.125 | $0.25 |\n\n### Fast pricing data\n\n| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n| gpt-6-luna | $0.20 | $1.00 | $0.02 | $0.25 | $0.50 | $0.40 | $1.50 | $0.04 | $0.50 | $1.00 |\n"
FAST = RAW[RAW.index('### Fast pricing data'):]

def test_current_raw_markdown_preserves_short_request_ceiling():
    assert prices.parse_prices(RAW) == {
        'input_usd_per_million': '0.50', 'output_usd_per_million': '1.00'}

@pytest.mark.parametrize('bad', [
    RAW.replace('Short context output', 'Short context unknown'),
    RAW.replace('Long context 12h write', 'Long context unknown'),
    RAW.replace('Prices per 1M tokens.', 'Prices per 1K tokens.'),
    RAW.replace('$0.10', 'EUR0.10'),
    RAW.replace('$0.20 | $0.75', '- | $0.75', 1),
    RAW.replace('$0.20 | $0.75', '$0.01 | $0.75', 1),
    RAW.replace('### Fast pricing data', '### Unreviewed pricing data'),
    RAW.replace('gpt-6-luna', 'gpt-6-luna-unreviewed'),
    RAW.replace(FAST, ''),
    RAW + '\n' + FAST,
])
def test_changed_ambiguous_or_incomplete_tables_fail_closed(bad):
    with pytest.raises(prices.QuoteUnavailable):
        prices.parse_prices(bad)

def test_fast_increase_is_not_hidden_by_discount_rows():
    increased = RAW.replace('$0.20 | $1.00 | $0.02', '$0.20 | $1.40 | $0.02')
    assert Decimal(prices.parse_prices(increased)['output_usd_per_million']) == Decimal('1.40')

def test_unrecognized_additional_model_table_is_not_ignored():
    added = '\n| Model | Other |\n| --- | --- |\n| gpt-6-luna | $99.00 |\n'
    with pytest.raises(prices.QuoteUnavailable, match='columns'):
        prices.parse_prices(RAW + added)

def official(config):
    cfg = copy.deepcopy(config.llm)
    cfg.enabled = True
    cfg.api.base_url = 'https://api.openai.com/v1'
    cfg.generation_kwargs.model = prices.MODEL
    cfg.budget.enabled = True
    cfg.budget.daily_cny = .30
    cfg.budget.timezone = 'Asia/Singapore'
    return cfg

def test_enlarged_input_bound_cannot_use_short_context_quote(config, monkeypatch):
    monkeypatch.setattr(budget, 'PROMPT_BYTES', prices.SHORT_CONTEXT_TOKEN_CEILING + 1)
    monkeypatch.setattr(prices, 'quote', lambda *a, **k: pytest.fail('quote must not be used'))
    with pytest.raises(budget.BudgetUnavailable, match='short-context'):
        budget.budget_plan(official(config))

def test_safe_parser_error_survives_production_budget_message(config, monkeypatch):
    def broken(*_):
        raise prices.QuoteUnavailable('openai_price_columns_changed')
    monkeypatch.setattr(prices, 'quote', broken)
    with pytest.raises(budget.BudgetUnavailable, match='openai_price_columns_changed') as error:
        budget.budget_plan(official(config))
    assert error.value.reason == 'billing_unverified'
