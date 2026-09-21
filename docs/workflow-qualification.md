# Create and qualify a workflow

Describe the repeatable job. The creator proposes a package and a few cases; the qualifier
checks its shape and evaluates pinned results. A person reviews the source, cases, quality
and external effects before deciding whether to activate or publish it.

This uses ordinary Tin packages, runs and usage accounting. There is no new execution engine,
automatic publication, or single quality score. Existing packages work without qualification
files; adopt the checks when adding or revising a package.

## Authoring through MCP or the app

1. Call `get_workflow_authoring_guide(project_id)`. Its `creator_files` are the Tin-owned
   `custom.workflow_create` package. Commit them to the project through the existing file
   service, validate, and explicitly activate that revision once. The private-workflow
   allowlist and protected Codex runtime are required, as for other private procedures.
2. Start the creator through the normal workflow start tool or app form. Supply `brief`,
   `workflow_key`, optional `scope` (`private` or `public`) and `constraints`. Ordinary billing
   applies to the creator itself. It chooses deterministic Python, managed-model Python or
   a bounded procedure, and writes `reports/WORKFLOW_CANDIDATE.json`.
3. Call `inspect_workflow_candidate(project_id, run_id)`. It reads the exact run artifact,
   independently validates it, and returns proposed file changes plus a static report.
   Review those changes and commit them with the existing revision-checked file tool.
4. Call `qualify_workflow_package(project_id, path, revision)`. This checks the pinned package
   and cases without running the candidate. Use ordinary pytest for code and mocked-provider
   behavior; use explicitly authorized live cases for real model quality and cost. Review the
   resulting evidence before normal activation or a public contribution.

The creator is shipped in the Python distribution, not selected in `PUBLIC_WORKFLOWS`.
It has no integration bindings or authority to start candidates. Its self-checks are author
claims; the trusted qualifier supplies independent evidence. Authoring and validation run
within the existing enforced agent budget and runtime bounds.

HTTP exposes the same services at `POST /api/projects/{project_id}/workflow-packages/candidate`,
`/qualify` and `/evaluate`. Candidate takes `run_id`; qualification takes `path`, `revision`
and optional `runs: [{"case_id": "ordinary", "run_id": "..."}]`. Every request requires project
membership. The app can start an installed creator through its existing form; there is no new
dashboard qualification editor in this slice.

## The one additional file

Keep the existing manifest and source unchanged. Put cases beside them, outside the runtime
package, at `workflow_evals/<workflow_key>/qualification.json`:

```json
{
  "version": 1,
  "assumptions": ["CSV has integer cents and at most 16000 characters."],
  "effects": ["Writes reports/CSV_SUMMARY.md in this project."],
  "cases": [{
    "id": "ordinary",
    "description": "Two rows with exact arithmetic.",
    "inputs": {"csv_text": "amount_cents\n1250\n2250\n"},
    "expect": {"contains": ["Rows: 2", "Total amount (cents): 3500"]}
  }]
}
```

Use a few concrete inputs: a normal job, a meaningful boundary, and a plausible wrong result
when models or providers are involved. Start small and add cases when a real failure teaches
you something. The case file and package get separate content digests; changing either makes
the report a different qualification. Cases omit `project_id`; Tin binds it.

Optional `cost_drivers` describes what changes paid call counts or token sizes. Optional
`rubric: [{"id": "grounding", "question": "Does each claim follow from the evidence?"}]`
holds independent quality questions. The maintainer proposing Registry registration owns
their review; contributors propose changes. Successful cases require assertions or a rubric.

Assertions support literal `contains` / `excludes` and `expect.json_schema` using the bounded
managed-model output subset: closed objects, required properties, scalar enums and bounded
arrays. No arbitrary Python evaluators, regex or remote schema references run on the server.
`expected_status: "failed"` expresses an expected runtime rejection, not an admission failure.
Code-level pytest tests remain the place for richer invariants and precise exception checks.
The CSV [case file](../workflow_evals/example.csv_summary/qualification.json) is a complete example.

## Offline checks

```bash
uv run tin-lite validate-community
uv run python -m tin_lite.workflow_qualification_cli check \
  workflow_packages/example.csv_summary/workflow.json
uv run pytest tests/test_workflow_qualification.py
```

`check --fixtures outputs.json` optionally checks canned outputs. The file maps case IDs to
`{"status": "succeeded", "content": "..."}` or `{"status": "failed", "content": null}`.
This tests assertions, not workflow execution or model quality. It never establishes measured
cost. `candidate candidate.json` inspects a saved creator result and prints proposed changes
without writing them.

