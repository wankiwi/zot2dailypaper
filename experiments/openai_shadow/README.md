# Generic isolated OpenAI experiment runner

This test branch replaces only the existing manual `Test` workflow on this branch. The default branch, daily workflow, provider configuration, delivery history and email code are unchanged. There are no push, PR, schedule or daily-delivery triggers here.

The published runner contains no real paper fixture, research rubric, preference labels, private profile or service credential. Synthetic tests run before the sole step that receives the existing Actions `OPENAI_API_KEY`. The secret stays in the job and is only sent to fixed official OpenAI API endpoints. No secret is downloaded or created.

A real model call is blocked until separately approved public files exist: `data_approval.public.json`, `papers.public.json`, `rubric.public.json`, `source_review.public.json`, `pricing.public.json`. Publishing these requires explicit public-disclosure authorization. Do not publish labels, Zotero contents or private notes. The parent retains labels locally for evaluation. Input papers permit only anonymous ID, public title and original public abstract; all five must be complete.

With those gates satisfied, the job first atomically claims the entire USD 1 allowance in this test branch's `state/budget.json`. A second run, concurrent creator or rerun cannot receive a fresh allowance. Actual per-request accounting uses the client ledger, verified prices, token bounds and no retries. Unknown usage retains its reservation. The optional LLM comparator shares the same allowance and additionally requires verified schema support.

Artifacts in a public repository must not be treated as private. This runner uploads only sanitized accounting and outputs derived from inputs that were explicitly authorized for public disclosure. It never uploads labels, private profiles, API credentials, account metadata or raw error bodies. Retention is one day. If public disclosure of a required input is not approved, stop before model calls; do not add a secret or an alternate data-injection path.

The registered default-branch workflow file already supports `workflow_dispatch`; the supported manual entry is `Test` with this test branch selected and `mode=tests`. No new default-branch registration or security-setting change is required. This repository has no connector dispatch tool in the current environment; any denied dispatch must be reported rather than worked around.
