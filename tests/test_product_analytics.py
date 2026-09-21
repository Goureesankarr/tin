"""Reviewed analytics resources, synthetic data only; no provider/model calls in CI."""

import math
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "workflow_packages/product.analytics_brief"
RESOURCES = PACKAGE / "skills/product-analytics"


def resource(name):
    path = RESOURCES / name
    blocks = re.findall(r"```python\n(.*?)\n```", path.read_text(), re.S)
    assert len(blocks) == 1
    namespace = {}
    # Only fixed repository resources execute here, never uploaded author code.
    exec(compile(blocks[0], str(path), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture
def statistics():
    return resource("STATISTICS.md")


def test_exact_test_matches_published_reference_and_is_symmetric(statistics):
    fisher = statistics["fisher_exact_two_sided"]
    assert fisher(6, 2, 1, 4) == pytest.approx(0.10256410256410256)
    assert fisher(1, 4, 6, 2) == pytest.approx(fisher(6, 2, 1, 4))
    assert fisher(2, 6, 4, 1) == pytest.approx(fisher(6, 2, 1, 4))
    assert fisher(0, 20, 0, 20) == pytest.approx(1)
    assert fisher(10, 0, 0, 10) == pytest.approx(2 / math.comb(20, 10))
    assert fisher(0, 0, 0, 0) is None
    assert fisher(0, 0, 4, 8) is None


@pytest.mark.parametrize("invalid", [-1, True, 1.5, float("nan"), "4"])
def test_stats_reject_invalid_counts(statistics, invalid):
    with pytest.raises(ValueError):
        statistics["fisher_exact_two_sided"](invalid, 2, 3, 4)


def test_screen_corrects_whole_family_and_rejects_small_samples(statistics):
    assert statistics["holm"]([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert statistics["holm"]([]) == []
    rows = statistics["screen_comparisons"]([(40, 10, 5, 45), (0, 0, 3, 7), (10, 0, 0, 10)])
    assert rows[0]["supported"] is True
    assert rows[0]["difference_pp"] == pytest.approx(70)
    # Unavailable and small-sample comparisons still belong to the correction family.
    assert rows[0]["adjusted_p"] == pytest.approx(3 * rows[0]["p"])
    assert rows[1]["p"] is None and rows[1]["supported"] is False
    assert rows[2]["p"] < 0.05 and rows[2]["supported"] is False
    assert statistics["fisher_exact_two_sided"](10001, 10001, 10001, 10001) is None
    assert statistics["fisher_exact_two_sided"](1, 1000000, 1, 1000000) is None


@pytest.mark.parametrize("p", [float("nan"), float("inf"), -0.1, 1.01, True])
def test_holm_refuses_invalid_values(statistics, p):
    with pytest.raises(ValueError):
        statistics["holm"]([p])


def analytics():
    namespace = resource("STATISTICS.md")
    path = RESOURCES / "CALCULATIONS.md"
    block = re.findall(r"```python\n(.*?)\n```", path.read_text(), re.S)[0]
    exec(compile(block, str(path), "exec"), namespace)  # noqa: S102
    return namespace


def fixture(name):
    import json

    return json.loads((ROOT / f"tests/fixtures/product_analytics/{name}.json").read_text())


def plan(name="accounts"):
    data = fixture(name)
    profile = data["profile"]
    return {
        "version": "reusable-v1",
        "actor_key": profile["actor"].replace("properties.", "event:"),
        "actor_label": profile["unit"],
        "chain_key": profile["attempt"].replace("properties.", "event:"),
        "chain_label": "sessions" if name == "website" else "attempts",
        "steps": profile["events"],
        "labels": profile["labels"],
        "key_events": [profile["events"][0], profile["events"][-1]],
        "error_events": ["job_failed"],
        "pageview_event": "$pageview" if name == "website" else "",
        "path_property": "$pathname" if name == "website" else "",
        "source_property": "$referring_domain" if name == "website" else "",
        "paths": ["/start"] if name == "website" else [],
        "sources": ["search.example"] if name == "website" else [],
        "category_property": "$device_type" if name == "website" else "",
        "categories": ["Desktop", "Mobile"] if name == "website" else [],
        "exclusions": [],
        "semantic_evidence": ["Synthetic documented event and identity semantics."],
        "traffic_actor_key": "distinct_id",
        "traffic_chain_key": "event:$session_id",
    }


def windows():
    return analytics()["settings"]({"posthog_project_id": "101", "as_of_utc": "2026-01-12"})[1]


@pytest.mark.parametrize("name", ["accounts", "website"])
def test_ordered_population_excludes_ties_cross_attempts_and_later_only(name):
    from datetime import datetime

    a, p, data = analytics(), plan(name), fixture(name)
    a["validate_plan"](p)

    def field(row, key):
        return row["distinct_id"] if key == "distinct_id" else row["properties"].get(key[6:])

    rows = [
        (
            "current",
            field(r, p["actor_key"]),
            field(r, p["chain_key"]),
            r["event"],
            datetime.fromisoformat(r["timestamp"]).timestamp(),
        )
        for r in data["events"]
    ]
    result = a["reference_funnel"](rows, p["steps"])
    assert result["current"]["counts"] == data["expected_ordered"]
    assert result["current"]["medians"] == [float(i) for i in range(1, len(p["steps"]))]


def test_inputs_freeze_complete_utc_windows_and_separate_semantics_binding():
    from datetime import UTC, datetime

    a = analytics()
    inputs = {"posthog_project_id": "101"}
    _, w, binding = a["settings"](inputs, datetime(2026, 1, 12, 11, tzinfo=UTC))
    assert [x.isoformat() for x in w] == [
        "2025-12-29T00:00:00+00:00",
        "2026-01-05T00:00:00+00:00",
        "2026-01-12T00:00:00+00:00",
    ]
    assert a["settings"]({**inputs, "as_of_utc": "2026-01-19"})[2] == binding
    assert a["settings"]({**inputs, "event_mapping": "Changed meaning"})[2] != binding
    for change in [
        {"posthog_project_id": "../2"},
        {"posthog_project_id": 101},
        {"reporting_days": True},
        {"reporting_days": 32},
        {"as_of_utc": "yesterday"},
        {"project_id": "cannot override"},
    ]:
        with pytest.raises(ValueError):
            a["settings"]({**inputs, **change})


def test_queries_are_bounded_and_provider_project_bound():
    import json

    a, p = analytics(), plan("website")
    c = {"posthog_project_id": "101"}
    for step in ["inventory", "coverage", "trends", "funnel", "dimensions", "traffic", "breakdown"]:
        request = a["request"](c, step, p, windows())
        assert request["path"] == "/api/projects/101/query/"
        assert request["method"] == "POST"
        assert len(json.dumps(request).encode()) <= 16000
        assert "LIMIT" in request["body"]["query"]["query"]
    p["steps"] = [f"step {i}" for i in range(6)]
    p["labels"] = list(p["steps"])
    query = a["request"](c, "funnel", p, windows())["body"]["query"]["query"]
    assert "n6" in query and "median_d1_6" in query
    assert a["literal"]("I'm here") == "'I\\'m here'"
    for key in ["email;DELETE", "person:email", "event:bad.key", None]:
        with pytest.raises(ValueError):
            a["identity"](key)


def test_exclusions_preserve_types_null_inclusion_and_do_not_publish_values():
    a, p = analytics(), plan()
    rules = [
        {"property": "person:email", "value": "team@example.test"},
        {"property": "is_test", "value": True},
    ]
    assert a["exclusions"]([{"property": "tier", "value": 1}]) != a["exclusions"](
        [{"property": "tier", "value": "1"}]
    )
    assert "NOT coalesce" in a["exclusions"](rules)
    assert "person.properties" in a["exclusions"](rules)
    p["exclusions"] = rules
    pin = a["plan_state"](None, "binding", p, {})["pin"]
    public = a["public_pin"](pin)
    assert "team@example.test" not in str(public)
    assert a["restore_pin"](public, "binding", rules) == pin
    with pytest.raises(ValueError, match="binding"):
        a["restore_pin"](public, "another", rules)
    with pytest.raises(ValueError, match="exclusions"):
        a["restore_pin"](public, "binding", [])
    query = a["request"]({"posthog_project_id": "101"}, "inventory", p, windows())
    assert "team@example.test" not in a["safe_sql"](query["body"]["query"]["query"], rules)


def test_repeat_keeps_labels_and_saved_input_edits_explicitly_reconfigure():
    from copy import deepcopy

    a, p = analytics(), plan()
    first = a["plan_state"](None, "binding", p, {"event": "string"})
    assert first["state"] == "provisional"
    previous = deepcopy(first["pin"])
    assert a["plan_state"](previous, "binding", p, {"event": "string"})["state"] == "pinned"
    proposed = deepcopy(p)
    proposed["labels"][0] = "Different meaning"
    changed = a["plan_state"](previous, "binding", proposed, {"event": "string"})
    assert changed["state"] == "schema changed" and changed["pin"] == previous
    assert changed["changes"] == ["plan.labels"]
    assert a["plan_state"](previous, "new binding", proposed, {})["state"] == "reconfigured"


@pytest.mark.parametrize(
    "bad",
    [
        {"error": "failed"},
        {"hasMore": True},
        {"next": "page2"},
        {"is_partial": True},
        {"query_status": {"complete": False}},
        {"columns": ["unexpected"]},
        {"results": [[1, 2]]},
        {"results": [[1]] * 2},
    ],
)
def test_plausible_but_unusable_provider_result_is_rejected(bad):
    with pytest.raises(ValueError):
        analytics()["table"]({"columns": ["n"], "results": [[1]], **bad}, ["n"], 2)


def test_cached_complete_results_are_usable_and_zero_is_not_failure():
    a = analytics()
    assert a["table"]({"columns": ["n"], "results": [], "is_cached": True}, ["n"], 2) == []
    assert a["ratio"](0, 0) is None
    assert a["change"](5, 0) == {"current": 5, "prior": 0, "delta": 5, "relative_pct": None}
    totals, rows = a["reconcile_funnel"]([], plan(), windows(), {})
    assert totals == {"prior": [0, 0, 0], "current": [0, 0, 0]}
    assert len(rows) == 14 and all(r["median_d1_3"] is None for r in rows)


def test_funnel_checks_reject_monotone_but_wrong_counts():
    a, p = analytics(), plan()
    row = {
        "period": "current",
        "day": "2026-01-05",
        "n1": 5,
        "n2": 3,
        "n3": 2,
        "median_d1_2": 1,
        "median_d1_3": 2,
        "median_d2_3": 1,
        "order_violations": 0,
        "duplicate_choices": 0,
    }
    cov = {
        ("current", event): {"eligible_actors": n}
        for event, n in zip(p["steps"], [6, 7, 2], strict=True)
    }
    with pytest.raises(ValueError, match="coverage mismatch"):
        a["reconcile_funnel"]([row], p, windows(), cov)
    for changes in [
        {"n2": 6},
        {"median_d1_3": float("nan")},
        {"order_violations": 1},
        {"duplicate_choices": 1},
        {"day": "2026-01-12"},
        {"n1": True},
    ]:
        with pytest.raises(ValueError):
            a["validate_funnel"]([{**row, **changes}], p, windows())


def test_missing_breakdown_categories_stay_in_declared_test_family():
    a, p = analytics(), plan("website")
    result = a["screen"]([{"category": "Desktop", "total": 40, "converted": 20}], p)
    assert result["family_size"] == 2
    assert result["results"][1]["category"] == "Mobile"
    assert result["results"][1]["p"] is None
    assert not any(x["supported"] for x in result["results"])


def test_website_keys_are_independent_from_account_funnel():
    a, p = analytics(), plan()
    p.update(
        {
            k: plan("website")[k]
            for k in ["pageview_event", "path_property", "source_property", "paths", "sources"]
        }
    )
    a["validate_plan"](p)
    sql = a["request"]({"posthog_project_id": "101"}, "traffic", p, windows())["body"]["query"][
        "query"
    ]
    assert "account_id" not in sql and "job_id" not in sql
    assert "$session_id" in sql and "distinct_id" in sql
    p["steps"][0] = "$pageview"
    with pytest.raises(ValueError, match="consistent identity"):
        a["validate_plan"](p)


async def test_registered_package_publishes_pinned_resources_and_run_owned_reports():
    from uuid import uuid4

    from test_public_workflows import PublishedSnapshots
    from test_registry_recipe_publication import WIKI, catalog_database

    from tin_lite import catalog, public_workflows
    from tin_lite.procedures import load_pinned_codex_procedure

    entry = next(w for w in public_workflows.PUBLIC_WORKFLOWS if w.key == "product.analytics_brief")
    db, storage = catalog_database(), PublishedSnapshots()
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    row = db.rows[entry.id]
    assert row.definition["schedule_modes"] == ["on_demand", "daily", "weekly"]
    procedure = await load_pinned_codex_procedure(
        storage=storage,
        repo_id=row.definition_repo_id,
        commit_sha=row.current_commit_sha,
        definition_path=row.definition_path,
    )
    run_id = uuid4()
    pinned = procedure.resolve_inputs(inputs={}, run_id=run_id)
    assert pinned.output_path == f"reports/analytics/{run_id}.md"
    assert pinned.resolve_inputs(inputs={}, run_id=run_id).output_path == pinned.output_path
    assert procedure.resolve_inputs(inputs={}, run_id=uuid4()).output_path != pinned.output_path
    assert procedure.services[0].provider_key == "custom.api.posthog"
    assert procedure.services[0].max_calls == 8
    assert procedure.sandbox.egress == "fenced"
    assert (
        procedure.skill_files["product-analytics/CALCULATIONS.md"]
        == (RESOURCES / "CALCULATIONS.md").read_bytes()
    )
    first = row.current_commit_sha
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    assert db.rows[entry.id].current_commit_sha == first


@pytest.mark.parametrize(
    "path,media_type",
    [
        ("content/drafts/{run_id}.md", "text/markdown"),
        ("reports/../wiki/{run_id}.md", "text/markdown"),
        ("reports/analytics/{run_id}.json", "application/json"),
        ("reports/analytics/{slug}.md", "text/markdown"),
        ("reports/analytics/{run_id}.md", "application/json"),
    ],
)
def test_generic_report_paths_do_not_bypass_existing_output_contracts(path, media_type):
    import json

    from tin_lite.procedures import validate_codex_procedure_definition

    definition = json.loads((PACKAGE / "workflow.json").read_text())["definition"]
    definition["procedure"]["output"].update(path_template=path, media_type=media_type)
    with pytest.raises(ValueError):
        validate_codex_procedure_definition(definition)


async def test_package_uses_shared_qualification_and_diagnostic_fails_normal_case():
    from tin_lite.workflow_qualification import Qualification, assess_output, check_package

    contract = Qualification.model_validate_json(
        (ROOT / "workflow_evals/product.analytics_brief/qualification.json").read_bytes()
    )
    files = {str(p.relative_to(ROOT)): p.read_bytes() for p in PACKAGE.rglob("*") if p.is_file()}
    checked = await check_package(
        files, "workflow_packages/product.analytics_brief/workflow.json", contract
    )
    assert checked["cost"]["basis"] == "unmeasured"
    assert checked["cost"]["expected_range_usd"] is None
    assert checked["cost"]["configured_ceiling_usd"] == "5"
    assert checked["safety"]["status"] == "review_required"
    # API below is deliberately asserted through its public existing contract.
    case = next(c for c in contract.cases if c.id == "ordinary_web")
    text = "\n".join(case.expect.contains).replace("Status: complete", "Status: incomplete")
    result = assess_output(case, status="succeeded", content=text.encode())
    assert result["status"] == "failed"


@pytest.mark.parametrize("name", ["accounts", "website"])
def test_provider_fixtures_reconcile_exact_generated_queries(name):
    import hashlib

    a, p = analytics(), plan(name)
    saved = fixture("provider_results")["cases"][name]
    rows = {}
    for step, recorded in saved.items():
        query = a["request"]({"posthog_project_id": "101"}, step, p, windows())["body"]["query"][
            "query"
        ]
        assert hashlib.sha256(query.encode()).hexdigest() == recorded["query_sha256"]
        rows[step] = a["table"](recorded["data"], *a["query_columns"](step, p))
    _, keys = a["coverage"](p, windows())
    cov = a["validate_coverage"](rows["coverage"], a["event_list"](p), windows(), keys)
    a["reconcile_coverage"](cov, rows["inventory"], p, windows())
    totals, days = a["reconcile_funnel"](rows["funnel"], p, windows(), cov)
    assert totals["current"] == fixture(name)["expected_ordered"]
    display = a["funnel_display"](days, p)
    day = [r for r in display if r["selected_start_day"] == "2026-01-05"]
    assert day[1]["of_previous_pct"] == 50
    assert day[2]["of_first_pct"] == pytest.approx(100 / 3)
    assert day[2]["median_from_first_seconds"] == 2
    _, comparisons = a["validate_trends"](rows["trends"], p, windows(), cov)
    assert comparisons[p["key_events"][0]]["current"] == 7  # raw events, not six actors
    if name == "website":
        assert a["validate_traffic"](rows["traffic"], p, cov)["current"] == [6, 7]
        assert a["validate_breakdown"](rows["breakdown"], p, totals["current"]) == (6, 2)
        assert a["validate_dimensions"](rows["dimensions"], p) == {
            "categories": ["Desktop"],
            "paths": ["/start"],
            "sources": ["search.example"],
        }


@pytest.mark.parametrize("name", ["six", "empty", "exclusions", "mixed"])
def test_provider_boundary_fixtures(name):
    import hashlib

    a = analytics()
    case = fixture("provider_edges")["cases"][name]
    p, rows = case["plan"], {}
    for step, response in case["responses"].items():
        query = a["request"]({"posthog_project_id": "101"}, step, p, windows())["body"]["query"][
            "query"
        ]
        assert hashlib.sha256(query.encode()).hexdigest() == response["query_sha256"]
        rows[step] = a["table"](response["data"], *a["query_columns"](step, p))
    if name in {"six", "empty"}:
        totals = a["validate_funnel"](rows["funnel"], p, windows())
        assert totals["current"] == ([1] * 6 if name == "six" else [0] * 3)
        return
    _, keys = a["coverage"](p, windows())
    cov = a["validate_coverage"](rows["coverage"], a["event_list"](p), windows(), keys)
    if name == "exclusions":
        a["reconcile_coverage"](cov, rows["inventory"], p, windows())
        row = cov[("current", p["steps"][0])]
        # Only boolean true excluded: false, string "true", null and integer 1 remain.
        assert row["raw"] == 4 and row["missing_p0"] == 1
    else:
        assert cov[("current", p["steps"][0])]["eligible_actors"] == 6
        assert a["validate_traffic"](rows["traffic"], p, cov)["current"] == [6, 7]


@pytest.mark.parametrize(
    "value",
    [
        "customer@example.test",
        "/users/123456",
        "Other",
        "https://private.test/",
        "01234567-abcd",
        None,
    ],
)
def test_configured_dimensions_have_the_same_safe_labels_as_discovery(value):
    a, p = analytics(), plan("website")
    p["categories"] = [value]
    with pytest.raises(ValueError):
        a["validate_plan"](p)
    with pytest.raises(ValueError):
        a["validate_dimensions"](
            [{"kind": "categories", "value": value, "volume": 2}], plan("website")
        )
