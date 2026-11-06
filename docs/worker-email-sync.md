# Sync recommendations before sending Worker save links

This integration uses the existing successful Worker v8 and D1 `papers` table.
It does not deploy a Worker, migrate D1, change the Zotero collection, or change
the existing daily schedule, quotas, LLM budget, delivery history, or Site. Old
email links remain available through the existing Site.

When enabled, the normal recommendation run saves its pending history, upserts
only this email batch's public citation metadata into D1, then reads back the
exact IDs, identities and payloads. Only after every batch verifies does it send
the email using `https://zot2dailypaper-save.wankaiweii.workers.dev` save links.
The identifier remains SHA256 of the same canonical identity used by the email.
Opening a link is a GET preview; the owner still signs in with Zotero and
explicitly confirms the save. The pipeline does not write to Zotero.

On an HTTP, timeout, malformed response, or readback failure, it does not send
this email or mark it delivered. Pending history remains available for the next
normal run. Repeating an upsert changes only different public citation payloads,
so a partial failure can be retried without duplicate metadata rows. There is
no automatic HTTP retry or redirect. Missing/invalid setup stops before
retrieval and budgeted enrichment.

The sync uses fixed SQL over `papers` only. It never reads credential, session,
library-index, or save tables. It includes title, authors, abstract, public URL,
DOI, journal and date; no full text, AI summary, scores, private Zotero library
contents, email address, or secrets. Text is bounded to v8's UTF-16 field limits
and 32 KiB per record; oversized or unsafe URLs stop delivery. Long display
text is truncated only in the saved citation payload, not the recommendation
history or email. The entire batch is validated before the first D1 write.

One supported degradation keeps large-collaboration papers deliverable without
silently losing creators: a valid citation with more than the Worker's 100-author
limit remains in the email and saved history, with every author preserved in
history. Only that paper's Worker save link is omitted, with an explicit notice
and its original Article link for full-metadata import. It is not written to D1
as a truncated citation. All remaining citations must still pass sync/readback
before email. Invalid URLs, identities, author values or other malformed fields
continue to block the entire batch. Both normal delivery and saved-batch replay
use this rule. No Worker deployment or data migration is needed.

At the current 50-paper quota this is at most six D1 REST requests per run
(three writes and three indexed public readbacks). The sync admits at most
1,000 papers, uses batches of 20/80 bound write parameters, caps each response
at 1 MiB, and stops admitting requests after a 120-second deadline. Each network
read also has a 30-second timeout; a stalled request may complete its timeout
after the deadline. Existing Free storage/query limits still apply. No local
test or these request bounds prove production CPU/quota compliance. Storage is
not pruned, preserving previously emailed links; Free-limit errors hold email
pending rather than creating paid resources.

## Required user setup before production enablement

Do these steps yourself in the secure service UIs. Never put a token in chat,
repository files, CUSTOM_CONFIG, Actions variables, logs, or PR text.

1. Create a **dedicated** Cloudflare API token with **Account → D1 → Edit**,
   restricted to account `7d50defa6cf2776ad44d6da8c9669607`. Grant no Workers,
   DNS, R2, billing, user, or additional account permissions. Use the shortest
   practical expiration and rotate it through the same secure UI.
   [Cloudflare's D1 token instructions](https://developers.cloudflare.com/d1/tutorials/import-to-d1-with-rest-api/)
   and [D1 write permission requirement](https://developers.cloudflare.com/d1/platform/release-notes/#2025-05-02).
   This native permission is an **account-level D1 administrative permission**;
   it is not limited to the `papers` table by this application. Do not assume
   the hardcoded database URL restricts what a leaked token could access.
2. In `wankiwi/zot2dailypaper` → Settings → Secrets and variables → Actions,
   add the token as the repository **secret** `ZOTERO_WORKER_D1_TOKEN`.
   Do not reuse/extract the Worker OAuth/encryption secrets, Zotero API key,
   Cloudflare login session, or a previously configured token from another
   destination.
3. After checking that the account remains Free, set the repository **variable**
   `ZOTERO_WORKER_SYNC` to `free`. This is the explicit enablement and Free-plan
   acknowledgment. Absence or `disabled` preserves the old configured email
   origin. Unsupported values fail configuration. No additional token belongs
   in an email URL.

The target is the already verified database `zot2dailypaper-save-db`,
`7b5f060d-6375-4159-a548-89cc9ba91a41`. No new hostname or callback registration
is required; the existing callback remains
`https://zot2dailypaper-save.wankaiweii.workers.dev/auth/zotero/callback`.

Keep the PR in draft until secure setup and its enablement are confirmed and
exact-head CI succeeds. Then review/merge without manual production dispatch.
The next existing scheduled run is the live integration check: verify its
sync-success count before SMTP and the normal email's new confirmation links.
That run is not initiated by code validation. CI uses mocked transports/local
SQLite and has no production sync token, mail, or Zotero write credentials.

## Rollback

Set `ZOTERO_WORKER_SYNC` back to `disabled` through the approved configuration
process; the previous `email.zotero_action_origin` from CUSTOM_CONFIG is retained.
Do not promote the retained temporary diagnostic Worker v10 or deploy anything
to fix an email configuration issue. Public citation rows remain in D1 so
previously issued Worker links continue to resolve. The existing Site continues
serving old links.
