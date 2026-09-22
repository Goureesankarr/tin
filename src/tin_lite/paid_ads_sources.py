"""Verified reads of earlier project publications for the paid-ads assessment.

Every upstream run is optional. When one is named it must belong to this project, have
succeeded, and its documents must match the publish receipt they were published under.
"""

from __future__ import annotations

import json
from uuid import UUID

from tin_lite.domain import GROWTH_ONBOARDING_PLAN_PATH, GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME
from tin_lite.keyword_plan import LIMITS as KEYWORD_LIMITS
from tin_lite.keyword_plan import paths as keyword_paths
from tin_lite.organic_audit import ARTIFACT_LIMITS, audit_paths, digest

PLAN_EXCERPT_CHARS = 6000
AUDIT_EXCERPT_CHARS = 2500
KEYWORD_ROWS = 300


async def pinned_bundle(
    *, database, storage, project, run_id, executor, prefix, paths, limits
) -> tuple[dict, dict]:
    """Read one earlier run's documents at the revision its publish receipt vouches for."""
    source = await database.get_run(UUID(str(run_id)))
    if (
        source is None
        or source.project_id != project.id
        or source.executor != executor
        or source.status.value != "succeeded"
        or not source.canonical_commit_sha
    ):
        raise ValueError(f"Choose a successful {executor} run in this project.")
    receipt = await database.get_effect(f"{prefix}:{source.id}:publish")
    publication = receipt.result if receipt and receipt.status == "completed" else {}
    if publication.get("canonical_commit_sha") != source.canonical_commit_sha:
        raise ValueError(f"The {executor} publication receipt does not match its run.")
    documents = {}
    for name, path in paths(str(source.id)).items():
        content = await storage.read_canonical_artifact(
            repo_id=project.state_repo_id, commit_sha=source.canonical_commit_sha, path=path
        )
        if not 0 < len(content) <= limits[name]:
            raise ValueError(f"The {executor} source exceeds its read contract.")
        documents[path] = content.decode("utf-8")
    if digest(documents) != publication.get("documents_sha256"):
        raise ValueError(f"The {executor} bundle failed receipt verification.")
    return (
        {"run_id": str(source.id), "revision": source.canonical_commit_sha},
        {name: documents[path] for name, path in paths(str(source.id)).items()},
    )


async def onboarding_plan_source(*, database, storage, project, run_id) -> dict:
    """The Start here plan: its founder inputs and a bounded excerpt of the written plan."""
    source = await database.get_run(UUID(str(run_id)))
    if (
        source is None
        or source.project_id != project.id
        or source.executor != GROWTH_ONBOARDING_PLAN_WORKFLOW_NAME
        or source.status.value != "succeeded"
        or not source.canonical_commit_sha
        or source.artifact_path != GROWTH_ONBOARDING_PLAN_PATH
    ):
        raise ValueError("Choose a successful Start here plan in this project.")
    content = await storage.read_canonical_artifact(
        repo_id=project.state_repo_id,
        commit_sha=source.canonical_commit_sha,
        path=GROWTH_ONBOARDING_PLAN_PATH,
    )
    text = content[: PLAN_EXCERPT_CHARS * 4].decode("utf-8", "replace")
    inputs = source.input or {}
    return {
        "run_id": str(source.id),
        "revision": source.canonical_commit_sha,
        "inputs": {
            key: inputs.get(key)
            for key in ("product_url", "priority", "budget", "hard_nos", "notes", "system_ads")
            if inputs.get(key) not in (None, "", [])
        },
        "plan_excerpt": text[:PLAN_EXCERPT_CHARS],
    }


def _keyword_rows(rows: list) -> list[dict]:
    kept = []
    for row in rows[:KEYWORD_ROWS]:
        if not isinstance(row, dict) or not isinstance(row.get("keyword"), str):
            continue
        observations = row.get("observations") or [row]
        first = observations[0] if isinstance(observations[0], dict) else {}
        kept.append(
            {
                "keyword": row["keyword"],
                **{
                    key: first.get(key)
                    for key in ("search_volume", "cpc", "paid_competition", "position")
                },
            }
        )
    return kept


async def upstream_sources(*, database, storage, project, inputs, market) -> dict:
    """All three optional sources, verified; a missing id simply leaves the key out."""
    found: dict = {}
    if inputs.get("onboarding_run_id"):
        found["onboarding"] = await onboarding_plan_source(
            database=database, storage=storage, project=project, run_id=inputs["onboarding_run_id"]
        )
    if inputs.get("keyword_run_id"):
        meta, documents = await pinned_bundle(
            database=database,
            storage=storage,
            project=project,
            run_id=inputs["keyword_run_id"],
            executor="organic.keyword_plan",
            prefix="keyword",
            paths=keyword_paths,
            limits=KEYWORD_LIMITS,
        )
        keywords = json.loads(documents["keywords.json"])
        evidence = json.loads(documents["evidence.json"])
        scope = evidence.get("scope") or keywords.get("scope") or {}
        if scope.get("market") not in (None, market):
            raise ValueError("The keyword plan was researched for a different market.")
        found["keyword"] = {
            **meta,
            "seeds": list(evidence.get("seeds") or [])[:15],
            "competitors": list(evidence.get("competitors") or [])[:6],
            "keywords": _keyword_rows(keywords.get("keywords") or keywords.get("candidates") or []),
        }
    if inputs.get("audit_run_id"):
        meta, documents = await pinned_bundle(
            database=database,
            storage=storage,
            project=project,
            run_id=inputs["audit_run_id"],
            executor="organic.audit",
            prefix="organic",
            paths=audit_paths,
            limits=ARTIFACT_LIMITS,
        )
        evidence = json.loads(documents["evidence.json"])
        scope = evidence.get("scope") or {}
        if scope.get("market") not in (None, market):
            raise ValueError("The audit was run for a different market.")
        found["audit"] = {
            **meta,
            "host": scope.get("host"),
            "report_excerpt": documents["AUDIT.md"][:AUDIT_EXCERPT_CHARS],
        }
    return found
