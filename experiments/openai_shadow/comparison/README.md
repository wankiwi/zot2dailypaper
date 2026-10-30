# Isolated paired pilot

This manual experiment compares the actual frozen production Jina baseline,
direct Decisions, and the same Decisions assessment with an ordinary LLM evidence
record. It does not run the daily workflow or send mail. Production remains at
`385a7574def6f948ca60f57c3f95bb6b28c6f2c5`.

The private protocol fixes equal multi-select research-value paths, a separate
interest grade, candidate exclusions, sampling, sorting, and evaluation before
candidate retrieval and paid calls. Value grades are 0–3 and interest grades 0–2.
Sufficient evidence plus value >=2 and a valid path qualifies a paper. Qualified
papers sort by interest, value, then canonical-ID-derived stable ID. Known lower
value papers follow as not recommended. Insufficient or inconsistent evidence
abstains and is unranked; it is never silently assigned a zero. Decisions returns
typed probabilities rather than text explanations; frozen modal categories use
the lower category on a tie, and evidence ties abstain.

Both arms see the same original title/abstract and criterion. The augmented arm
also sees a bounded LLM JSON record: three paraphrased study facts with abstract
sentence pointers, separately from a proposed computational use, prerequisites
and unknowns. That stage cannot assign scores. Proposed uses are not reported
findings. Ordinary LLM calls use `gpt-6-luna` through Responses and a strict JSON
schema; scoring uses the official Decisions endpoint and the same model.

Sampling uses a deterministic private protocol seed, no Jina score threshold,
equal uniform/computational/method/experimental draws, and source round-robin.
Shortfalls and all source, sampling-path and direction indices stay in encrypted
results. The 29 prior papers, versions and near titles are excluded. Only full
abstracts qualify. The predeclared size sequence is 24,20,16,12; the first size
whose conservative input/output reservation fits the remaining original budget
is frozen before any paid call. Sampling metadata records fallback draws.

The original cumulative USD1 ledger is preserved, including USD0.001743 across
all earlier rounds. One new fixed comparison slot is allowed. All 3N calls are
reserved first; a confirmed compare-and-swap receipt precedes every request.
Transport, schema and unknown-usage failures stop without retries. Unknown
usage holds its full reservation; definitely unsent calls are released. Usage
reports include cache/reasoning counters, price version, currency, price-derived
estimated spend and reservation. They do not claim invoice verification. Caching
is disabled explicitly; reasoning is part of output, not an extra charge.

Before running, set the **repository Actions variable** `SHADOW_PROTOCOL_V2`
to the complete privately delivered JSON file. Do not place it in a public file,
workflow input or production `CUSTOM_CONFIG`. No new stored Secret is needed.
The reusable workflow takes the variable as an ephemeral secret parameter so
the configuration is masked in step metadata. Existing OpenAI/Zotero Secrets
and actual production `CUSTOM_CONFIG` provide credentials and the real baseline.
Zotero content stays in runner memory and never goes to OpenAI or artifacts.

Run the existing **Test** workflow on `experiment/openai-shadow-20261029`, choosing
**compare**; leave `collection_name_sha256` empty. If the default-branch UI has
not refreshed the choice, the equivalent owner command is:

```sh
gh workflow run test.yml --repo wankiwi/zot2dailypaper --ref experiment/openai-shadow-20261029 -f mode=compare
```

One manual run performs offline guards and source/baseline/price/budget preflight,
then paid requests only when all gates pass. Do not rerun a failed/partial paid
run: inspect cost receipts first. The price snapshot must be verified on the UTC
run date. Result artifact `compare-sealed-RUN_ID` contains only encrypted results
and a safe status/cost summary. Sanitized model responses and precise validation
diagnostics are retained inside the encrypted payload, including failed LLM
outputs, to permit offline diagnosis. Public fields and logs contain no raw
abstracts, criteria, scores, rankings, mapping or private configuration.

Decrypt locally with `decrypt.py --encrypted ... --private-key ... --protocol ...
--output ...`. The existing recipient private key stays local. The blind CSV has
only shuffled IDs, titles, links and a neutral abstract excerpt of at most 25
words. It contains no algorithm names, scores or proposed uses. Keep the hidden
paired JSON private until human labels are returned. Evaluate the frozen P@10
(labels >=2) and NDCG@10 (gain 2^label−1), coverage and abstention; if fewer than
10 ranked judged items exist, report P@10/NDCG@10 as unavailable. This is a small
pilot and does not authorize production deployment or prompt tuning after labels.

Official interface and current price references:
[Decisions](https://developers.openai.com/api/docs/guides/decisions),
[Responses structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[pricing](https://developers.openai.com/api/docs/pricing).

## One controlled format recovery

Run `41003868876` settled one failed extraction at USD0.0004857. Its original
response and precise validation error were not retained, so the exact validation
failure cannot be reconstructed. Encoding the existing length, null-fact and
sentence-index rules in the generation schema and retaining encrypted diagnostics
does not establish that the original underlying failure is known or fixed.

The single named recovery is `format-schema-recovery-20261030-v1`. It preserves
the original failed ledger under schema5 and includes cumulative USD0.0022287
before any recovery request. It allows at most 60 new requests within the original
USD1 cap; no automatic repeated recovery or unknown-usage retry is permitted.

Set the existing experiment-only `SHADOW_PROTOCOL_V2` variable to the complete
private recovery input, which wraps the unchanged protocol and the fixed public
paper metadata/hashes. It contains no baseline scores or blind mapping. Choose
**recover** in Test on the same experimental branch. The normal guarded run first
refetches only the same 20 DOI records from their original Crossref/bioRxiv sources.
Every title, DOI, abstract byte count and SHA256 must match before any cost claim.
Missing metadata or drift stops without substituting papers or shrinking the set.
The original output did not retain complete abstracts, so this exact-hash refetch
is necessary. No fresh Zotero retrieval or Jina recomputation occurs in recovery.

The artifact is `recover-sealed-RUN_ID`. After local decryption, pass the original
failed run's privately retained paired JSON to `decrypt.py --original-results ...`.
The decoder verifies the fixed candidates, original Jina score/order hash and
original blind-order hash, then restores their exact pairing. The original baseline
scores, source/direction metadata and blind mapping remain local throughout the
job. Only the resulting neutral CSV is a human evaluation deliverable.
