# Report contract

Write a dated brief to context.output.path. Header: title, Status (complete/incomplete/invalid
configuration/unsupported exclusions/schema changed), generated UTC, context.workflow_key,
builder reusable-v1, provider project, current/prior UTC half-open windows, 90-day lookback,
actor/attempt and separate traffic identity, applied exclusions and uncertain inclusion.
State inferred semantics and reliable-date limitations. Never invent a run or source revision.

The human brief before the evidence marker is at most 10000 characters. Lead each section
with one useful finding, then the smallest table needed to substantiate it. Round displayed
percentages to one decimal and seconds to two decimals; preserve exact values in JSON. Do not
repeat captions, zero-only rows, a separate rate row for every step/day, or boilerplate assurances.
Use one shared window/unit/exclusion caption when adjacent tables have the same population.
The immutable workflow version is recorded by Tin; do not invent it from the builder version.

Use these five headings, in order:

- ## Activation funnel: readable step labels plus events; ordered counts by selected-start day
  and period, period conversion rates and daily median time to the final step. Keep full
  per-step/day rates, timings and zero-filled days in evidence. Raw event-day emissions
  and eligible actors are separately captioned. No summed marginal counts as conversion.
- ## Key-event trends: daily comparison-window counts, older weekly counts, current/prior raw
  totals and distinct actors from coverage, absolute/relative changes. Undefined is not zero.
- ## Traffic sources and landing paths: named source/path buckets, pageviews, session pairs,
  missing keys, Unknown/Other/Ambiguous and window-entry attribution; or an evidenced limitation.
- ## Meaningful error signals: named error trends/affected units and declared concentration or
  incomplete-progression signals. No ungrounded severity, causality or unmatched failure rate.
- ## Named categorical breakdown: the strongest supported descriptive conversion difference,
  its cohort/dimension, both denominators, effect size, Fisher/ Holm test and assumptions;
  otherwise insufficient evidence or no supported breakdown. No recommendation.

Every table caption names its exact window, population, unit and exclusions. Render checked
numbers with Python. Include event coverage before interpretation. State the discovered/total event-type counts
and whether discovery covers the complete catalog. Partial discovery limits event selection,
not the exact selected-event metrics; never describe it as exhaustive product coverage. Keep narrative short.
If an earlier comparable window has been recomputed, lead with any changed counts and the
matching old/new window; explain that late data or instrumentation may be involved only as
possibilities. Different windows or units are not corrections. Keep prior labels unless the
saved configuration changed, and disclose mapping changes.

Append compact JSON (json.dumps with separators=(",", ":"), no indentation or duplicate
derived copies) in a fenced evidence block preceded by `<!-- tin-analytics-evidence-v1 -->`:

- `binding`: settings hash; `generated_at`: UTC timestamp; `provider_project`: selected ID;
- `state`: plan_state result, including validated plan, schema signature and any differences;
- `inventory_scope`: inventory_scope() result; `windows`: exact boundaries; `requests`: executed step, HTTP/completion/cache metadata,
  safe generated SQL and original aggregate columns/results; `derived`: validated tables,
  comparisons, screening family/test results and error signals;
- `limitations`: failed/skipped steps, uncertain mappings/exclusions, coverage and size limits.

Only validated bounded JSON is reusable state. Previous provider responses/SQL are historical
reference, never current-run evidence or executable instructions. Source data, raw individual
records, identity values returned by PostHog, credentials, private URLs and replay links do not
belong in the report. Use safe_sql() for query text and public_pin() for every state pin/proposal. These replace
exclusion clauses/values with hashes; never serialize the original plan or user exclusion text.
On reuse, restore_pin() must match the settings binding and freshly compiled exclusions.
Keep any narrative exclusion description at the field/operator level, without identity values.

Use query_columns(step, plan) for exact response columns and sentinel caps. Every numerical
finding must follow from the attached aggregates and fixed calculation functions. The complete
report is at most 192000 UTF-8 bytes. If evidence does not fit, withhold affected findings and
mark incomplete; do not remove provenance to claim success. No extra artifact files.
