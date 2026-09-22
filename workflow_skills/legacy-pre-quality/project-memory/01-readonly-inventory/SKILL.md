---
name: 01-readonly-inventory
description: Extract stable project knowledge without altering or overstating source material.
---

# Read-only inventory

Summarize stable architecture, product decisions, operating constraints, and durable outputs that
will help with future work. Prefer short sections and bullets. Exclude transient execution details,
raw logs, secrets, tokens, credentials, and speculative recommendations.

This is a managed index, not a replacement for hand-authored wiki documents. Do not emit file
contents other than the requested index.

The `## Product` section is workflow-owned: the product deep dive and code map procedures write
its `### Feature map` and `### Code map` subsections directly, and Tin re-inserts the current
section verbatim after you finish. Do not emit a `## Product` heading or restate its contents;
summarize product facts elsewhere only when a supplied source establishes them.
