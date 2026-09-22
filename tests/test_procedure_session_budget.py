"""One funded Codex session; trusted usage, disposable Postgres and synthetic wire only."""

import asyncio
import json
import tomllib
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from test_billing import billed as billed
from test_billing import finish, fund, quote, start
from test_codex_api import BODY, post
from test_codex_api_billing import facts, paid_relay
from test_codex_isolation import load_sandbox_module, observation
from test_procedure_publication import publication_db as publication_db

from tin_lite.billing_contracts import BillingError, final_charge
from tin_lite.codex_api import CONTRACT, PROCEDURE_CONTRACT, SESSION_CONTRACT
from tin_lite.codex_api_pricing import RATE_CARD, api_terms, price_response
from tin_lite.codex_api_relay import request_body
from tin_lite.workflow_costs import configured_terms, session_funded

MAXIMUM = 5_000_000_000


def test_session_contract_and_model_capacity(tmp_path):
    raw = json.dumps({**BODY, "input": "x" * 1_100_000, "max_output_tokens": 200_000}).encode()
    assert request_body(raw, "responses", SESSION_CONTRACT)["max_output_tokens"] == 128_000
    for old in (CONTRACT, PROCEDURE_CONTRACT):
        with pytest.raises(HTTPException):
            request_body(raw, "responses", old)
    assert (
        request_body(
            json.dumps({**BODY, "max_output_tokens": 12345}).encode(), "responses", SESSION_CONTRACT
        )["max_output_tokens"]
        == 12345
    )
    config = tmp_path / "config.toml"
    config.write_text('model="gpt-6-astra"\n')
    load_sandbox_module("codex_api_config").configure(
        config,
        {
            "TIN_PROCEDURE_ISOLATED": "1",
            "TIN_CODEX_API_URL": "https://tin.test/internal/codex-api/run/v1",
            "TIN_CODEX_API_GRANT": "synthetic",
            "TIN_CODEX_API_CONTRACT": json.dumps(SESSION_CONTRACT),
        },
    )
    parsed = tomllib.loads(config.read_text())
    assert parsed["model_context_window"] == 1_050_000
    assert parsed["model_auto_compact_token_limit"] == 922_000
    usage = load_sandbox_module("codex_usage").CodexUsage("thread", "turn", limit=None)
    assert usage.observe(observation(3_000_000))
    assert not usage.limit_reached
    assert usage.record()["observed_token_limit"] is None


@pytest.mark.parametrize("profile", ["default", "isolated", "browser", "studio"])
@pytest.mark.parametrize("validator", [None, "tin-diagram.reviewed.v1", "demo-video.v1"])
def test_only_ordinary_root_procedures_select_session_terms(profile, validator):
    definition = {
        "executor": "codex.procedure",
        "procedure": {"sandbox": {"profile": profile}, "output": {"validator": validator}},
    }
    selected = api_terms(definition, session_budget=True)
    eligible = profile in {"default", "isolated"} and validator is None
    assert session_funded(selected) == eligible
    if eligible:
        assert selected["codex_contract"] == SESSION_CONTRACT
        assert "request_maximum_nanos" not in selected
        assert session_funded(configured_terms(selected, definition, {}))
    else:
        assert selected == api_terms(definition)
    assert not session_funded(api_terms(definition))  # Included/child compatibility path.
    assert not session_funded(api_terms({"executor": "project.task"}, session_budget=True))


async def test_long_session_uses_returned_usage_without_wallet_lock_or_history_repricing(
    billed, monkeypatch
):
    import tin_lite.codex_api_relay as transport

    f = billed
    run, relay, client, sent = await paid_relay(
        f, contract=SESSION_CONTRACT, provider_usage=(30_000, 1, 30_000, 0)
    )
    prices = []

    def meter(card, record):
        prices.append(record["request_fingerprint"])
        return price_response(card, record)

    monkeypatch.setattr(transport, "price_response", meter)
    try:
        assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == MAXIMUM
        # An unrelated wallet writer must not serialize every response of this session.
        async with f.db.pool.acquire() as lock, lock.transaction():
            await lock.execute("SELECT workspace_id FROM billing_accounts FOR UPDATE")
            response = await asyncio.wait_for(
                post(client, run, {**BODY, "input": "x" * 1_100_000}), 2
            )
            assert response.status_code == 200, response.text
        for i in range(99):
            response = await post(client, run, {**BODY, "input": f"model step {i}"})
            assert response.status_code == 200, response.text
        assert len(sent) == len(prices) == 100
        assert json.loads(sent[0].content)["max_output_tokens"] == 128_000
        assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == MAXIMUM
        assert (
            await f.db.pool.fetchval("SELECT committed_nanos FROM billing_run_budgets")
            == 3_005_000_000
        )
        assert (
            await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 0
        )
        assert await f.billing.settle(run.id) is None
        await finish(f, run)
        assert await f.billing.settle(run.id) == 3_010_000_000
        assert await f.billing.settle(run.id) == 3_010_000_000
        assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == 0
        assert (
            await f.db.pool.fetchval("SELECT count(*) FROM billing_ledger WHERE kind='charge'") == 1
        )
    finally:
        await client.aclose()
        await relay.close()


