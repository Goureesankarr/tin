"""Run-bound service calls through existing project integration capabilities."""

import asyncio
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime

import httpx

from tin_lite.billing_contracts import digest
from tin_lite.integrations import (
    IntegrationError,
    IntegrationRequirement,
    ServiceResponseTooLarge,
)
from tin_lite.project_connections import CUSTOM_KEY, READ_METHODS, request_api, request_contract

OPERATION = "code_service_call_v1"
# Adding an adapter operation is an explicit reviewed mapping, never getattr on author input.
OPERATIONS = {
    ("analytics.gsc", "sites.list"): ("sites.list", frozenset()),
    ("analytics.gsc", "search_analytics.read"): (
        "search_analytics.read",
        frozenset(
            {"start_date", "end_date", "dimensions", "row_limit", "start_row", "dimension_filters"}
        ),
    ),
    ("infra.github", "repositories.list"): ("repositories.list", frozenset()),
    ("workspace.google", "gmail.messages.search"): (
        "gmail.messages.read",
        frozenset({"query", "max_results"}),
    ),
    ("workspace.google", "gmail.thread.read"): ("gmail.messages.read", frozenset({"thread_id"})),
    ("workspace.google", "calendar.events.list"): (
        "calendar.events.read",
        frozenset({"time_min", "time_max", "query", "max_results"}),
    ),
}


class CodeServiceError(ValueError):
    """Fixed safe errors; never supplier exceptions, bodies, URLs or authentication."""


def _too_large(service):
    # The bound is the author's own declared value; naming it lets them size the next request.
    return CodeServiceError(
        f"The service response exceeded this binding's max_response_bytes "
        f"({service.max_response_bytes}); request less data, for example a smaller "
        "row_limit or the next start_row page."
    )


