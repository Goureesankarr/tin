# Technical repair from an organic audit

`organic.technical_fix` takes one evidenced technical finding, verifies its current
website and repository sources, and proposes a checked, unmerged GitHub PR. It does
not merge or deploy the repair. The current policy is `html-metadata-v3`; older runs
and saved configurations retain their pinned policy.

## Select a finding

Use `get_technical_fix_source(project_id, audit_run_id)` or the corresponding
`GET /api/projects/{project_id}/technical-fixes/sources/{audit_run_id}` endpoint.
The reader verifies the exact published revision and document digests, then recomputes
technical findings from the saved crawl. Its response includes:

- `findings`: technical findings, each with `source_eligible`, `ineligible_reason`
  and the full affected URL list from the bounded crawl.
- `excluded_findings`: content recommendation identifiers with `content_finding`,
  an explanation and `next_action: content.plan`. These are not repair candidates.
- `repair_availability`: whether any technical finding is source-eligible. An empty
  technical inventory reports `no_technical_findings`; an inventory with only
  unsupported checks, incomplete crawls or over-limit findings reports
  `no_eligible_findings`. Inspect `crawl_status` and `check_coverage`: no findings
  does not establish that unobserved checks passed or the whole site is healthy.

`execution_available` describes whether the workflow is implemented, not whether
this particular audit contains a repair candidate. Source eligibility still requires
live verification, source matching and delivery checks before a PR can be created.

Pass the exact audit run, revision and eligible finding ID to
`preflight_technical_fix`, with the selected repository and confirmation that it
serves the site. Preflight is read-only. A content finding returns `content_finding`
(HTTP 409), an unsupported technical check returns `check_not_supported` (409), and
an ID absent from the verified audit returns `finding_not_found` (404). These cases
stop before repository binding or repair compute. Normal workflow starts use the
same preflight, and execution independently pins and revalidates its selection.

Buyer-answer coverage recommendations mean the site was absent from sampled answer
citations. They do not establish a code defect or missing content. Inspect existing
answers first; `content.plan` can use the audit and matching keyword research if
content work is warranted. The repair preview does not start that workflow.

## Current repair coverage

| Audit check | Repair support |
| --- | --- |
| `metadata.title_missing` | Supported in current and legacy repair policies. |
| `metadata.description_missing` | Supported in `html-metadata-v2` and `html-metadata-v3`. |
| `metadata.title_duplicate`, `metadata.description_duplicate` | Visible for inspection; no automated repair profile. |
| `canonical.broken`, `canonical.redirect` | Visible for inspection; no automated repair profile. |
| `links.broken`, `discovery.possible_orphan` | Visible for inspection; no automated repair profile. |
| `http.redirect`, `http.redirect_chain`, `http.client_error`, `http.server_error` | Visible for inspection; no automated repair profile. |

Supported findings require a completed technical crawl and at most five affected
URLs. Current source profiles are exact static HTML and bounded Hatchling Python-wheel
HTML templates with known literal substitutions. The verifier permits only the selected
title or description change; application logic, dependencies and build configuration
cannot change. General framework builds need separate repair and verification support.

The current policy can repair matched pages while explicitly listing unsupported
pages as untouched. It never claims the whole finding is repaired when coverage is
partial. Already-resolved pages, unsupported sources and overlapping PRs produce a
durable no-change explanation. Verification failures remain failures.

## Verification

Synthetic audit publications cover content-only, technical-only and mixed inventories,
legacy/current audit policies, every registered technical check, source integrity and
HTTP/MCP parity. Disposable-Postgres tests carry real source selection through metadata
verification, mocked PR delivery and immutable publication receipts, including replay.
Gateway fixtures separately cover lost GitHub responses and single-PR recovery.
These are offline tests; positive live acceptance requires an authorized repository
and an actual supported finding.