PR CI uses these checks and ordinary fixture-based pytest with no production secrets or paid
model access. Source validation parses code; it does not import or execute submitted Python.
Never run a PR's tests on a maintainer machine or runner containing production credentials.

## Authorized live cases

Review the candidate, cases, integration scopes and possible effects first. Activate the exact
reviewed private package in an authorized evaluation project, or use an explicitly selected
public package in a maintainer's test deployment. The evaluator does not activate anything.
Public candidates that cannot pass private activation need that test deployment; renaming or
weakening a package for a private pilot produces a different digest and is not evidence for
the unchanged public version. Production Registry publication remains a separate decision.

`evaluate_workflow_case` takes `project_id`, `path`, `revision`, `workflow_id`, `case_id`, a
stable UUID `request_id`, and `maximum_usd`. It checks the exact active package and case inputs,
then calls Tin's normal admission service. The existing run ceiling must fit the caller's
maximum. Paid evaluations require enrolled billing with enforced run budgets. Membership,
private allowlists, prerequisites, connections and normal approval boundaries still apply.

For a set of cases, the CLI keeps a resumable journal and checks the sum of case ceilings:

```bash
uv run python -m tin_lite.workflow_qualification_cli run \
  --url https://your-tin.example \
  --project-id PROJECT_UUID --workflow-id WORKFLOW_UUID \
  --path workflow_packages/custom.your_workflow/workflow.json \
  --revision FULL_PROJECT_COMMIT_SHA --maximum-usd 5.00 \
  --token-file /private/path/to/tin-access-token --out /private/path/to/evaluation
```

This command starts real runs and may incur costs and declared external effects. The token
is a Tin access token, never a provider credential. Run from trusted code against an authorized
test project, with separately supplied test connections. Keep journals and reports private;
they contain run metadata and usage. Do not commit customer outputs or credentials.

The journal persists request IDs before any start. Resume with the same arguments and output
directory after a lost response or timeout; it recovers the same run rather than buying a new
attempt. It stops at human review and never approves a result. A fresh journal is a new set
of runs. The aggregate maximum applies to starts by this invocation, not unrelated activity
in the project. The existing ledger enforces each admitted run's bound.

Finished runs can also be supplied directly to `qualify_workflow_package`. Tin verifies project
ownership, exact package bytes and normalized case inputs, then reads the run's canonical
artifact revision and trusted usage. A later file edit or workflow activation cannot rewrite
that evidence. Provider state and project context can still change between runs; record their
assumptions and retain output provenance when interpreting comparisons.

## Read the evidence

- **Shape:** existing package/runtime validation plus case input validation. Prerequisites,
  schedules, review eligibility, model routes and service bindings come from the manifest.
- **Cost:** deterministic code without model routes has zero LLM cost. Other packages start
  as unmeasured, with their configured ceiling shown separately. Finished, priced samples
  produce a range per exact case, with count, date, input size, models, price identity and
  observed failures. Missing usage stays unknown; different price/model contracts are not
  pooled. One sample is one observation, not a reliable range for every supported input.
- **Authorization and charge:** each sample separately records its enforced budget, verified
  unrounded model cost, settlement status and actual rounded charge. Existing receipts,
  price cards, ledger and rounding rules remain authoritative. A $0.004 cost can settle to
  $0.00. Connected-provider and infrastructure spending is outside this model-cost estimate,
  not asserted to be free. Supplier overages never enlarge the customer's authorized ceiling.
- **Evaluation and safety:** assertion results remain separate from rubric and safety review.
  Passing text or schema checks cannot prove factual quality, least privilege or idempotency.
  Review declared effects against source, gateway permissions and provider scopes. Artifact
  review after execution cannot prevent an external effect that already happened.

This first slice supports packaged code and procedures. Native executors retain existing tests
and accounting; a definition pin alone does not freeze their deployed implementation. Reports
do not change admission estimates, prices, published versions or saved configurations. Better
prompts, cheaper models or different algorithms are new candidate versions to compare and review.

The creator's [standing analytics case](../workflow_evals/custom.workflow_create/qualification.json)
and synthetic candidate test preserve activation, key-event trends, traffic, errors and justified
breakdowns. They are not a live analytics qualification. Rolling windows, real provider setup,
quality and realistic cost measurements must be resolved before publishing that workflow.
