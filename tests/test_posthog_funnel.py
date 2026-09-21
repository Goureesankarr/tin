"""Offline checks of the checked-in recipe; no PostHog requests or model calls."""

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

RESOURCE = (
    Path(__file__).parents[1]
    / "workflow_packages/example.posthog_funnel/skills/posthog-funnel/HOGQL.md"
)


@pytest.fixture
def recipe():
    # This exact reviewed repository resource is executable inside the procedure sandbox.
    # Never use this loader for packages submitted to the qualification service.
    blocks = re.findall(r"```python\n(.*?)\n```", RESOURCE.read_text(), re.S)
    assert len(blocks) == 1
    namespace = {}
    exec(compile(blocks[0], str(RESOURCE), "exec"), namespace)  # noqa: S102 - fixed, reviewed fixture
    return namespace


def response():
    # Synthetic three-stage fixture: raw marginals differ from ordered unique actors.
    columns = [name for i in (1, 2, 3) for name in (f"raw{i}", f"eligible{i}", f"actors{i}")]
    columns += ["n1", "n2", "n3", "median_d12", "median_d13", "median_d23"]
    columns += ["order_violations", "duplicate_choices"]
    return {
        "columns": columns,
        "results": [[10, 10, 8, 11, 11, 8, 10, 10, 8, 8, 6, 5, 1.5, 5.0, 3.0, 0, 0]],
        "error": None,
        "hasMore": False,
        "query_status": None,
    }


def test_exact_aggregate_validation_and_rates(recipe):
    rows = recipe["read_funnel"](response(), 3)
    assert [r["chain_actors"] for r in rows] == [8, 6, 5]
    assert [r["raw_actors"] for r in rows] == [8, 8, 8]
    assert [r["median_from_first"] for r in rows] == [0.0, 1.5, 5.0]
    assert rows[2]["median_from_previous"] == 3.0
    assert rows[2]["of_previous_pct"] == pytest.approx(100 * 5 / 6)
    assert rows[2]["of_first_pct"] == 62.5


def test_generator_preserves_the_provider_qualified_synthetic_query(recipe):
    # A versioned SQL/response pair records the actual dialect check. This is a regression
    # guard, not a live query in CI; SQL changes require separately authorized requalification.
    fixtures = Path(__file__).parent / "fixtures/posthog_funnel"
    query = (fixtures / "ordered.sql").read_text().rstrip()
    events_cte = query.split(",\nb AS", 1)[0]
    steps = ["onboarding_plan_written", "onboarding_approved", "onboarding_set_up"]
    assert events_cte + recipe["funnel_tail"](steps) == query
    observed = json.loads((fixtures / "ordered.json").read_text())
    assert recipe["read_funnel"](observed, 3) == recipe["read_funnel"](response(), 3)


@pytest.mark.parametrize("raw_first", [0, 1])
def test_no_events_and_no_eligible_actor_have_undefined_rates(recipe, raw_first):
    data = response()
    data["results"] = [[raw_first, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, None, None, None, 0, 0]]
    rows = recipe["read_funnel"](data, 3)
    assert all(r["chain_actors"] == 0 for r in rows)
    assert all(r["of_first_pct"] is None and r["of_previous_pct"] is None for r in rows)
    assert all(r["median_from_first"] is None for r in rows)
    assert rows[0]["raw_events"] == raw_first  # Missing identity is distinguishable from no events.


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("n2", 9),  # Unmatched joins must not inflate completion past the event/actor counts.
        ("n3", 7),  # A later prefix cannot exceed its predecessor.
        ("order_violations", 1),
        ("duplicate_choices", 1),
        ("eligible2", 5),
        ("n2", True),
        ("n2", -1),
        ("median_d12", None),
        ("median_d12", 0),
        ("median_d12", -1),
        ("median_d13", float("nan")),
        ("median_d23", float("inf")),
    ],
)
def test_rejects_inconsistent_provider_aggregates(recipe, column, value):
    data = response()
    data["results"][0][data["columns"].index(column)] = value
    with pytest.raises(ValueError):
        recipe["read_funnel"](data, 3)


@pytest.mark.parametrize(
    "change",
    [
        {"error": "query_failed"},
        {"hasMore": True},
        {"query_status": {"complete": False}},
        {"results": []},
        {"results": [[1]]},
        {"columns": ["unexpected"]},
    ],
)
def test_rejects_unavailable_or_malformed_results(recipe, change):
    data = response() | deepcopy(change)
    with pytest.raises(ValueError):
        recipe["read_funnel"](data, 3)


def test_two_stage_results(recipe):
    data = {
        "columns": [
            "raw1",
            "eligible1",
            "actors1",
            "raw2",
            "eligible2",
            "actors2",
            "n1",
            "n2",
            "median_d12",
            "order_violations",
            "duplicate_choices",
        ],
        "results": [[10, 10, 8, 11, 11, 8, 8, 6, 1.0, 0, 0]],
    }
    rows = recipe["read_funnel"](data, 2)
    assert len(rows) == 2 and rows[1]["of_first_pct"] == 75
    assert rows[1]["median_from_first"] == rows[1]["median_from_previous"] == 1.0


@pytest.mark.parametrize("steps", [["one"], ["same", "same"], ["one", "x'); DROP TABLE events"]])
def test_query_builder_rejects_invalid_stage_literals(recipe, steps):
    with pytest.raises(ValueError):
        recipe["funnel_tail"](steps)