async def test_final_response_overage_never_increases_customer_ceiling(billed):
    f = billed
    run, relay, client, sent = await paid_relay(
        f, contract=SESSION_CONTRACT, provider_usage=(300_000, 128_000, 0, 0)
    )
    try:
        assert (await post(client, run)).status_code == 200
        assert (await post(client, run, {**BODY, "input": "another step"})).status_code == 402
        operation = await f.db.pool.fetchrow("SELECT * FROM billing_operations")
        assert operation["observed_nanos"] == MAXIMUM
        assert json.loads(operation["observation"])["overage_absorbed_nanos"] == 10_600_000_000
        await finish(f, run)
        assert await f.billing.settle(run.id) == MAXIMUM
        assert len(sent) == 1
    finally:
        await client.aclose()
        await relay.close()


async def test_session_blocks_overlapping_requests_and_identical_repurchase(billed):
    f = billed
    run, relay, client, sent = await paid_relay(f, contract=SESSION_CONTRACT)
    arrived, release = asyncio.Event(), asyncio.Event()
    original_send = relay.client.send

    async def delayed(*args, **kwargs):
        arrived.set()
        await release.wait()
        return await original_send(*args, **kwargs)

    relay.client.send = delayed
    task = asyncio.create_task(post(client, run))
    try:
        await asyncio.wait_for(arrived.wait(), 2)
        assert (await post(client, run, {**BODY, "input": "parallel"})).status_code == 409
        release.set()
        assert (await task).status_code == 200
        assert (await post(client, run)).status_code == 409
        assert len(sent) == 1
    finally:
        release.set()
        await task
        await client.aclose()
        await relay.close()


@pytest.mark.parametrize("gap", [False, True])
async def test_unknown_usage_blocks_continuation_and_reconciliation_does_not_repurchase(
    billed, gap
):
    f = billed
    run, relay, client, sent = await paid_relay(f, contract=SESSION_CONTRACT, missing_usage=not gap)
    original = f.billing.observe_operation
    if gap:
        f.billing.observe_operation = AsyncMock(side_effect=RuntimeError("interrupted projection"))
    try:
        await post(client, run)
        f.billing.observe_operation = original
        assert (await post(client, run, {**BODY, "input": "next"})).status_code == 409
        await finish(f, run)
        expected = final_charge(price_response(RATE_CARD, facts())[0], MAXIMUM) if gap else None
        assert await f.billing.settle(run.id) == expected
        if not gap:
            assert (
                await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == MAXIMUM
            )
            await f.db.pool.execute(
                "UPDATE billing_run_budgets SET reconcile_by=now()-interval '1 second'"
            )
            assert await f.billing.settle(run.id) == 0
            assert await f.db.pool.fetchval("SELECT status FROM billing_operations") == "absorbed"
        assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == 0
        assert len(sent) == 1
    finally:
        f.billing.observe_operation = original
        await client.aclose()
        await relay.close()


@pytest.mark.parametrize(
    "revoke", ["membership", "account", "run_limit", "monthly_limit", "lease", "stopped"]
)
async def test_session_preserves_current_authorization_boundaries(billed, revoke):
    f = billed
    run, relay, client, sent = await paid_relay(f, contract=SESSION_CONTRACT)
    changes = {
        "membership": "DELETE FROM project_memberships",
        "account": "UPDATE billing_accounts SET status='suspended'",
        "run_limit": "UPDATE billing_project_policies SET per_run_nanos=1",
        "monthly_limit": "UPDATE billing_project_policies SET monthly_nanos=1",
        "lease": "UPDATE workflow_runs SET lease_active=false",
        "stopped": "UPDATE workflow_runs SET status='failed'",
    }
    try:
        await f.db.pool.execute(changes[revoke])
        assert (await post(client, run)).status_code in {402, 403, 409}
        assert not sent
        assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0
    finally:
        await client.aclose()
        await relay.close()


async def test_concurrent_admission_cannot_reuse_the_same_session_funds(billed):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    await fund(f, cents=1000)
    q1, q2, q3 = await quote(f), await quote(f), await quote(f)
    outcomes = await asyncio.gather(
        start(f, q1), start(f, q2), start(f, q3), return_exceptions=True
    )
    assert sum(isinstance(value, BillingError) for value in outcomes) == 1
    assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == 2 * MAXIMUM
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == 2


@pytest.mark.parametrize("whole_run", [False, True])
async def test_issued_private_v1_quotes_keep_their_limits_and_funding(billed, whole_run):
    f = billed
    await fund(f)
    f.settings.codex_api_projects = {f.project.id}
    q = await quote(f)
    terms = f.billing.terms(
        f.workflow.definition,
        f.project.id,
        {"brief": "Explain the public docs"},
        session_budget=False,
    )
    if whole_run:
        terms.pop("funding")
        terms.pop("estimate")
    await f.db.pool.execute(
        "UPDATE billing_quotes SET terms=$2::jsonb WHERE id=$1", UUID(q["id"]), json.dumps(terms)
    )
    run = await start(f, q)
    pinned = json.loads(
        await f.db.pool.fetchval("SELECT terms FROM billing_run_budgets WHERE run_id=$1", run.id)
    )
    assert pinned == terms
    assert "codex_contract" not in pinned
    assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == (
        MAXIMUM if whole_run else 0
    )
