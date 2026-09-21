# Repository-aware article delivery

## In plain English

The article remains a Markdown document in Tin. Once the founder approves it, **Prepare
PR** adapts that same article to the connected website's existing structure. The user
chooses the article and repository, not Markdown versus a component file. The PR stays
unmerged. WordPress and other CMS publication need their own connectors later.

This is an explicit continuation on the content card. Repository adaptation uses compute,
so the normal spending confirmation applies. Approval of an older draft is still only
approval of its content; it does not silently acquire a publishing action.

## Implementation

- `content.deliver` is an ordinary immutable Organic traffic catalog definition on the
  existing `codex.procedure` executor, not another engine or saved program. It is callable
  independently through HTTP/MCP and from the existing My System content program card.
- Admission reads only an approved, successfully published `content.generate` artifact
  in the same project. It verifies the canonical receipt and source frontmatter, removes
  Tin metadata/verification notes, and pins the exact body, digest, source revision,
  repository ID, installation, connection, default branch and Git head.
- Run, source receipt and ordinary billing reservation commit in one transaction. A
  concurrent second start for the same article/repository cannot buy another adaptation.
  The same request ID returns the original run. Versions remain internal metadata.
- The normal project-scoped Codex execution queue, isolated/API authentication selection,
  usage accounting, lease fencing and sandbox cleanup remain unchanged. The article is
  passed as trusted source context; neither source text nor credentials enter Temporal.
- The agent inspects repository instructions, existing articles, layouts, routing and open
  PR evidence. It may change up to five text files / 180 KB. It cannot add dependencies.
  Markdown-native sites keep Markdown. Component sites use an existing Markdown renderer
  and retain the article as a complete JSON string or JSON data. Unsupported rendering
  arrangements are reported as a prerequisite, not silently approximated as rewritten JSX.
- The switchboard checks exact source preservation before accepting a patch and again
  before delivery. The immutable checkpoint survives sandbox teardown. The existing
  GitHub gateway checks the destination, overlaps and retry identity, and opens an
  unmerged PR. Unrelated base advances use the existing bounded content-delivery exception;
  changed destination paths are not silently rebased.
- Current repository procedure definitions explicitly permit snapshots of up to 1,000
  files / 100 MB (individual file cap remains 2 MB). This includes article delivery,
  site health, technical fixes and code maps. Legacy definitions without explicit limits
  retain 500 files / 10 MB; earlier explicit 20 MB definitions keep that limit. Limits
  are pinned in the definition and bound into gateway request identity. Saved configurations
  keep their selected revision; select the updated definition to use the larger snapshot.
  Oversized repositories fail before blob downloads, with their eligible file count and
  byte total alongside the pinned limits.
- A canonical receipt at `content/deliveries/{run_id}.md` links the PR. The original
  Markdown and the roadmap are unchanged. Status, article choices, titles and PR links
  are Postgres projections, shared by HTTP/MCP and the existing content card.

## Verification limits — do not overstate these

Exact Markdown/string preservation proves the original copy was retained in the patch.
It does **not** prove that every byte is rendered, that the wrapper has no additional copy,
that Markdown extensions display identically, or that a site build succeeded. The mandatory
runner check is `git diff --check`; it is not a compiler or build check.

The procedure must use the site's existing renderer, run available relevant checks, and
separately report checks it could not run. The PR needs a site-preview/editorial review
before merging. There is no universal dependency installer or framework-specific build
adapter in this slice. Verify live adaptation, build and preview against an explicitly
approved article. A stronger renderer/build contract should be shared across repository
procedures, not a hidden special case for one website.

## Failure and retry

Draft approval/success never changes because delivery fails. A member can invoke
`retry_content_delivery` for a failed adaptation that has a completed immutable patch.
The existing internal delivery workflow revalidates and sends that saved patch using
the original GitHub execution key. No model runs and no new article is generated.
The confirmed result is separately receipted and shown on the content card/Activity;
the failed adaptation's historical status is not rewritten as success.

An attempt with no accepted patch may be explicitly retried as a **new metered
adaptation**, using “Try adaptation again” on the content card or `retry_run_id`
through MCP (internal attempt context, not a revision selector). A saved
patch or any prior provider delivery receipt prevents that fresh purchase until the
existing effect is reconciled. Changed destination content requires fresh review;
this slice does not silently throw away an earlier checkpoint or redirect its PR.

The older automatic `.md` delivery contract remains intact for pinned runs/settings.
An article already enrolled in that contract must use its existing delivery action;
the new continuation cannot start a second competing PR for it.

## Acceptance

Automated coverage includes source approval/project boundaries, canonical proof, exact
Markdown and component-string copies, changed-copy/dependency rejection, atomic and
concurrent starts, HTTP/MCP parity, ordinary API billing admission/idempotence, normal
PR publication, frozen-checkpoint recovery, and reverse projection into the content card.
Browser coverage uses the existing controls in both themes, title-based article choices,
hidden run IDs, responsive widths, scoped saves and preservation of unsaved plan/settings.
Existing Temporal replay/queue tests remain part of the suite; no workflow command
sequence or registered execution engine was changed.
