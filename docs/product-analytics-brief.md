# Product analytics brief

`product.analytics_brief` reads one connected PostHog project and writes a standing
brief: activation, key-event trends, traffic, error signals and one useful breakdown.
It reports findings and their evidence, without recommendations or changes to the
customer's product. It does not inspect session replays or fix instrumentation.

## Set it up once

Connect `custom.api.posthog` in Integrations using the correct regional API origin,
GET/POST and a personal key restricted to the intended PostHog project with
`query:read` and `event_definition:read`. POST is needed for read-only queries;
Tin's `http.write` transport capability does not grant provider mutation authority.
The key stays in Tin's gateway, never in workflow files, inputs or the sandbox.

Save the workflow with the PostHog project number, reporting window and any known
funnel or exclusions. Leave the funnel empty to have the procedure propose one
from available event evidence and project context. Discovery reads up to 200 event types
ranked by recent volume and discloses the total catalog size. Selected metrics query their
complete event/window populations; events outside the discovery list are not assumed absent.
The report states what it chose
and why; edit the saved inputs to correct it. Missing semantics are a limitation,
not permission to invent an activation event or treat an identifier as a human.

Choose manual, daily or weekly execution through the ordinary saved-workflow form.
Existing schedule authorization and billing rules apply. Results arrive in Tin's
Files and Activity, with a separate `reports/analytics/<run_id>.md` for each run.
This package does not add email or Slack delivery.

Use a separate saved brief for a separate PostHog project. Website visitors and
product accounts are different populations; this workflow does not join them.
A project without pageviews can still produce a useful product brief, with traffic
explicitly unavailable. When pageviews exist, the procedure checks PostHog's documented web
properties through the same bounded queries. A missing session key on a server-side product
event can prevent a same-session funnel without preventing independent website traffic analysis.

## What is checked

The bounded procedure chooses and explains the analysis. Declared Python/SQL
resources own query construction, ordered counts, rates and statistical checks.
Queries return aggregates, with raw identities kept inside PostHog. Source data
and previous reports are evidence, never instructions. Read-only permissions come from
the provider key: the generic gateway does not inspect SQL or enforce these analytic
semantics. The procedure instructions are reviewed behavior, not a new SQL security sandbox.

Every table identifies its window, population, exclusions and unit. Queries use
explicit UTC boundaries. Missing keys, late instrumentation, zero denominators,
small samples and incomplete provider responses stay visible. First observed data
does not establish when an event became reliable or what its firing site means.
A failed required query makes the brief incomplete; an honest diagnostic is not
a passing ordinary qualification case.

A screened breakdown is descriptive evidence, not proof of causation. The report
names the test and its multiple-comparison correction. No supported split is a
valid outcome; the procedure must not keep searching until it finds significance.

## Qualification and publication

The package and `workflow_evals/product.analytics_brief/qualification.json` use the
same creator/qualifier contract as external contributions. Static checks establish
shape. Offline tests exercise calculations, query construction and bad responses.
They do not establish provider compatibility or model quality.

Live qualification separately checks the exact generated SQL against controlled
provider cases and reviews real reports against retained query results. Package
and case digests identify the evaluated version. A private on-demand copy is a
different manifest from the scheduled public package; do not describe its run as
exact public-package acceptance. Public schedule execution needs its own test
deployment or post-merge acceptance.

Model cost is unmeasured until priced runs exist for that package and case. A
configured ceiling is not an expected price. The existing usage receipts and
ledger charge verified actual model usage; connected PostHog spending is separate
and is not asserted to be free. Event/schema complexity, discovery, response sizes
and interpretation affect agent cost.

Maintainers review source, useful results, safety and measured costs before
publication. Explicit `PUBLIC_WORKFLOWS` registration is reviewed in the PR;
deployment/catalog sync is the publication step. No creator or evaluator publishes
automatically, and existing private runs and saved versions stay pinned.
