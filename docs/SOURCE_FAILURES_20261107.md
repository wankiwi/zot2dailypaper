# Source degradation follow-up, 2026-11-07

Observed in [production run 42313797037](https://github.com/wankiwi/zot2dailypaper/actions/runs/42313797037).

- JACS, JCTC and JPCL RSS returned HTTP 403. Their built-in presets now use the existing public Crossref journal queries and retain both available ISSNs. No publisher access-control workaround is introduced. Custom feeds remain explicit overrides.
- Nature Digest's generated `/ndigest.rss` returned 404. Discovery and old cached catalogue entries no longer synthesize that feed; the journal remains selected for Crossref metadata. This is not a claim that Crossref contains every publisher article.
- A missing first-page Crossref index for one ISSN no longer discards another ISSN's successful results. All indexes missing still reaches the existing explicit RSS-only fallback. Mid-pagination failures, rate limits and other errors are not treated as an empty successful result.
- The six new Nature titles absent from Crossref still carry one RSS-only warning with unverified historical coverage. The duplicate HTTP-error warning is removed, not the coverage limitation. No hardcoded permanent exclusion is added, so future runs can discover newly indexed titles.
- Publisher access refusals and Semantic Scholar 429 remain genuine limitations. Recovery already stops the blocked provider and verifies alternative metadata. The remaining missing abstract must stay explicitly missing; it cannot be safely fabricated or guaranteed by a code change.

No production rerun, email, model call, delivery-state reset or budget change is part of this repair.
