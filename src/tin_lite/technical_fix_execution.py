"""Trusted preparation for the opt-in Codex title repair policy."""

import json
from uuid import UUID

from tin_lite import technical_fix as contract
from tin_lite.integrations import GitHubRepositoryBinding
from tin_lite.organic_audit import digest
from tin_lite.run_reports import publish_run_report
from tin_lite.technical_build_profile import match_profile
from tin_lite.technical_fix_sources import TechnicalFixSources


def binding_from(prepared):
    value = dict(prepared["repository_binding"])
    value["connection_id"] = UUID(str(value["connection_id"]))
    return GitHubRepositoryBinding(**value)


async def prepared_result(database, run_id):
    receipt = await database.get_effect(f"technical:{run_id}:prepare")
    if not receipt or receipt.status != "completed":
        raise ValueError("Technical repair preparation is unavailable.")
    return receipt.result


class TechnicalFixExecution:
    def __init__(self, *, database, storage, integrations, fetch=contract.fetch_page):
        self.db, self.storage, self.integrations, self.fetch = (
            database,
            storage,
            integrations,
            fetch,
        )

    async def active(self, run_id):
        run = await self.db.get_run(run_id)
        if (
            run is None
            or run.executor != "codex.procedure"
            or run.status.value not in {"pending", "running"}
            or not await self.db.has_project_access(
                project_id=run.project_id, clerk_user_id=run.started_by_clerk_user_id
            )
        ):
            raise ValueError("The technical repair is no longer active or accessible.")
        return run

    async def once(self, run_id, step, operation):
        key = f"technical:{run_id}:{step}"
        async with self.db.effect_lock(key, contract.KEY) as (conn, receipt):
            if receipt and receipt.status == "completed":
                return receipt.result
            await self.active(run_id)
            await self.db.start_effect(conn, execution_key=key, operation=contract.KEY)
            value = await operation()
            # UUID values belong to Postgres JSON, not Temporal payloads.
            value = json.loads(json.dumps(value, default=str))
            await self.db.complete_effect(conn, execution_key=key, result=value)
            return value

    async def prepare(self, run, *, policy=contract.LEGACY_POLICY):
        async def select():
            result = await TechnicalFixSources(
                database=self.db,
                storage=self.storage,
                integrations=self.integrations,
                supported_checks=contract.supported_checks(policy),
            ).preflight(
                project_id=run.project_id,
                audit_run_id=UUID(run.input["audit_run_id"]),
                **{
                    key: run.input[key]
                    for key in (
                        "audit_revision",
                        "finding_id",
                        "expected_repository",
                        "repository_serves_site",
                    )
                },
            )
            return {**result, "input_sha256": digest(run.input)}

        selection = await self.once(run.id, "binding", select)
        if selection["input_sha256"] != digest(run.input):
            raise ValueError("Technical repair inputs changed after preparation.")

        async def resolve():
            pages = []
            for index, url in enumerate(selection["selection"]["affected_urls"]):

                async def observe(url=url):
                    return await self.fetch(
                        url, host=contract.verified_page_host(url, selection["target"])
                    )

                pages.append(await self.once(run.id, f"page:{index}", observe))
            check = selection["selection"]["finding"]["check_id"]
            reason, originals, profile, overlapping = None, {}, {}, []
            unsupported = []
            if all(contract.has_metadata(page["html"], check) for page in pages):
                reason = "already_resolved"
            else:
                binding = binding_from(selection)
                bundle = await self.integrations.github_repository_bundle(
                    project_id=run.project_id,
                    run_id=run.id,
                    execution_key=f"{run.id}:procedure_repository_workspace",
                    expected_binding=binding,
                )
                if policy == contract.LEGACY_POLICY:
                    originals = contract.matched_sources(bundle.archive, pages)
                else:
                    matched = (
                        match_profile(
                            bundle.archive, pages, check, allow_partial=policy == contract.POLICY
                        )
                        if getattr(bundle, "complete", True)
                        else None
                    )
                    originals = matched["originals"] if matched else None
                    profile = matched["verification_profile"] if matched else {}
                    unsupported = matched["unsupported_pages"] if matched else []
                if originals is None:
                    reason, originals = "unsupported_source", {}
                else:
                    evidence = await self.integrations.github_open_pull_requests(
                        project_id=run.project_id,
                        run_id=run.id,
                        execution_key=f"{run.id}:procedure_open_pull_requests",
                        base_branch=binding.default_branch,
                        expected_binding=binding,
                    )
                    if set(originals).intersection(evidence.changed_paths):
                        reason = "open_pr_overlap"
                        document = json.loads(getattr(evidence, "document", b"{}"))
                        overlapping = [
                            {"number": pull["number"], "url": pull["url"]}
                            for pull in document.get("pull_requests", [])
                            if any(
                                file["path"] in originals or file.get("previous_path") in originals
                                for file in pull.get("files", [])
                            )
                        ]
                    elif evidence.truncated:
                        reason = "incomplete_pr_evidence"
            prepared = {
                **selection,
                "policy": policy,
                "verification_profile": profile,
                "overlapping_pull_requests": overlapping,
                "unsupported_pages": unsupported,
                "pages": [
                    {key: value for key, value in page.items() if key != "html"} for page in pages
                ],
                "originals": originals,
                "reason": reason,
            }
            # Context travels through the existing bounded sandbox environment. Do
            # not discover an oversized escaped payload after allocating compute.
            if len(json.dumps(prepared, separators=(",", ":")).encode()) > 70_000:
                prepared.update(reason="unsupported_source", originals={})
            return prepared

        prepared = await self.once(run.id, "prepare", resolve)
        if prepared["reason"]:
            await publish_run_report(
                database=self.db,
                storage=self.storage,
                run_id=run.id,
                workflow_key=contract.KEY,
                prefix="technical",
                path=f"reports/technical-fix/{run.id}/RESULT.md",
                content=contract.report(prepared, reason=prepared["reason"]),
                summary="Technical finding checked. No change proposed.",
            )
            return True
        return False

    async def validate_delivery(self, run, manifest, prepared):
        await self.active(run.id)
        if digest(run.input) != prepared["input_sha256"]:
            raise ValueError("The technical repair inputs changed.")
        contract.validate_manifest(manifest, prepared)
        if manifest["outcome"] == "no_change":
            return
        # Recheck the website at delivery too. Changed or unavailable HTML never
        # becomes authorization to send a stale patch, nor a claim of resolution.
        for page in prepared["pages"]:
            if prepared.get("policy") == contract.POLICY and page["url"] not in {
                observation["url"]
                for observation in prepared["verification_profile"]["observations"]
            }:
                continue
            check = contract.selected_check(prepared)
            present = "has_title" if check == contract.TITLE_CHECK else "has_description"
            if page.get(present, False):
                continue
            current = await self.fetch(
                page["url"], host=contract.verified_page_host(page["url"], prepared["target"])
            )
            if current["sha256"] != page["sha256"] or contract.has_metadata(current["html"], check):
                raise ValueError("The website changed after preparation. Start a new repair.")
