# Isolated full-pool Jina evaluation (review only)

This branch-local experiment captures an immutable candidate snapshot and compares
A (the current multi-interest profile), B (only title+abstract input for candidates
and references), and C (the previously frozen independent-method/phenomenon lanes
plus B). C descriptions, lane count, RRF weights, depth and tie handling are fixed
before new labels. No model migration, paid inference, email, state write or
production cache sharing is performed.

## Complete inputs and rankings

The production checkout is pinned to source commit in `source_pin.json`. The
ranking config, actual post-filter Zotero text, recovered candidate text, public
model revision/runtime/artifact hashes and read-only history all enter the input
manifest. A must reproduce every score, direction, stable full position and quota
selection against the unchanged original local scorer; checking only top10 is
insufficient. The actual Zotero records and reference vectors never leave the
private job directory. Reference assignment changes between A and B are reported
as aggregate transitions and hashes, without reference identifiers or text.

Two pools are explicit. `production_full_pool_audit` preserves every captured
production candidate and full A replay, plus B/C audit records. Separately,
`validation_common_full_pool` excludes the frozen exposed-family list, uncertain
identity/date/version records and the protocol's information-ineligible records
**before** any validation scoring or quota/random selection. Its true heads are
heads of the unexposed eligible pool; they are never called unfiltered production
heads. Eligibility uses metadata only, with no labels or score-based refilling.
Every exclusion reason, count and membership hash is retained. Missing abstracts
and their original production ranks remain in the production audit; restricting
validation to complete information is an explicit scope limitation, not a claim
of improvement over all production inputs.

A/B/C are saved on the exact same complete qualified validation pool: every score,
matched profile direction, support, stable model rank, quota position, random
position and exclusion disposition is retained. C ranking scores and each lane's
contribution are explicit; its profile eligibility still uses B's production
minimum-score gate. C's direction fields refer to B, not a fabricated new profile.
The complete expanded union, when successfully retrieved, has its own A/B/C
controls and separate blind plan. Common-pool reranking and source expansion are
never combined into one effect estimate.

## Dates, exposed families and source failures

The private protocol fixes a date **policy**, not a date that expires during
approval. At job start, before requests or scores, freeze the last completed UTC
day's end. All source queries use that immutable cutoff and their unchanged
production lookbacks. Runs earlier than the protocol's minimum new cutoff stop.
Do not crop already computed rankings to a one-day subset. Version metadata is
retained: a later bioRxiv/Research Square version cannot be assigned an original
date by parsing its DOI. Unknown original dates and uncertain title families are
quarantined. Every formerly exposed family, including quarantined drafts and all
versions linked by known identifiers, is excluded from independent labeling.
Full production ranks remain available. Qualification creates a separately named
validation population before ranking; it is not post-ranking replacement of an
old production head. All arms use exactly the same qualified rows.

Source diagnostics distinguish complete empty, successful, failed and partial
retrieval. They store source/scope/phase, safe host/path, page offsets, query
length/hash, status, attempts, bounded retry history, timing and conversion-error
counts. No headers, API keys, response bodies or full private query strings are
retained. ArXiv instrumentation calls the original transport: 403/429 are not
retried, 5xx retry limits and endpoint remain unchanged, redirects are not followed.
A common-source failure/partial result stops before encoding and skips expansion.
Other SDKs' hidden retries/coverage remain explicitly unknown when not observable.
Diagnostic event truncation is counted. Swallowed HTTP failures cannot become a
valid empty result. Events are scoped to this collection; unrelated prior SDK
failures are excluded. A later successful different page/endpoint cannot clear a
failure. Same-request transient recovery is recognized, and the pinned journal
adapter's own per-journal/catalog completion contract explicitly certifies its
existing allowed fallbacks. Unrecovered or ambiguous failures remain failed or
partial. The observer adds no retry or alternate fetch route.

## Blind burden and metrics

The mandatory pool is the union of A/B/C's actual **model top20 and quota-main
top20**; top10 is a prefix. Actual random-exploration selections and six fixed-seed
source-stratified draws are included with private selection reasons. No handpicked
disagreement examples or relevance labels affect sampling. The theoretical bound
is 141 = 6*20+3*5+6; if model/main heads coincide it is at most 81. Overlap normally
reduces the count, but no actual count is claimed before capture. Report the real
count first. Stage1 is the union of all six validation model/main top10 lists
(maximum60, or30 when the two views coincide); stage2 is the fixed complement in
the full blind pool. Both stages keep their original blind IDs and full-pool
shuffle order, in batches of five. Stage2 never depends on stage1 judgments.
Automatic export writes each stage plus its five-paper CSV batches, without
algorithm, rank, score or sampling hints. Expanded-source labeling is a separate
optional batch with the same bound, not automatically combined with the primary
burden. All heads are true heads of the qualified validation population; exclusions were
applied before scoring. Any remaining information gap blocks the affected metrics;
never silently refill or truncate the mandatory head union.

Blind files show the same captured title, original abstract, source/link and
missingness/provenance to the reader. They hide algorithm, score, rank, selection
reason and generated recommendation explanation. Unjudged and information-
insufficient records remain unknown, never label0. Compute P@10/@20 and DCG only
when every prefix member is judged. Model, quota-main and random yield are separate
views. Stage1 reports only fully judged top10 metrics; top20 waits for complete
prefix judgments. Pooled NDCG explicitly uses only judged members of the comparison pool;
complete-pool NDCG is unknown/bounded without a complete IDCG. No full-pool recall
claim follows from partial labels. Must-read results are N/A if no label3 exists.