class CodeServices:
    def __init__(self, *, database, integrations, authorize, client=None, resolver=None):
        self.db, self.integrations, self.authorize = database, integrations, authorize
        self.client, self.resolver = client, resolver

    async def call(self, *, conn, run, workflow, spec, payload):
        try:
            if not isinstance(payload, dict) or set(payload) != {
                "service",
                "step",
                "operation",
                "arguments",
            }:
                raise ValueError
            step = payload["step"]
            if not isinstance(step, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", step
            ):
                raise ValueError
            service = next(s for s in spec.services if s.name == payload["service"])
            custom = bool(CUSTOM_KEY.fullmatch(service.provider_key))
            args = payload["arguments"]
            if custom:
                if payload["operation"] != "http.request":
                    raise ValueError
                request_contract(args)
                capability = "http.read" if args["method"] in READ_METHODS else "http.write"
            else:
                capability, fields = OPERATIONS[(service.provider_key, payload["operation"])]
                if not isinstance(args, dict) or set(args) - fields:
                    raise ValueError
            if (
                capability not in service.capabilities
                or len(json.dumps(args, allow_nan=False).encode()) > 16_000
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, StopIteration, RecursionError):
            raise CodeServiceError(
                "The service request differs from its declared contract."
            ) from None
        fingerprint = digest(
            {
                "definition": run.definition_commit_sha,
                "service": asdict(service),
                "request": payload,
            }
        )
        prefix = f"{run.id}:code-service:"
        key = prefix + digest(step)
        async with self.db.effect_lock(f"{run.id}:code-service-gate", OPERATION, conn=conn):
            from tin_lite.code_models import CodeModelError

            try:
                await self.authorize(conn=conn, run=run, workflow=workflow, require_budget=False)
            except CodeModelError:
                raise CodeServiceError(
                    "The run no longer has permission to use services."
                ) from None
            requirement = IntegrationRequirement(service.provider_key, service.capabilities)
            try:
                await self.integrations.ensure_requirements(
                    project_id=run.project_id, requirements=(requirement,)
                )
                connection = await self.db.get_integration_connection(
                    project_id=run.project_id, provider_key=service.provider_key
                )
                if connection is None:
                    raise IntegrationError("connection removed")
            except IntegrationError:
                raise CodeServiceError(
                    "The declared project connection is unavailable or permission was removed."
                ) from None
            # Pin selected account/origin/permissions for this run, while allowing key rotation.
            binding = digest(
                {
                    "id": str(connection.id),
                    "account": connection.external_account_id,
                    "configuration": {
                        k: v for k, v in connection.configuration.items() if k != "access_verified"
                    },
                }
            )
            prior = await conn.fetchval(
                """SELECT result->>'binding' FROM effect_receipts WHERE operation=$1
                   AND execution_key LIKE $2 AND result->>'service'=$3 LIMIT 1""",
                OPERATION,
                prefix + "%",
                service.name,
            )
            if prior is not None and prior != binding:
                raise CodeServiceError("The connection changed during this run. Start a new run.")
            saved = await self.db.get_effect(key, conn=conn)
            if saved:
                record = saved.result or {}
                if record.get("fingerprint") != fingerprint:
                    raise CodeServiceError("This service step already has a different request.")
                if saved.status == "completed":
                    if record.get("error") == "response_too_large":
                        raise _too_large(service)
                    return record["response"]
                raise CodeServiceError(
                    "A service request has an unconfirmed result; "
                    "Tin will not repeat it automatically."
                )
            if await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM effect_receipts WHERE operation=$1 "
                "AND execution_key LIKE $2 AND status <> 'completed')",
                OPERATION,
                prefix + "%",
            ):
                raise CodeServiceError(
                    "An earlier service request is unresolved; a new step cannot retry it."
                )
            count = await conn.fetchval(
                "SELECT count(*) FROM effect_receipts WHERE operation=$1 "
                "AND execution_key LIKE $2 AND result->>'service'=$3",
                OPERATION,
                prefix + "%",
                service.name,
            )
            if count >= service.max_calls:
                raise CodeServiceError("The workflow reached its declared service-call limit.")
            secret = None
            if custom:
                if args["method"] not in connection.configuration["methods"]:
                    raise CodeServiceError("The connection does not allow this method.")
                secret, secret_revision = await self.integrations.custom.open_secret_record(
                    run.project_id, connection.configuration["secret_name"]
                )
            record = {
                "version": 1,
                "run_id": str(run.id),
                "step": step,
                "service": service.name,
                "provider": service.provider_key,
                "binding": binding,
                "fingerprint": fingerprint,
            }
            await self.db.start_effect(conn, execution_key=key, operation=OPERATION)
            await self.db.save_effect_progress(conn, execution_key=key, result=record)
            # Customer-provider usage is separate from Tin purchases. These observations
            # deliberately do not enter billing.begin_operation or settle customer credits.
            usage_key = f"usage:{key}"
            usage = {
                "version": 1,
                "run_id": str(run.id),
                "provider": service.provider_key,
                "category": "connected_api",
                "endpoint": payload["operation"],
                "step": step,
                "attempted_at": datetime.now(UTC).isoformat(),
                "outcome": "unconfirmed",
                "usage": None,
                "reported_cost_usd": None,
            }
            await self.db.start_effect(conn, execution_key=usage_key, operation="external_usage_v1")
            await self.db.save_effect_progress(conn, execution_key=usage_key, result=usage)
            try:
                async with asyncio.timeout(25):
                    response = (
                        await request_api(
                            connection,
                            secret,
                            args,
                            maximum=service.max_response_bytes,
                            operation_id=key,
                            client=self.client,
                            resolver=self.resolver,
                        )
                        if custom
                        else await self.adapter(
                            service.provider_key,
                            payload["operation"],
                            args,
                            run,
                            connection,
                            key,
                            max_response_bytes=service.max_response_bytes,
                        )
                    )
                if (
                    len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode())
                    > service.max_response_bytes
                ):
                    raise ServiceResponseTooLarge("oversized response")
            except ServiceResponseTooLarge:
                # A response arrived and was refused by size: a known outcome, not an uncertain
                # one. Settle both receipts so later steps are not blocked; the call still counts.
                async with conn.transaction():
                    await self.db.complete_effect(
                        conn, execution_key=key, result={**record, "error": "response_too_large"}
                    )
                    await self.db.complete_effect(
                        conn,
                        execution_key=usage_key,
                        result={**usage, "outcome": "response_received", "usage": {"requests": 1}},
                    )
                raise _too_large(service) from None
            except (
                IntegrationError,
                httpx.HTTPError,
                OSError,
                TimeoutError,
                ValueError,
                TypeError,
                KeyError,
            ):
                raise CodeServiceError(
                    "Service response unavailable or invalid; "
                    "the request will not be repeated automatically."
                ) from None
            async with conn.transaction():
                from tin_lite.code_progress import project_completed_steps

                await self.db.complete_effect(
                    conn, execution_key=key, result={**record, "response": response}
                )
                await self.db.complete_effect(
                    conn,
                    execution_key=usage_key,
                    result={**usage, "outcome": "response_received", "usage": {"requests": 1}},
                )
                await project_completed_steps(conn, run.id)
                if custom and response["status"] in {401, 403}:
                    await conn.execute(
                        """UPDATE integration_connections
                           SET status='needs_attention', last_error_code='authentication_failed',
                               configuration=jsonb_set(configuration,'{access_verified}','false'),
                               last_checked_at=now()
                           WHERE id=$1 AND configuration->>'revision'=$2
                           AND EXISTS(SELECT 1 FROM project_secrets WHERE project_id=$3
                             AND name=$4 AND revision=$5)""",
                        connection.id,
                        connection.configuration["revision"],
                        run.project_id,
                        connection.configuration["secret_name"],
                        secret_revision,
                    )
                if custom and 200 <= response["status"] < 300:
                    await conn.execute(
                        """UPDATE integration_connections
                           SET configuration=jsonb_set(configuration,'{access_verified}','true'),
                           last_checked_at=now() WHERE id=$1 AND configuration->>'revision'=$2
                           AND EXISTS(SELECT 1 FROM project_secrets WHERE project_id=$3
                             AND name=$4 AND revision=$5)""",
                        connection.id,
                        connection.configuration["revision"],
                        run.project_id,
                        connection.configuration["secret_name"],
                        secret_revision,
                    )
            return response

    async def adapter(self, provider, operation, args, run, connection, key, *, max_response_bytes):
        service = self.integrations
        if operation == "sites.list":
            return {
                "sites": [asdict(x) for x in await service.google_sites(project_id=run.project_id)]
            }
        if operation == "repositories.list":
            return {
                "repositories": [
                    asdict(x) for x in await service.github_repositories(project_id=run.project_id)
                ]
            }
        if provider == "analytics.gsc":
            return await service.search_console_analytics(
                project_id=run.project_id,
                run_id=run.id,
                execution_key=key,
                expected_site_url=connection.configuration.get("selected_site_url"),
                max_response_bytes=max_response_bytes,
                **args,
            )
        common = {
            "project_id": run.project_id,
            "run_id": run.id,
            "connection_id": connection.id,
            "external_account_id": connection.external_account_id,
            "execution_key": key,
        }
        if operation == "gmail.messages.search":
            return await service.workspace_search_messages(**common, **args)
        if operation == "gmail.thread.read":
            return await service.workspace_get_thread(**common, **args)
        return await service.workspace_list_calendar_events(**common, **args)
