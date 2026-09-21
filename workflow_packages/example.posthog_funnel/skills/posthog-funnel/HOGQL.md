# Ordered funnel implementation

Use the Python below verbatim inside the isolated procedure. Do not ask a model to reimplement
this fixed algorithm. `funnel_tail(stage_events)` appends CTEs and the final SELECT to a
bounded `WITH e AS (...)`. That e CTE selects only `actor`, `attempt`, `event`, exact `timestamp AS t`,
and boolean `eligible`. Build it from validated configuration: select the configured keys,
use both exact window bounds, allowed event names and the validated exclusions, and retain raw
rows while `eligible` checks the types and nonempty keys. Project UUID validity applies only
when project identity semantics require it; run_id is a nonempty string, not necessarily UUID.
Never hardcode the example project's events, IDs or dates. Actor/attempt identifiers stay inside
PostHog. The output is one aggregate row; medians average the two middle observations.

The algorithm groups by actor AND attempt, uses strict timestamp comparisons and selects one
deepest, earliest chain per actor. Missing array elements cannot advance a stage: length checks
own that decision. Empty input yields zero counts and null medians. Coverage decides whether
zero eligible rows mean no events or missing instrumentation. Two and three steps are supported.
Request limits still apply. Do not run the synthetic cases against the provider in ordinary runs;
those belong to separately authorized qualification. Use local synthetic responses to check the
validator and compare the generated SQL against this resource before dispatch.

On HTTP 200 completed results, call `read_funnel(response["data"], len(stage_events))`.
Keep the original columns/results in evidence and report the returned stage rows. Also verify
provider types are numeric for these named columns; reject missing, truncated or malformed data.
An error or inconsistent row is unavailable, never zero. Every required window must pass the ordinary case.

