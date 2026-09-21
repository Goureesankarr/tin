from __future__ import annotations

import asyncio
import copy
import json
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from temporalio.exceptions import ApplicationError
from test_procedure_publication import publication_db as publication_db
from test_visibility_audit import (
    FakeDatabase,
    FakeResponses,
    FakeStorage,
    activity_fixture,
    adjudication,
    panel,
    response_text,
)

from tin_lite.activities import TinActivities
from tin_lite.domain import EffectReceipt, RunStatus
from tin_lite.usage_capture import begin_observation, observation_key, observe_response
from tin_lite.visibility import (
    MAX_VISIBILITY_RESPONSE_BYTES,
    VisibilityAuditor,
    VisibilityProtocolError,
    read_visibility_response_checkpoint,
    visibility_response_checkpoint,
)


@pytest.mark.parametrize(
    ("returned", "canonical"),
    [
        ("virvid.app", "virvid.app"),
        ("  VIRVID.APP  ", "virvid.app"),
        ("https://virvid.app/", "virvid.app"),
        ("HTTP://VIRVID.APP/product?ref=a#intro", "virvid.app"),
        ("//www.virvid.app/", "www.virvid.app"),
        ("virvid.app/docs", "virvid.app"),
        ("https://virvid.app:443/", "virvid.app"),
        ("https://virvid.app./", "virvid.app"),
    ],
)
async def test_panel_normalizes_domain_without_changing_identity(returned, canonical):
    value = panel()
    value["target"]["domain"] = returned
    responses = FakeResponses(response_text("panel", json.dumps(value)))
    auditor = VisibilityAuditor(responses=responses, skill_suite="rules")
    prepared = await auditor.prepare_panel(
        project_name="Container", target_request="https://virvid.app", sources=[]
    )
    assert prepared["target"]["domain"] == canonical
    schema = responses.payloads[0]["text"]["format"]["schema"]
    Draft202012Validator.check_schema(schema)
    value["target"]["domain"] = canonical
    Draft202012Validator(schema).validate(value)
    value["target"]["domain"] = "https://virvid.app/"
    assert list(Draft202012Validator(schema).iter_errors(value))


@pytest.mark.parametrize(
    "domain",
    [
        None,
        12,
        "unknown",
        "not a domain",
        "ftp://virvid.app",
        "javascript:virvid.app",
        "https://name@virvid.app",
        "https://virvid.app:bad-secret",
        "https://virvid.app:65536",
        "https://virvid.app\\@other.app",
        "https://vir\nvid.app",
        "127.0.0.1",
        "https://[::1]",
        "virvid.app..",
        "a" * 2049,
    ],
)
async def test_panel_rejects_ambiguous_or_invalid_domain(domain):
    value = panel()
    value["target"]["domain"] = domain
    auditor = VisibilityAuditor(
        responses=FakeResponses(response_text("panel", json.dumps(value))), skill_suite="rules"
    )
    with pytest.raises(VisibilityProtocolError, match="target domain is invalid"):
        await auditor.prepare_panel(
            project_name="Container", target_request="this project", sources=[]
        )


@pytest.mark.parametrize("domain", ["https://other.app/", "https://virvid.app.other.app/"])
async def test_url_normalization_still_rejects_a_different_target(domain):
    value = panel()
    value["target"]["domain"] = domain
    auditor = VisibilityAuditor(
        responses=FakeResponses(response_text("panel", json.dumps(value))), skill_suite="rules"
    )
    with pytest.raises(VisibilityProtocolError, match="different target domain"):
        await auditor.prepare_panel(
            project_name="Container", target_request="virvid.app", sources=[]
        )


async def test_explicit_url_with_root_dot_still_binds_its_hostname():
    auditor = VisibilityAuditor(
        responses=FakeResponses(response_text("panel", json.dumps(panel()))), skill_suite="rules"
    )
    with pytest.raises(VisibilityProtocolError, match="different target domain"):
        await auditor.prepare_panel(
            project_name="Container", target_request="https://other.app./virvid.app", sources=[]
        )


class ObservedResponses(FakeResponses):
    async def create(self, payload):
        observation = await begin_observation("openai", "model", "responses", request=payload)
        response = await super().create(payload)
        await observe_response(observation, response)
        return response


