# Analytics qualification

The versioned cases in `qualification.json` share the normal package qualification
contract. They are test specifications, not evidence that an agent passed them.
`tests/fixtures/product_analytics/{website,accounts}.json` contains fabricated events,
documented identity/event semantics, and independently specified ordered populations.
The website population is 6 → 3 → 2 → 2; the account population is 6 → 3 → 2.
Duplicates, tied timestamps, later-only actors and cross-attempt/cross-actor events
must not inflate those counts. The website has real synthetic pageviews and named
path/source/device buckets; the account dataset has no pageviews.

Run `uv run pytest tests/test_product_analytics.py` and the package qualification
CLI offline. Tests execute only the fixed reviewed resources, never incoming PR
code with credentials. Statistical fixtures include an independently published
Fisher example, symmetric/empty tables, Holm correction and small-sample rejection.
Plausible but inconsistent provider tables are negative cases.

For an authorized live case, use an isolated PostHog test project with the matching
fixture events and metadata, then substitute its actual project number in an
explicitly versioned case copy. Do not seed a customer's analytics with fixtures.
Name the Tin project, authorized payer and spending ceiling before starting through
the existing evaluator. Retain source/case digests, usage receipts, report, query
outcomes and human rubric judgments. A mocked response or deterministic provider
SQL probe does not establish agent quality or expected model cost.

The `repeat_labels` case supplies the previous report as project context. Negative
provider cases use a controlled response fixture: HTTP 403, or a monotone but wrong
funnel count. These are offline response/quality tests, not permission to alter a
real provider's response. Normal output assertions require `Status: complete`;
headings or successful artifact delivery alone cannot pass.

Provider SQL probes can substitute an inline fabricated `fixture` CTE for `events`
and execute through an authorized read-only connection, without ingestion. Their
sanitized columns/results can be retained as regression fixtures. Query compilation
and numerical provider compatibility are distinct from a full agent report and
from scheduled execution after deployment.

The large_catalog case adds 201 independently named one-row events to the four-event
website fixture. Bounded discovery must disclose 200/205 types without blocking exact
selected-event analysis or asserting absence of omitted events. A response that returns
fewer rows than min(total_event_types, 200) is still an incomplete query and must fail.
Web cases should exercise the documented PostHog defaults even when event descriptions
are empty. Missing product session keys must not suppress independently valid traffic
analysis or become a fabricated product conversion rate.