```python
"""Deterministic HogQL after a bounded e CTE: actor, attempt, event, t, eligible."""

import re


def funnel_tail(steps):
    if len(steps) not in (2, 3) or len(set(steps)) != len(steps):
        raise ValueError("two or three distinct stages required")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", ev) for ev in steps):
        raise ValueError("unsupported event name")
    n = len(steps)
    ctes = [
        "b AS (SELECT DISTINCT actor,attempt,event,t FROM e WHERE eligible)",
        "stages AS (SELECT actor,attempt,"
        + ",".join(
            f"arraySort(groupArrayIf(t,event='{ev}')) AS a{i}" for i, ev in enumerate(steps, 1)
        )
        + " FROM b GROUP BY actor,attempt)",
        "firsts AS (SELECT *,arrayElement(a1,1) AS t1 FROM stages WHERE length(a1)>0)",
        "seconds AS (SELECT *,arrayFilter(x -> x>t1,a2) AS later2 FROM firsts)",
    ]
    if n == 3:
        ctes += [
            "thirds AS (SELECT *,arrayElement(later2,1) AS t2,arrayFilter(x -> length(later2)>0 AND x>arrayElement(later2,1),a3) AS later3 FROM seconds)",
            "attempts AS (SELECT actor,attempt,t1,t2,arrayElement(later3,1) AS t3,multiIf(length(later3)>0,3,length(later2)>0,2,1) AS depth FROM thirds)",
        ]
    else:
        ctes += [
            "attempts AS (SELECT actor,attempt,t1,arrayElement(later2,1) AS t2,if(length(later2)>0,2,1) AS depth FROM seconds)"
        ]
    times = ",".join(f"t{i}" for i in range(1, n + 1))
    ctes += [
        f"ranked AS (SELECT *,row_number() OVER(PARTITION BY actor ORDER BY depth DESC,{times},attempt) AS choice FROM attempts)",
        "chosen AS (SELECT * FROM ranked WHERE choice=1)",
    ]
    pairs = [(1, 2)] + ([(1, 3), (2, 3)] if n == 3 else [])
    counts = ["count() AS n1"] + [f"countIf(depth>={i}) AS n{i}" for i in range(2, n + 1)]
    durations = [
        f"arraySort(groupArrayIf(dateDiff('microsecond',t{a},t{b})/1000000.0,depth>={b})) AS d{a}{b}"
        for a, b in pairs
    ]
    violations = " OR ".join(f"(depth>={i} AND t{i}<=t{i - 1})" for i in range(2, n + 1))
    ctes += [
        "metrics AS (SELECT "
        + ",".join(
            counts
            + durations
            + [
                f"countIf({violations}) AS order_violations",
                "count()-uniqExact(actor) AS duplicate_choices",
            ]
        )
        + " FROM chosen)"
    ]
    raw = []
    for i, ev in enumerate(steps, 1):
        raw += [
            f"countIf(event='{ev}') AS raw{i}",
            f"countIf(event='{ev}' AND eligible) AS eligible{i}",
            f"uniqExactIf(actor,event='{ev}' AND eligible) AS actors{i}",
        ]
    ctes += ["raw AS (SELECT " + ",".join(raw) + " FROM e)"]
    cols = ["raw.*"] + [f"metrics.n{i}" for i in range(1, n + 1)]
    for a, b in pairs:
        cols.append(
            f"if(metrics.n{b}=0,NULL,(arrayElement(metrics.d{a}{b},intDiv(metrics.n{b}+1,2))+arrayElement(metrics.d{a}{b},intDiv(metrics.n{b}+2,2)))/2.0) AS median_d{a}{b}"
        )
    cols += ["metrics.order_violations", "metrics.duplicate_choices"]
    return ",\n" + ",\n".join(ctes) + "\nSELECT " + ",".join(cols) + " FROM raw CROSS JOIN metrics"


def read_funnel(data, stages):
    import math

    columns = data.get("columns")
    rows = data.get("results")
    expected = [
        name for i in range(1, stages + 1) for name in (f"raw{i}", f"eligible{i}", f"actors{i}")
    ]
    expected += [f"n{i}" for i in range(1, stages + 1)]
    expected += ["median_d12"] + (["median_d13", "median_d23"] if stages == 3 else [])
    expected += ["order_violations", "duplicate_choices"]
    if (
        columns != expected
        or not isinstance(rows, list)
        or len(rows) != 1
        or len(rows[0]) != len(columns)
    ):
        raise ValueError("unexpected funnel result schema")
    if data.get("error") or data.get("hasMore") or data.get("query_status"):
        raise ValueError("incomplete provider result")
    r = dict(zip(columns, rows[0]))
    for name in expected:
        if not name.startswith("median_") and (type(r[name]) is not int or r[name] < 0):
            raise ValueError("invalid count")
    if r["order_violations"] or r["duplicate_choices"]:
        raise ValueError("invalid ordered chains")
    result = []
    for i in range(1, stages + 1):
        n = r[f"n{i}"]
        if not n <= r[f"actors{i}"] <= r[f"eligible{i}"] <= r[f"raw{i}"]:
            raise ValueError("inconsistent stage counts")
        if (i == 1 and n != r["actors1"]) or (i > 1 and n > r[f"n{i - 1}"]):
            raise ValueError("nonmonotonic funnel")
        first = (0.0 if n else None) if i == 1 else r[f"median_d1{i}"]
        previous = None if i == 1 else r[f"median_d{i - 1}{i}"]
        for v in (first, previous):
            if v is not None and (
                type(v) not in (int, float) or not math.isfinite(v) or v < 0 or n == 0
            ):
                raise ValueError("invalid median")
        if i > 1 and n and (first is None or previous is None or first <= 0 or previous <= 0):
            raise ValueError("missing or non-positive duration")
        result.append(
            dict(
                step=i,
                raw_events=r[f"raw{i}"],
                eligible_events=r[f"eligible{i}"],
                raw_actors=r[f"actors{i}"],
                chain_actors=n,
                median_from_first=first,
                median_from_previous=previous,
                of_previous_pct=None if i == 1 or r[f"n{i - 1}"] == 0 else 100 * n / r[f"n{i - 1}"],
                of_first_pct=None if r["n1"] == 0 else 100 * n / r["n1"],
                order_violations=0,
                duplicate_choices=0,
            )
        )
    return result
```
