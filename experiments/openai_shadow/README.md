# Same-five v2: complete evidence before relevance scoring

v1 completed one Decisions request: 1736 input tokens, zero cache/output, usage-derived cost **USD 0.000173600**. Its original USD 1 reservation, run ID, artifact digest and verified receipt are preserved under `history_v1` in the **same** `state/budget.json`. The parent verified the original ZIP. This reconciliation settles known v1 usage; it does not erase the first reservation or create a new allowance. Remaining before v2: **USD 0.999826400**.

The user explicitly authorized one v2 retest of the same five papers. The deterministic extension ID is `same-five-complete-abstracts-v2`, with at most **one additional model attempt**. A remote compare-and-swap reservation must succeed before the model request. A second attempt, rerun, unknown usage, changed receipt or cap overrun stops spending; there is no automatic retry or arbitrary new claim ID. Both rounds together remain under the original USD 1 cap.

The planned ablation uses the exact v1 rubric with all five complete abstracts. Original paper-fixture and rubric hashes are pinned in `protocol_v2.public.json`; the code does not tune prompts to make development labels perfect. Evidence sufficiency is a separate gate: an unavailable abstract is `insufficient_evidence` with `relevance_score=null`, never a manufactured low-relevance score. **No v2 model call occurs unless all five abstracts are obtained and identity-checked.**

Missing abstracts are retrieved at runtime from the bounded routes in `sources_v2.public.json`: Nature; NCBI PubMed with DOI/PMID checks and a labeled secondary mirror fallback; ACS/ChemRxiv with a labeled exact-DOI ResearchGate fallback. Secondary evidence is never described as primary verification. There are at most six public GETs, 15 seconds each, no blind retries. New abstract text remains only in runner memory/temp files. Public artifacts contain source/provenance, content hashes and sizes, a request hash, actual model scores/distributions and accounting. They contain no new raw abstract text or full raw requests.

## Manual workflow

Open **Actions → Test → Run workflow**, choose branch `experiment/openai-shadow-20261029`, leave `collection_name_sha256` empty.

1. Choose **`mode=tests`** first. This is the default **sources-only preflight** and receives no OpenAI secret. Inspect `source_status.json` in `shadow-v2-source-preflight-RUN_ID`. Both `ready` and `price_ready` must be true. A successful workflow badge alone does not mean sources were complete.
2. If sources and current-day pricing are ready, choose **`mode=llm`** once on the same branch. This refetches/validates evidence, then allows only the explicitly authorized v2 request. If evidence is incomplete, it exits before reserving v2 or calling a model.

The existing `OPENAI_API_KEY` Secret is supplied only to the paid test step, with fixed official endpoint/model. There are no new secrets, encrypted injection, private profiles, Zotero credentials or email credentials. The standard job GitHub token can update only the experimental ledger via the fixed test-branch path. This workflow has no schedule, push or PR trigger, and does not invoke daily delivery, main/provider migration or production budget state.

In the preparation environment, all six runtime routes were actually attempted and blocked at the network tunnel with `403 Forbidden`; the three missing abstracts could not be verified there. The GitHub sources-only mode exists to assess its permitted network environment without risking a paid request. It does not bypass a denied dispatch route: run manually using existing authorized GitHub access. Current-day pricing must be reverified if the execution date changes.

Scores remain actual model outputs; user preference labels stay outside public git and model requests. These five papers are development checks, with no held-out result or real Jina comparison. v1's title-only/abstract-evidence confound cannot support a personalization or deployment conclusion.
