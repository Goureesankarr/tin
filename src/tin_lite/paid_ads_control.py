"""The paid ads assessment's shared, membership-gated HTTP/MCP stop boundary."""

from uuid import UUID

from tin_lite.paid_ads import KEY


async def stop_paid_ads_assessment(*, runtime, run_id: UUID, clerk_user_id: str):
    run = await runtime.database.get_run(run_id)
    if run is None or not await runtime.database.has_project_access(
        project_id=run.project_id, clerk_user_id=clerk_user_id
    ):
        raise LookupError("run not found")
    if run.executor != KEY:
        raise ValueError("This control stops a paid ads assessment only.")
    stopped = await runtime.database.stop_paid_ads_assessment(
        run_id=run_id, project_id=run.project_id, actor=clerk_user_id
    )
    # Postgres fences new paid work even if delivery of the stop signal needs retry.
    await runtime.temporal.get_workflow_handle(run.temporal_workflow_id).signal("stop")
    return stopped


async def stop_paid_ads_launch(*, runtime, run_id: UUID, clerk_user_id: str):
    from tin_lite.paid_ads_launch import KEY as LAUNCH_KEY

    run = await runtime.database.get_run(run_id)
    if run is None or not await runtime.database.has_project_access(
        project_id=run.project_id, clerk_user_id=clerk_user_id
    ):
        raise LookupError("run not found")
    if run.executor != LAUNCH_KEY:
        raise ValueError("This control stops a Google Ads launch only.")
    stopped = await runtime.database.stop_paid_ads_launch(
        run_id=run_id, project_id=run.project_id, actor=clerk_user_id
    )
    await runtime.temporal.get_workflow_handle(run.temporal_workflow_id).signal("stop")
    return stopped


async def stop_paid_ads_monitor(*, runtime, run_id: UUID, clerk_user_id: str):
    from tin_lite.paid_ads_monitor import KEY as MONITOR_KEY

    run = await runtime.database.get_run(run_id)
    if run is None or not await runtime.database.has_project_access(
        project_id=run.project_id, clerk_user_id=clerk_user_id
    ):
        raise LookupError("run not found")
    if run.executor != MONITOR_KEY:
        raise ValueError("This control stops a Google Ads check only.")
    stopped = await runtime.database.stop_paid_ads_monitor(
        run_id=run_id, project_id=run.project_id, actor=clerk_user_id
    )
    await runtime.temporal.get_workflow_handle(run.temporal_workflow_id).signal("stop")
    return stopped
