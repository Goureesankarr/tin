"""Local Temporal retry, stop and replay proof for the paid ads assessment; no paid calls."""

from __future__ import annotations

import asyncio
import json
import shutil
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tin_lite.workflows import PaidAdsAssessmentWorkflow

NAMES = [
    "paid_ads_prepare",
    "paid_ads_gather",
    "paid_ads_research",
    "paid_ads_assess",
    "paid_ads_publish",
    "paid_ads_project",
    "paid_ads_failure",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_native_paid_ads_retry_stop_and_identifier_only_history(stop):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Local Temporal CLI is required")
    run_id, calls = str(uuid4()), []
    researching, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    def stub(name):
        @activity.defn(name=name)
        async def implementation(control):
            nonlocal attempts
            calls.append((name, control))
            if name == "paid_ads_research" and stop:
                researching.set()
                await release.wait()
            if name == "paid_ads_publish":
                attempts += 1
                if attempts == 1:
                    raise ApplicationError("Injected lost publication response")
            if name == "paid_ads_failure":
                raise AssertionError("Completion and stop must not fail")

        return implementation

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue="paid-ads-local-test",
            workflows=[PaidAdsAssessmentWorkflow],
            activities=[stub(name) for name in NAMES],
        ):
            handle = await env.client.start_workflow(
                PaidAdsAssessmentWorkflow.run,
                run_id,
                id=f"growth.paid_ads_assessment:{run_id}",
                task_queue="paid-ads-local-test",
            )
            if stop:
                await asyncio.wait_for(researching.wait(), timeout=15)
                await handle.signal("stop")
                release.set()
            await asyncio.wait_for(handle.result(), timeout=30)
            history = await handle.fetch_history()
        await Replayer(workflows=[PaidAdsAssessmentWorkflow]).replay_workflow(history)
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            for payload in event.activity_task_scheduled_event_attributes.input.payloads:
                assert json.loads(payload.data) == run_id
    if stop:
        assert not any(
            name in {"paid_ads_assess", "paid_ads_publish", "paid_ads_project"} for name, _ in calls
        )
    else:
        assert attempts == 2 and calls[-1] == ("paid_ads_project", run_id)
