# One isolated local Jina capture

This experimental branch changes only this generic driver and its manual Test
workflow. It does not modify the production baseline, daily schedule, email,
delivery history, variables or shared embedding cache.

The baseline is checked out separately at immutable source commit
339d0185fff0f67e7726eeb162462652e8669a38. The history branch is read-only. Existing
read credentials supply Zotero and configured public-paper sources. A transient
**data Secret**, JINA_OFFLINE_INPUT, is required for the private frozen protocol,
credential-free runtime overrides, ranking projection and previous-study
exclusions. It is not an API credential. It must be explicitly authorized,
created by the operator before the single run and removed by that operator after
retrieval. It is not provisioned or read back by this workflow. No private
protocol values are committed here or passed through public dispatch inputs.

Only workflow_dispatch mode=jina_offline is accepted, only on the named experiment
branch. The job has contents:read, a distinct concurrency group, a 45-minute
timeout and no production cache restore/save. It never invokes Executor.run,
_recommend, delivery, worker sync, remote embeddings or any LLM.

All process output is suppressed. The sole uploaded path is an authenticated
RSA-OAEP-SHA256 + AES-256-GCM envelope addressed to the supplied public recipient
key, using the previously tested envelope format. The private key remains with
the operator. Public artifacts therefore contain ciphertext, not plaintext
personalized scores, mappings or new abstracts.

The corpus is fetched and used only inside the ephemeral runner. The export guard
rejects raw corpus/config/history/vector/path keys. Only hashes, aggregate
support, public candidate metadata and comparison outputs are sealed. Temporary
input JSON, local private vector/model caches and runtime config are removed;
runner memory disappears with the job. Ciphertext artifact retention is one day.
Do not assume this deletes GitHub's own retained logs or an operator's separately
saved Library deliverable. Logs contain no user text or rankings.

A new real A baseline is captured and checked with all scores, directions,
ordering and selection membership; lost historical run inputs are not recreated.
A/B share identical public candidate and actual Zotero information. B only adds
titles. The private frozen protocol controls separate C_fixed/C_expanded results,
old-family exclusions and blind-pool selection. Missing or inconsistent inputs
seal a blocked result; a green workflow badge alone is not evidence of completed
evaluation. Source coverage can remain incomplete and must be reported.

Expanded arXiv retrieval writes the effective preprint_interests.arxiv.categories
as well as the legacy source field, checks the actual categories_for result and
requires categories outside the original scope. The encrypted audit records the
base, effective and new query categories. Tests use the public production profile
and the real retriever query builder with mocked transport.

The private blind CSV includes original title, captured original abstract,
neutral source/venue/link, abstract provenance and explicit available/missing/
incomplete markers. It never includes scores, algorithm identity, sampling
reasons, directions or generated recommendation explanations.

No OpenAI cost or budget-ledger mutation is possible: no paid API credential is
passed, and all scoring uses the pinned local Jina model. Standard hosted runner
minutes and public metadata rate quotas remain subject to the existing account.

After the separately approved run, retrieve the ciphertext through normal
authorized artifact access, move it through private Library if necessary, decrypt
locally and deliver only an algorithm-blinded table to the user. Do not export
the Zotero library. Delete the transient data Secret and ciphertext artifact once
verified retrieval completes. Nothing is pushed, dispatched or configured merely
by reviewing this package.

Generic tests use synthetic inputs only. They do not validate personalization or
claim real baseline reproduction.
