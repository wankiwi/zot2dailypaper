# Daily API cost reporting

The existing daily recommendation email now includes the current run's API usage and estimated cost in both HTML and its plaintext alternative. Recipients, schedule, recommendation selection, ranking formulas, channel receipts, and reservation/deduplication policies are unchanged. No separate report email is created.

Summary dispatches snapshot the same reviewed SiliconFlow/DeepSeek input/output rates that admitted the winning `BudgetRequests` reservation. Each schedule has a rate/date fingerprint, verification date, validity date and currency. Expired schedules are marked stale; the existing pricing warning and conservative budget policy still apply. Rendering cost telemetry does not fetch prices, query account balances, or write budget state. The normal daily renderer retains its existing public pricing warning check; delivery-only replay and the new synthetic preview suppress that check.

Each actual SDK dispatch creates exactly one attempt. Automatic retries are disabled at both chat and optional API-embedding boundaries. Chat's existing bounded application retries each receive their own budget precharge and usage record. Tokens are captured before summary content validation, so truncated, refused or otherwise invalid summaries still contribute their measured estimate. Exceptions with usable provider usage retain that usage; timeouts, missing/malformed usage, unverified response models and missing tariffs have unknown cost. A known subtotal with any unknown attempts is explicitly incomplete, never a total that silently treats unknown as zero. Positive estimates smaller than the display precision are shown with a less-than bound.

Input/output, cache reads/writes and reasoning token counts are shown independently; unreported counts are unknown. For OpenAI-compatible chat usage, cache counts are subsets of prompt/input and reasoning is a subset of completion/output, so neither is added twice. The reviewed SiliconFlow schedule applies its conservative peak input rate to all input tokens, without claiming cache discounts or time-of-day savings. This is a usage-priced estimate, not a provider invoice, settled charge, account balance or tax calculation.

Provider, requested model, API type, schedule version and currency form separate report groups. Currencies are subtotaled separately with no conversion. The optional embedding path reports its returned token usage but has unknown currency/cost because this repository has no reviewed embedding tariff. This change does not enable API embeddings, add an embedding budget grant, or deploy the separately evaluated OpenAI path. A future provider or model needs an exact reviewed schedule and its existing budget authorization; the report must never borrow another model's price.

The CNY whole-day reservation is displayed separately from estimated expenditure. No amount is refunded or inferred from `remaining`: that value is conservative dispatch accounting, and becomes zero after some guard aborts. The existing `llm_budget.json` schema, immutable reservations, migration digests, CAS writes and delivery history are preserved without a new reporting ledger or reconciliation writes.

Run telemetry lives in memory and covers all instrumented model calls in that invocation, including embedding work for ranking and summaries that may not be in the final pending batch. Bibliographic retrieval, SMTP, D1, and subscription/account-level fees are outside this token-usage report. It is not an all-day invoice or a reconstruction of prior runs. A crash before delivery may prevent that run's report from being sent; existing reservations remain consumed. Historical costs for saved summaries are unknown here. Reusing saved summaries or replaying a delivery makes no new generation charge and never repeats their historical cost. A standalone render without run telemetry explicitly says cost is unavailable; a measured run with zero dispatches reports zero new model API cost. Disabling LLM summaries does not imply API embeddings are disabled.

Only numeric usage, safe model identifiers and fixed provider labels are retained. Prompts, private interest profiles, account/request identifiers, endpoint paths/credentials, response text and provider exception bodies are excluded from cost reports.

## Local verification and rollout

`tests/test_api_costs.py` exercises pricing, cache/reasoning arithmetic, invalid/unknown usage, failures, concurrency, separate currencies and safe output. `tests/test_api_cost_integration.py` uses the real SDK with `httpx.MockTransport`, synthetic state and stub SMTP. Existing budget CAS/concurrency and replay tests continue to cover preserved reservations and transport deduplication. No real messages, paid requests or production history writes are needed for these tests.

Generate the five HTML/plaintext previews with `uv run --frozen python scripts/preview_api_costs.py`. They never load credentials or contact services; synthetic USD prices are examples only.

Publishing requires review and permission to push/open a PR, then merge/deploy through the existing process. No new schedule, recipient, credential or settings change, state migration or historical backfill is required. Before rollout, review the price-staleness notice and the optional embedding behavior: its SDK retries are now explicitly zero so failed attempts cannot remain hidden. A newly deployed run will report its own instrumented usage; local tests cannot establish provider invoicing or SMTP inbox delivery.

Verified locally on 2026-10-29 using the existing Python 3.13 environment, with this checkout selected explicitly so subprocess tests import it too:

```bash
PYTHONPATH=/workspace/zot2dailypaper/src /workspace/zotero-arxiv-daily/.venv/bin/python -m pytest -q
# 1362 passed, 1 skipped (optional Playwright unavailable in this venv), 1 deselected (slow)

PYTHONPATH=/workspace/zot2dailypaper/src /workspace/zotero-arxiv-daily/.venv/bin/python -m pytest tests/test_api_costs.py tests/test_api_cost_integration.py tests/test_budget.py tests/test_budget_local_day.py tests/test_pricing.py tests/test_pricing_state.py tests/test_delivery_replay.py -q
# 163 passed after the final admission-price snapshot hardening

ruff check --select F401,F821,F841 src scripts tests
git diff --check
```

Five synthetic HTML/plaintext pairs and three Chromium screenshots with overflow metrics are included under `docs/previews/api-costs/`. Browser verification used the separately available system Playwright/Chromium with external requests blocked. No paid requests, real email, production state writes or publication operations were performed. Git pushes inside existing concurrency tests target disposable local bare repositories only.
