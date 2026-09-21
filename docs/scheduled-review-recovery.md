# Scheduled review lifetime and recovery

The 24-hour catch-up window limits how late a scheduled occurrence may start. It is
not an approval deadline. The dispatcher waits for its child to finish, including
human review, so Temporal's skip-overlap policy covers the whole review. Postgres
also rejects a new scheduled occurrence while the same saved configuration has a
pending, running, needs-input or paused run. This protects recovered children whose
original dispatcher has already closed. Retrying the same occurrence still returns
the existing run.

## Upgrade existing schedules

Older stored Temporal schedule actions have a 24-hour workflow execution timeout.
Updating the application does not update those stored actions. After deploying the
fix, use the normal operator environment to preview and apply the migration:

```bash
uv run tin-lite repair-schedule-timeouts
uv run tin-lite repair-schedule-timeouts --apply
```

This operator command touches only Tin dispatcher actions with the old 24-hour
timeout. It preserves the latest calendar, pause state, catch-up/overlap policy,
task queue, arguments and metadata, and is safe to rerun. An unexpected action or
timeout fails closed for inspection. The command does not change Postgres inputs,
approve drafts, reset workflows or alter deadlines already pinned to executions.

Inventory active legacy dispatchers separately. Their existing execution timeout
cannot be removed by updating the schedule; they can still expire after the upgrade.
If one expires while awaiting review, inspect and recover its child as below.

## Recover a terminated answer-page draft

Recovery is a separate operator action. Do not create another product run, regenerate
the paid draft or mark it approved in Postgres. For `content.answer_page`, first verify:

1. The product run is still `needs_input`, has no review decision, and its exact
   committed artifact is readable. It belongs to the scheduled configuration in question.
2. The latest Temporal execution of that exact child is terminated with reason
   `by parent close policy`. Its recorded parent is the matching Tin dispatcher,
   which timed out with the legacy 24-hour deadline. The child itself has no deadline.
3. `draft_answer_page` and `request_answer_page_review` completed successfully,
   the review activity returned true, and the following workflow task completed.
   There are no later approvals, other signals, updates, effects or pending activities.
4. The current worker can replay this history, and the schedule migration and durable
   overlap guard are deployed. Coordinate with other operators and recheck the latest
   execution immediately before resetting; resetting an older execution can terminate
   a newer one.

Reset that child to the verified completed workflow task immediately after the review
activity, explicitly excluding event reapplication. With a configured Temporal CLI:

```bash
temporal workflow reset \
  --workflow-id "$REVIEW_WORKFLOW_ID" \
  --run-id "$TERMINATED_EXECUTION_ID" \
  --event-id "$REVIEW_TASK_COMPLETED_EVENT_ID" \
  --reapply-exclude All \
  --reason "Restore scheduled draft waiting for approval after dispatcher timeout"
```

Record the returned Temporal execution ID. If the response is lost, inspect the
latest execution before considering another reset. The reset uses completed activity
results from history and resumes the approval wait in a new Temporal execution with
the same workflow ID. It does not recreate the parent or change the product run ID.
Verify the child is running, the same artifact and product review gate remain, and
no drafting or approval activity ran again. The founder can then use the ordinary
authenticated approval action. Other workflow types and histories require their own
review of the exact reset boundary; do not apply this procedure to them blindly.

The regression tests use synthetic activities with the real dispatcher and answer-page
workflow: a 25-hour wait, worker restart, stored-schedule migration, skip-overlap,
parent-timeout termination, and reset without repeating drafting or approving.