def effect(db, response, *, stage="panel"):
    run_id = uuid4()
    responses = ObservedResponses(response)
    auditor = VisibilityAuditor(responses=responses, skill_suite="rules")
    activities = object.__new__(TinActivities)
    activities._db = db

    async def execute(checkpoint):
        if stage == "panel":
            return await auditor.prepare_panel(
                project_name="Container",
                target_request="virvid.app",
                sources=[],
                checkpoint=checkpoint,
            )
        if stage == "adjudication":
            return await auditor.adjudicate(panel=panel(), measurements=[], checkpoint=checkpoint)
        return await auditor.answer(question="Which tool?", searched=True, checkpoint=checkpoint)

    async def invoke():
        return await activities._visibility_effect(
            run_id=run_id, step_id=stage, operation=f"visibility_{stage}", execute=execute
        )

    return run_id, responses, invoke


@pytest.mark.parametrize("stage", ["panel", "answer", "adjudication"])
async def test_rejected_response_replays_original_error_without_another_call(publication_db, stage):
    if stage == "panel":
        value = panel()
        value["target"]["domain"] = "https://other.app/"
        response, error = response_text("panel", json.dumps(value)), "different target domain"
    elif stage == "answer":
        response, error = response_text("answer", "No search was used."), "did not search"
    else:
        response, error = response_text("adjudication", "not JSON"), "not JSON"
    run_id, responses, invoke = effect(publication_db, response, stage=stage)
    for _ in range(2):
        with pytest.raises(ApplicationError, match=error) as failure:
            await invoke()
        assert failure.value.non_retryable and failure.value.type == "VisibilityProtocolError"
    assert len(responses.payloads) == 1
    result = await publication_db.get_effect(f"{run_id}:visibility:{stage}")
    assert result.status == "failed" and error in result.error_message
    saved = await publication_db.get_effect(f"{run_id}:visibility:{stage}:response")
    assert saved.status == "completed" and saved.result["response"]["id"] == stage
    usage = await publication_db.get_effect(
        observation_key(run_id, f"visibility:{stage}", "responses")
    )
    assert usage.status == "completed" and usage.result["outcome"] == "response_received"
    assert "output" not in usage.result and "response" not in usage.result


@pytest.mark.parametrize("failure_point", ["response_ack", "parent_write", "cancel_after_response"])
async def test_checkpoint_recovers_interrupted_validation_or_lost_ack(
    publication_db, monkeypatch, failure_point
):
    value = panel()
    value["target"]["domain"] = "https://virvid.app/"
    run_id, responses, invoke = effect(publication_db, response_text("panel", json.dumps(value)))
    parent_key = f"{run_id}:visibility:panel"
    original = publication_db.complete_effect
    interrupted = False

    async def complete(conn, *, execution_key, result):
        nonlocal interrupted
        if not interrupted and failure_point == "parent_write" and execution_key == parent_key:
            interrupted = True
            raise OSError("lost connection before parent write")
        await original(conn, execution_key=execution_key, result=result)
        if (
            not interrupted
            and execution_key == parent_key + ":response"
            and failure_point != "parent_write"
        ):
            interrupted = True
            if failure_point == "cancel_after_response":
                raise asyncio.CancelledError()
            raise OSError("lost response checkpoint acknowledgment")

    monkeypatch.setattr(publication_db, "complete_effect", complete)
    with pytest.raises(
        asyncio.CancelledError if failure_point == "cancel_after_response" else OSError
    ):
        await invoke()
    prepared = await invoke()
    assert prepared["target"]["domain"] == "virvid.app"
    assert prepared == await invoke()
    assert len(responses.payloads) == 1
    assert (await publication_db.get_effect(parent_key)).status == "completed"


async def test_lost_response_checkpoint_does_not_repurchase(publication_db, monkeypatch):
    run_id, responses, invoke = effect(publication_db, response_text("panel", json.dumps(panel())))
    original = publication_db.complete_effect

    async def complete(conn, *, execution_key, result):
        if execution_key.endswith(":response"):
            raise OSError("response checkpoint was not saved")
        await original(conn, execution_key=execution_key, result=result)

    monkeypatch.setattr(publication_db, "complete_effect", complete)
    with pytest.raises(OSError):
        await invoke()
    with pytest.raises(ApplicationError, match="could not be recovered") as failure:
        await invoke()
    assert failure.value.non_retryable and failure.value.type == "VisibilityRecoveryError"
    assert len(responses.payloads) == 1
    usage = await publication_db.get_effect(
        observation_key(run_id, "visibility:panel", "responses")
    )
    assert usage.status == "completed"


