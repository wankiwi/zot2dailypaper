# Email previews

All papers are synthetic. Generate HTML/plain text with `uv run --frozen python scripts/preview_email.py`; no credentials, network calls or delivery are used.

- `daily`: 25 journals, 15 preprints, 5 random picks, independently numbered in three vertically stacked sections.
- `showcase`: successful Chinese summary, missing abstract, failed-summary fallback and a long affiliation.
- `empty` and `shortage`: empty groups and insufficient eligible papers.
- PNGs verify a single column at desktop and mobile widths. Affiliations use a deterministic character limit, not CSS clipping.

Successful AI summaries replace original abstracts in both email formats; original abstracts and affiliations remain complete in internal data. Without a valid summary, email shows the original abstract or a truthful unavailable notice.

Presentation tables, inline CSS and Aptos/system fallbacks require no media query, external images, scripts or tracking pixels. Browser checks do not establish exact Gmail, Outlook or Apple Mail rendering; clients may simplify corners or clip long digests. Attribution and license remain in README and LICENSE.

API-cost previews are generated separately, entirely offline:

```bash
uv run --frozen python scripts/preview_api_costs.py
```

`api-costs/` contains HTML/plaintext pairs for measured usage, a timeout plus retry with incomplete accounting, multiple providers/currencies/model types, disabled generation with zero calls, and delivery-only replay of a saved summary. All usage is synthetic. The USD demo tariff is illustrative; it does not configure OpenAI or change the deployed provider. See [cost accounting](../API_COST_REPORTING.md) for semantics and rollout limits.

`partial-mobile.png`, `mixed-desktop.png`, and `replay-mobile.png` were rendered in local Chromium with all external requests blocked. `layout-metrics.json` records 375px mobile and 1200px desktop viewports with no horizontal overflow. These browser checks do not establish exact email-client rendering.
