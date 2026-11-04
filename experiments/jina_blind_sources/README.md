# Public-source capture draft

This branch job retrieves public paper metadata only. It does not read Zotero,
load embeddings, use an LLM, generate summaries, send email, update production
state, restore/save production caches, or change main. Personalized scoring
uses the existing private desktop-exported reference snapshot locally afterward.

The proposed branch is `experiment/jina-blind-source-20261104`. The existing
manual `Test` workflow has two branch-local modes: `tests` (default) and
`public_sources`. Nothing is scheduled. Publishing or running requires approval.

The data Secret `JINA_BLIND_SOURCE_INPUT` is source settings only: enabled
public sources, journal lists, preprint category/query scopes, common query
date policy and independently recorded expanded arXiv categories. It contains
no API credential, ranking profile, Zotero content or user labels. These source
settings can reveal research interests and require explicit consent before
upload. Existing OpenReview login and OpenAlex API credentials are passed only
to their original public-source clients inside the capture step. They are not
read, copied, replaced, printed or exported by the experiment. No Zotero,
OpenAI, SMTP or production CUSTOM_CONFIG input is passed to the job.

Source code blobs are pinned inside the runner to the reviewed main snapshot.
The SDKs, source scope, pagination and refusal behavior remain unchanged.
HTTP failure diagnostics retain status, domain, safe endpoint shape, query
length/hash, operation and retry/time measurements. They omit query text,
response bodies and credentials. A failed/partial source is never called a
valid empty source. No blind sheet is released from this capture alone.

The query cutoff is the most recently completed UTC day, frozen before calls.
An explicit minimum cutoff prevents an old date being silently reused if
approval is delayed. All sources share that cutoff. The local validation layer
additionally excludes papers predating its frozen new-cohort floor, all old
exposure families, uncertain version/date matches and incomplete abstracts
before scoring. Source expansion remains a separately measured pool.

The job reads the existing public paper-state branch without writing it and
retains identity fields only. Pending/uncertain records are conservatively
excluded locally. The exposure snapshot must be checked again before blind
release; this initial history read does not guarantee papers stay unseen.

The capture step has 32 minutes within a 45-minute job. It writes an encrypted
checkpoint before and after every source. Interrupt handlers persist the latest
checkpoint before SDK cleanup. Artifacts contain only RSA-OAEP-SHA256/AES-256-GCM
ciphertext, using the existing public recipient key. The private key remains
local. Retention is one day. Ciphertext download/decryption uses supported
private file delivery. The temporary data Secret is deleted and verified after
retrieval by the authorized operator; the workflow itself has no Secrets API
permission. No new service credentials are created.

Dependency installation is limited to four minutes; failure before Python
starts can leave no checkpoint. `always()` is best effort: runner loss, forced
kill or artifact-upload failure can still prevent recovery. Source coverage and
remote duration remain unverified until an authorized real capture completes.
No model API fee is incurred; Actions minutes and existing source quotas apply.

Offline tests:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s experiments/jina_blind_sources -p 'test_*.py'
```

Removing the experimental branch and its temporary data Secret is sufficient
to remove this proposed remote capability; main is never modified.