async def test_legacy_observed_attempt_without_checkpoint_stays_unpurchased(publication_db):
    run_id, responses, invoke = effect(publication_db, response_text("panel", json.dumps(panel())))
    key = observation_key(run_id, "visibility:panel", "responses")
    async with publication_db.effect_lock(key, "external_usage_v1") as (conn, _):
        await publication_db.start_effect(conn, execution_key=key, operation="external_usage_v1")
    with pytest.raises(ApplicationError, match="already attempted without a recoverable response"):
        await invoke()
    assert responses.payloads == []
    assert (await publication_db.get_effect(key)).status == "started"


async def test_concurrent_panel_execution_purchases_once(publication_db):
    run_id, responses, invoke = effect(publication_db, response_text("panel", json.dumps(panel())))
    first, second = await asyncio.gather(invoke(), invoke())
    assert first == second and len(responses.payloads) == 1
    assert (await publication_db.get_effect(f"{run_id}:visibility:panel")).status == "completed"


async def test_oversized_response_saves_bounded_failure_before_rejecting(publication_db):
    run_id, responses, invoke = effect(
        publication_db, response_text("panel", "x" * (MAX_VISIBILITY_RESPONSE_BYTES + 1))
    )
    for _ in range(2):
        with pytest.raises(ApplicationError, match="exceeds its checkpoint limit"):
            await invoke()
    saved = await publication_db.get_effect(f"{run_id}:visibility:panel:response")
    assert saved.status == "completed" and len(json.dumps(saved.result)) < 200
    assert "response" not in saved.result and len(responses.payloads) == 1


async def test_checkpoint_excludes_reasoning_and_preserves_answer_evidence():
    response = response_text("answer", "A useful recommendation.", searched=True)
    response["usage"].update(
        input_tokens_details={"cached_tokens": 2}, output_tokens_details={"reasoning_tokens": 3}
    )
    original = copy.deepcopy(response)
    response["instructions"] = "PRIVATE INPUT"
    response["output"].append(
        {"type": "reasoning", "summary": "PRIVATE REASONING", "encrypted_content": "PRIVATE"}
    )
    checkpoint = visibility_response_checkpoint(response)
    assert "PRIVATE" not in json.dumps(checkpoint)
    restored = read_visibility_response_checkpoint(checkpoint)
    before = await VisibilityAuditor(responses=FakeResponses(original), skill_suite="rules").answer(
        question="Which tool?", searched=True
    )
    after = await VisibilityAuditor(responses=FakeResponses(restored), skill_suite="rules").answer(
        question="Which tool?", searched=True
    )
    assert before == after


