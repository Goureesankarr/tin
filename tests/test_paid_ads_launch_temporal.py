"""Local Temporal proof for the Google Ads launch: the approval wait, a retried publication, a
stop that fences drafting and creation, the setup short-cut, identifier-only history and replay.
No paid calls; every activity is a stub."""

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

from tin_lite.activity_lanes import TRUSTED_ACTIVITIES
from tin_lite.workflows import PaidAdsLaunchWorkflow

NAMES = sorted(name for name in TRUSTED_ACTIVITIES if name.startswith("paid_ads_launch_"))
AFTER_REVIEW = {
    "paid_ads_launch_record_approval",
    "paid_ads_launch_apply",
    "paid_ads_launch_publish",
}


def test_every_launch_activity_is_a_trusted_lane():
    assert NAMES == [
        "paid_ads_launch_apply",
        "paid_ads_launch_draft",
        "paid_ads_launch_failure",
        "paid_ads_launch_gather",
        "paid_ads_launch_prepare",
        "paid_ads_launch_publish",
        "paid_ads_launch_record_approval",
        "paid_ads_launch_request_review",
        "paid_ads_launch_settle_setup",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["launch", "setup", "stop"])
async def test_launch_waits_for_approval_retries_publish_and_fences_on_stop(scenario):
    binary = shutil.which("temporal")
    if binary is None:
        pytest.skip("Local Temporal CLI is required")
    run_id, calls = str(uuid4()), []
    gathering, release, reviewing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = 0

    def stub(name):
        @activity.defn(name=name)
        async def implementation(control):
            nonlocal attempts
            calls.append((name, control))
            if name == "paid_ads_launch_gather" and scenario == "stop":
                gathering.set()
                await release.wait()
            if name == "paid_ads_launch_draft":
                return "setup" if scenario == "setup" else "launch"
            if name == "paid_ads_launch_request_review":
                reviewing.set()
            if name == "paid_ads_launch_publish":
                attempts += 1
                if attempts == 1:
                    raise ApplicationError("Injected lost publication response")
            if name == "paid_ads_launch_failure":
                raise AssertionError("Completion, setup and stop must not fail")
            return None

        return implementation

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, dev_server_log_level="error"
    ) as env:
        async with Worker(
            env.client,
            task_queue="paid-ads-launch-local-test",
            workflows=[PaidAdsLaunchWorkflow],
            activities=[stub(name) for name in NAMES],
        ):
            handle = await env.client.start_workflow(
                PaidAdsLaunchWorkflow.run,
                run_id,
                id=f"growth.paid_ads_launch:{run_id}",
                task_queue="paid-ads-launch-local-test",
            )
            if scenario == "stop":
                await asyncio.wait_for(gathering.wait(), timeout=15)
                await handle.signal("stop")
                release.set()
            elif scenario == "launch":
                await asyncio.wait_for(reviewing.wait(), timeout=15)
                await asyncio.sleep(0.2)
                assert not any(name in AFTER_REVIEW for name, _ in calls)
                await handle.signal("approve")
            await asyncio.wait_for(handle.result(), timeout=30)
            history = await handle.fetch_history()
        await Replayer(workflows=[PaidAdsLaunchWorkflow]).replay_workflow(history)
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            for payload in event.activity_task_scheduled_event_attributes.input.payloads:
                assert json.loads(payload.data) == run_id
    names = [name for name, _ in calls]
    if scenario == "stop":
        assert names == ["paid_ads_launch_prepare", "paid_ads_launch_gather"]
    elif scenario == "setup":
        assert names == [
            "paid_ads_launch_prepare",
            "paid_ads_launch_gather",
            "paid_ads_launch_draft",
            "paid_ads_launch_settle_setup",
        ]
    else:
        assert attempts == 2 and names == [
            "paid_ads_launch_prepare",
            "paid_ads_launch_gather",
            "paid_ads_launch_draft",
            "paid_ads_launch_request_review",
            "paid_ads_launch_record_approval",
            "paid_ads_launch_apply",
            "paid_ads_launch_publish",
            "paid_ads_launch_publish",
        ]