## Future execution, after separate approval

Proposed branch: `experiment/jina-timeout-diagnostics-20261103`. The manual-only Test workflow
has safe default `mode=tests` (synthetic controls, no private Secrets). A separately
authorized `mode=jina_fullpool` may use one newly approved transient **data** Secret
`JINA_FULLPOOL_INPUT` plus existing Zotero/OpenReview/OpenAlex credentials. This is
not permission to create it or dispatch now. The old `JINA_OFFLINE_INPUT` is not
read, changed or deleted by this proposal. Private input has no credentials and
contains frozen rules, ranking config, source overrides and exposed-family IDs.
Keep it local until separately approved.

The job has read-only repository permissions, 45 minutes maximum, a fresh pinned
checkout and fresh model/vector directories. No Actions cache, production state
write or credential persistence is used. Only an RSA-OAEP-SHA256/AES-256-GCM sealed
result is uploaded, retained one day. Public console has only status/paid_calls=0.
The existing recipient private key stays local; it is absent from git and artifacts.
Raw Zotero records, reference vectors and private runtime directories are deleted
on normal/failure completion; ephemeral runner teardown covers cancellation.
Read-only state is used only for identical filtering/pending quota reservation.
No new paid model calls are permitted; earlier experimental spend is not reset.

## Offline controls

From the installed project environment, with BASELINE_ROOT pointing at the source
checkout:

```
BASELINE_ROOT=/path/to/baseline python experiments/jina_offline/test_controls.py
```

The suite uses synthetic text/vectors and mocked HTTP only. Passing it establishes
implementation behavior, not relevance gains or current source availability.
Actual capture/effectiveness remains unverified until a separately authorized run
and independent human labels are available.

From an already decrypted captured result, staged export needs neither Zotero,
embeddings nor API access:

```
python experiments/jina_offline/offline_eval.py --export-evaluation result.private.json --out private-blind
```

The existing recipient key and encrypted one-day artifact flow are unchanged.
This export is local/private; it is not an additional GitHub run or data upload.

## Local timeout-diagnostics revision (not published or dispatched)

Run 41556801480 reached the 45-minute job limit in the quiet capture step,
confirmed by the GitHub UI; decoded logs alone only showed cancellation.
It produced no artifact; its source/model phase and whether a final local file
had briefly existed are unobservable. No scores or baseline completion are
reconstructed from that failure.

The local revision preserves the 45-minute job ceiling. Three checkouts and uv
setup each have a 1-minute cap, dependency install has 4, capture 32, encrypted
upload 3 and configuration cleanup 1 (44 total step-minutes, leaving a minute for
ordinary runner overhead). This is an operational deadline, not a candidate,
source, reference or ranking truncation. If dependency installation exceeds its
4-minute limit, capture has not started and no encrypted seed exists, so there
may still be no artifact. Other setup failures before capture have the same
limitation; their public step failures remain visible.

A valid encrypted seed precedes private work. Fixed stage transitions, encoder
entry/exit metadata and30-second heartbeats atomically replace the encrypted
checkpoint. Full source diagnostics and aggregate counts stay encrypted. Original
stdout/stderr remain discarded. Only a dedicated inherited FD3 emits a small,
fixed-schema JSON phase/event/elapsed/source heartbeat; it never tees raw output,
queries, corpus/candidate counts, profile text, identifiers or ranking scores.
The encoder wrapper forwards the original call exactly once without warming,
batching differently or introducing extra inference texts.

The shell uses `exec` so Python replaces bash as the step entry process. GitHub
first sends cancellation SIGINT to that entry process, followed by SIGTERM and
then a process-tree kill if it has not exited (see the official
[cancellation reference](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation)).
The dedicated cancellation exception derives from `BaseException`, so ordinary
`except Exception` handlers in retrievers or SDKs cannot swallow it. Main handles
it separately from source failures. SIGINT/SIGTERM handling preserves a cancelled
checkpoint and exits 130; even a
SIGKILL leaves the last fully written checkpoint because writing uses atomic
replacement. Capture timeout leaves room for artifact upload and cleanup. Upload
uses `always()` rather than excluding cancellation, retaining only the encrypted
file for the existing one-day period. Incomplete checkpoints explicitly set
`baseline_verified=false` and `blind_ready=false`; they are diagnostics, not
completed A/B/C rankings. Normal completed results keep their genuine validation
fields and receive separate timing metadata.

Offline tests simulate blocked work, SIGINT/SIGTERM through nested SDK and source
collector error handlers, the Actions-style bash entry, SIGKILL, interrupted writes, safe
FD3 versus raw stdout/stderr, heartbeat shutdown and workflow time budgets. They
do not establish live performance or guarantee recovery after host loss, forced
workflow termination or artifact-service failure. No software change can recover
the missing artifact from the prior run. The scoring code, C protocol, source/date
policy, sample seed and private input remain unchanged. Another actual execution
requires separate approval; this revision makes no publish or dispatch request.
If approved, the next run is a diagnostics run: it may produce complete A/B/C
results, but the 32-minute capture budget is a retention safeguard, not a speed
fix or a promise to deliver a blind-evaluation pool.
