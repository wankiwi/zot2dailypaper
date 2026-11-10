# bioRxiv/medRxiv transport recovery

Date-window requests allow a 10-second connection timeout and a 60-second read timeout, with the shared bounded GET retries. A category-filtered request that still fails with a timeout or connection error triggers one complete, unfiltered date-window retrieval from the same official API. The configured categories are then applied locally, and DOI versions are deduplicated as usual. Partial category results are discarded, not merged into a purported complete result.

Recovery paginates from cursor zero under the same `max_pages` limit. Invalid responses, incomplete pagination, and exhausted page limits remain errors. HTTP status failures (including access/rate-limit refusals), TLS failures, and failures of an already-unfiltered request do not trigger this recovery path. There is no cache or RSS fallback pretending to cover the date window.

If recovery also fails, the existing executor continues other sources, saves accepted deliveries, and reports the source failure at the end. This can mean an email was accepted by SMTP even though the workflow is red. Do not blindly rerun delivery to fix a retrieval outage. The recovery is resilience against upstream transport latency, not a guarantee that the upstream service is available.
