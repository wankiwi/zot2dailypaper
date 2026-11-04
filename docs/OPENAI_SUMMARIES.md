# Official OpenAI daily summaries

The existing official `OPENAI_API_KEY` is reused without copying or replacing it.
Jina ranking, profile weights, quotas, source filtering and recipient are unchanged.
Only selected papers' bounded public title/abstract enter the summary request;
Zotero references, private profile, notes and fulltexts are not sent.

The supported official route is `https://api.openai.com/v1/chat/completions`,
model `gpt-6-luna`. It uses the existing SDK and summary parser, explicitly requests
`reasoning_effort=none`, `service_tier=default`, `store=false`, `n=1` and
`max_completion_tokens=96`. No tools or provider-specific `enable_thinking` field
are sent. The output bound includes reasoning tokens; unexpected reasoning,
processing tier/model, invalid usage, truncation or refusals retain the original
abstract. The installed SDK is tested through an in-process HTTP transport only.

Merge only these three fields into the existing private `CUSTOM_CONFIG` variable;
do not replace the complete variable or change the existing key reference:

```yaml
llm:
  enabled: true
  api:
    base_url: https://api.openai.com/v1
  generation_kwargs:
    model: gpt-6-luna
```

Keep the existing `llm.budget.enabled: true`, `daily_cny: 0.30`, and
`timezone: Asia/Singapore`. A manual `llm_mode=disabled` still overrides this.
Saving configuration does not send mail or call a model. The next ordinary
scheduled workflow uses the new settings; no extra production run is required.

The official pricing page was checked on 2026-11-02: Standard USD per million
input/output/cached input/30-minute write/12-hour write =
0.10/0.50/0.01/0.125/0.25. Admission conservatively reserves the maximum published
Luna mode/cache input rate (USD0.50) and output rate (USD1.00), without cache discounts.
The request itself uses Standard/default processing. This reserve ceiling is
explicitly distinguished from actual provider invoices.

ECB 2026-10-30 reference rates EUR/USD1.1172 and EUR/CNY7.4326 imply about CNY6.653
per USD. Add 10% headroom and round upward to the next tenth: CNY7.4 per USD.
The 1152-input/96-output token bound then reserves CNY0.0049728 per attempt.
50 attempts reserve CNY0.24864; 60 fit within CNY0.30 and a 61st cannot be admitted.
Each retry consumes a new slot; failed/unknown usage is never zero or refunded.
The report records measured token/cache/reasoning fields, quote version and this
conservative conversion, not an actual card settlement rate or taxes.

Only public, unauthenticated, bounded GETs refresh the OpenAI price table and ECB
XML; no redirect, alternate host or retry is used. Network unavailability may use
only the still-fresh independently reviewed seed. Maximum age is seven calendar
days; a reachable but changed/invalid schema or stale/future date fails closed.
Both price and FX must be fresh, including immediately before dispatch. Unknown
or expired estimates stop new requests and keep original abstracts.

The same durable `paper-state/llm_budget.json` Asia/Singapore daily claim is reused.
A provider switch does not reset or create a second budget. Existing UTC/local
claims remain intact; a crash/rerun cannot reclaim the whole-day reservation.
No production ledger is written by the offline tests.

The isolated Jina full-pool experiment remains pinned to its own existing branch
and no-paid-call input; these summary changes do not alter it.

Sources:
- https://developers.openai.com/api/docs/models/gpt-6-luna
- https://developers.openai.com/api/docs/pricing
- https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html
- https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml


## Raw Markdown pricing compatibility

The public Markdown export groups short- and long-context rates in an eleven-column
row, unlike the six-column visible-page table. The parser accepts these two exact
reviewed schemas only. For the expanded form it requires the exact Standard,
Batch, Flex and Fast headings, complete model rows, USD units and ordered cache
rates in both context groups. Missing, duplicated or unknown tiers/columns fail
closed rather than silently applying the seed. The admission code independently
checks that the input bound remains within the reviewed 272,000-token short-context
boundary. Its actual bound remains 1,152 input tokens, so short-context maxima
preserve the existing USD0.50/1.00 reserve ceiling and CNY0.30 daily limit.

The expanded public source was inspected on 2026-11-04 in Actions run
41776433387; full-source SHA256 was
`842ac0dce2324251d274026d197a81473e72ff40eef1194bdd4e6b32833d8f55`.
Only public pricing structure was logged. Production budget warnings retain safe
machine-readable quote failure codes, without provider bodies or credentials.
