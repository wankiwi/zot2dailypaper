# Automatic public price verification

The existing daily allowance supplied by CUSTOM_CONFIG remains authoritative.
The non-thinking request bound, response-usage audit, daily ledger reservation,
no-refund policy and paid-request retry restrictions remain in force.

For the reviewed CN provider (`api.siliconflow.cn`) and exact model
`deepseek-ai/DeepSeek-V4-Flash`, budget planning and email warning generation
share one bounded price-check attempt per process and UTC date. The checker
uses a separate HTTP session without API keys, .netrc or ambient proxies.
It makes only GET requests to these two fixed public sources:

- https://www.siliconflow.cn/pricing
- https://api-docs.siliconflow.cn/docs/api/chat-completions-post

The adapter checks the CN yuan quote, exact model row, input/output/cache column
order, explicit M Tokens units and complete daily time tiers. It estimates
using the highest published input and output rates, treating every input token
as uncached. It never relies on low-traffic timing or cache discounts to admit
more paid requests. The current public quote has peak input/output/cache rates
of CNY 3/9/0.30 per million tokens; the corresponding low-traffic rates are
1.50/4.50/0.15. No currency conversion or provider/model substitution occurs.

The documentation must still declare boolean enable_thinking with false allowed,
describe max_tokens as excluding chain-of-thought, and name this exact model in
its reasoning-effort contract. This verifies the documented switch, not the
provider's actual billing behavior. Requests still explicitly disable thinking,
and unexpected reasoning tokens or contradicted usage stop queued paid calls
under the existing response audit. Thinking-mode prices are never substituted
for the approved non-thinking contract.

Only successful parsing of both sources and successful cache persistence update
the verification timestamp. A page returning 200, an available URL, a saved
receipt, or the passage of a date is insufficient. Redirects, oversized bodies,
unknown currencies/units/layouts, incomplete time tiers, missing model rows or
changed contract fields remain unverified. No raw provider error enters email
or logs. Each source is limited to 1 MiB, without retries or redirects; the
transport uses 3-second connection / 8-second read bounds and a shared deadline.

The public-only `data/llm_pricing.json` receipt retains observed peaks, source
hashes and verification time. The existing paper-state restore/save mechanism
retains it across normal runs; it does not alter the budget ledger. Estimates
never fall below the reviewed seed or a previously verified higher rate. Price
increases therefore reduce the number of admitted requests without increasing
the configured cap; decreases do not expand the previous allowance.

If a check fails, the original timestamp is retained. Email and logs warn while
LLM continues under the last confirmed conservative estimate and existing daily
ledger. This follows the user's warn-and-continue policy: an undetected price
increase can still make actual provider charges exceed the estimate. The checker
is a conservative public-page adapter, not an authoritative live billing cap.
Public page structure can change; a parser change then needs code review.

Unit tests use source fixtures, temporary receipts and a failing default HTTP
stub. `tests/test_email_layout.py` additionally checks 1200/375/320px Chromium
viewports with synthetic citations and all external requests blocked. It needs
the optional Playwright test tool and a local Chrome or Playwright Chromium.
These viewport checks do not establish rendering in every proprietary email
client. No validation should send real email, create a budget reservation on
the remote, call paid models or write Zotero.