@pytest.mark.parametrize("bad_answer", [False, True])
async def test_real_auditor_activity_finishes_all_calls_before_success_or_failure(
    monkeypatch, bad_answer
):
    project, run = activity_fixture()

    class Database(FakeDatabase):
        async def save_effect_progress(self, conn, *, execution_key, result):
            receipt = self.receipts[execution_key]
            self.receipts[execution_key] = EffectReceipt(
                execution_key, receipt.operation, "started", result
            )

    class Responses:
        model = "test-model"
        calls = 0
        pending = 0

        async def create(self, payload):
            observation = await begin_observation("openai", "model", "responses", request=payload)
            self.calls += 1
            self.pending += 1
            try:
                schema_name = payload.get("text", {}).get("format", {}).get("name")
                if schema_name == "visibility_panel":
                    value = panel()
                    value["target"]["domain"] = "https://virvid.app/"
                    response = response_text("panel", json.dumps(value))
                elif schema_name == "visibility_adjudication":
                    response = response_text("score", json.dumps(adjudication()))
                else:
                    # The first answer can fail while other paid calls are outstanding.
                    is_bad = bad_answer and self.calls == 2
                    await asyncio.sleep(0 if is_bad else 0.01)
                    response = response_text(
                        f"answer-{self.calls}",
                        "" if is_bad else "A useful tool.",
                        searched="tools" in payload,
                    )
                await observe_response(observation, response)
                return response
            finally:
                self.pending -= 1

    database, storage, responses = Database(project=project, run=run), FakeStorage(), Responses()
    activities = TinActivities(
        database=database,
        storage=storage,
        sandboxes=None,
        settings=None,
        visibility_auditor=VisibilityAuditor(responses=responses, skill_suite="rules"),
    )
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda details: None)
    if bad_answer:
        for _ in range(2):
            with pytest.raises(ApplicationError, match="visibility answer is empty"):
                await activities.generate_visibility_audit(str(run.id))
            assert responses.pending == 0 and responses.calls == 11
        assert storage.publishes == 0
    else:
        await activities.generate_visibility_audit(str(run.id))
        await activities.project_visibility_result(str(run.id))
        await activities.generate_visibility_audit(str(run.id))
        await activities.project_visibility_result(str(run.id))
        assert responses.calls == 12 and responses.pending == 0
        assert storage.publishes == database.projection_writes == 1
        assert database.run.status == RunStatus.SUCCEEDED
        evidence = json.loads(storage.documents[f"reports/visibility/{run.id}/evidence.json"])
        assert evidence["panel"]["target"]["domain"] == "virvid.app"
    checkpoints = [r for r in database.receipts.values() if r.operation == "visibility_response_v1"]
    assert len(checkpoints) == responses.calls
    assert all(r.status == "completed" for r in checkpoints)


async def test_completed_legacy_effect_does_not_need_a_response_checkpoint(publication_db):
    run_id, responses, invoke = effect(publication_db, response_text("panel", json.dumps(panel())))
    key = f"{run_id}:visibility:panel"
    async with publication_db.effect_lock(key, "visibility_panel") as (conn, _):
        await publication_db.start_effect(conn, execution_key=key, operation="visibility_panel")
        await publication_db.complete_effect(conn, execution_key=key, result=panel())
    assert await invoke() == panel()
    assert responses.payloads == []
    assert await publication_db.get_effect(key + ":response") is None


async def test_cancelled_provider_call_remains_unknown_and_cannot_be_rebought(publication_db):
    run_id = uuid4()
    dispatched = 0

    class Responses:
        model = "test-model"

        async def create(self, payload):
            nonlocal dispatched
            await begin_observation("openai", "model", "responses", request=payload)
            dispatched += 1
            raise asyncio.CancelledError()

    auditor = VisibilityAuditor(responses=Responses(), skill_suite="rules")
    activities = object.__new__(TinActivities)
    activities._db = publication_db

    async def invoke():
        return await activities._visibility_effect(
            run_id=run_id,
            step_id="panel",
            operation="visibility_panel",
            execute=lambda checkpoint: auditor.prepare_panel(
                project_name="Container",
                target_request="virvid.app",
                sources=[],
                checkpoint=checkpoint,
            ),
        )

    with pytest.raises(asyncio.CancelledError):
        await invoke()
    with pytest.raises(ApplicationError, match="already attempted without a recoverable response"):
        await invoke()
    assert dispatched == 1
    usage = await publication_db.get_effect(
        observation_key(run_id, "visibility:panel", "responses")
    )
    assert usage.status == "started" and usage.result["outcome"] == "unconfirmed"
    assert usage.result["usage"] is None


async def test_admission_rejection_is_not_recorded_as_a_paid_attempt(publication_db, monkeypatch):
    run_id, responses, invoke = effect(publication_db, response_text("panel", json.dumps(panel())))
    original = responses.create
    attempts = 0

    async def admit(payload):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("spending admission rejected before dispatch")
        return await original(payload)

    monkeypatch.setattr(responses, "create", admit)
    with pytest.raises(RuntimeError, match="spending admission rejected before dispatch"):
        await invoke()
    assert await publication_db.get_effect(f"{run_id}:visibility:panel:response") is None
    assert (
        await publication_db.get_effect(observation_key(run_id, "visibility:panel", "responses"))
        is None
    )
    assert responses.payloads == []
    assert (await invoke())["target"]["domain"] == "virvid.app"
    assert attempts == 2 and len(responses.payloads) == 1
