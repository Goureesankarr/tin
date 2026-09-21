# Deterministic analytics resources

Execute this reviewed Python block after STATISTICS.md. All SQL comes from fixed builders.

```python
import datetime as dt
import hashlib
import json
import math
import re
import statistics

VERSION = "reusable-v1"
KEY = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,63}\Z")
RESERVED = {"Unknown", "Ambiguous", "Other"}


def literal(value):
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int and abs(value) <= 10**12:
        return str(value)
    if type(value) is str and 1 <= len(value) <= 256 and not any(ord(c) < 32 for c in value):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    raise ValueError("unsupported scalar literal")


def prop(name, source="properties"):
    if type(name) is not str or not KEY.fullmatch(name):
        raise ValueError("unsupported property")
    if source not in ("properties", "person.properties"):
        raise ValueError("unsupported property source")
    raw = f"JSONExtractRaw({source},{literal(name)})"
    return f"if({raw}='' OR {raw}='null',NULL,if(startsWith({raw},'\"'),JSONExtractString({source},{literal(name)}),{raw}))"


def identity(key):
    if key == "distinct_id":
        return "distinct_id"
    if type(key) is str and key.startswith("event:"):
        name = key[6:]
        prop(name)
        return f"if(startsWith(JSONExtractRaw(properties,{literal(name)}),'\"'),JSONExtractString(properties,{literal(name)}),NULL)"
    raise ValueError("identity must be distinct_id or event:<string property>")


def nonempty(expr):
    return f"(isNotNull({expr}) AND toString({expr})!='')"


def stable_hash(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def settings(inputs, now=None):
    allowed = {"posthog_project_id", "reporting_days", "as_of_utc", "event_mapping", "exclusions"}
    if not isinstance(inputs, dict) or set(inputs) - allowed:
        raise ValueError("unexpected client input")
    c = dict(reporting_days=7, as_of_utc="", event_mapping="", exclusions="")
    c.update(inputs)
    if type(c.get("posthog_project_id")) is not str or not re.fullmatch(
        r"[0-9]{1,20}", c["posthog_project_id"]
    ):
        raise ValueError("invalid provider project ID")
    if type(c["reporting_days"]) is not int or not 1 <= c["reporting_days"] <= 31:
        raise ValueError("invalid reporting_days")
    for k in ("event_mapping", "exclusions"):
        if type(c[k]) is not str or len(c[k]) > 2000:
            raise ValueError("invalid " + k)
    anchor = c["as_of_utc"]
    if type(anchor) is not str:
        raise ValueError("invalid date")
    if anchor:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", anchor):
            raise ValueError("invalid date")
        end = dt.datetime.combine(dt.date.fromisoformat(anchor), dt.time(), dt.timezone.utc)
    else:
        clock = now or dt.datetime.now(dt.timezone.utc)
        if clock.tzinfo is None:
            raise ValueError("naive clock")
        end = clock.astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    current = end - dt.timedelta(days=c["reporting_days"])
    prior = current - dt.timedelta(days=c["reporting_days"])
    # Date/window changes do not silently replace the semantic mapping.
    binding = stable_hash({k: c[k] for k in ("posthog_project_id", "event_mapping", "exclusions")})
    return c, (prior, current, end), binding


def validate_plan(p):
    fields = {
        "version",
        "actor_key",
        "actor_label",
        "chain_key",
        "chain_label",
        "steps",
        "labels",
        "key_events",
        "error_events",
        "pageview_event",
        "path_property",
        "source_property",
        "paths",
        "sources",
        "category_property",
        "categories",
        "exclusions",
        "semantic_evidence",
        "traffic_actor_key",
        "traffic_chain_key",
    }
    if type(p) is not dict or set(p) != fields or p["version"] != VERSION:
        raise ValueError("unsupported plan shape/version")
    identity(p["actor_key"])
    identity(p["chain_key"])
    identity(p["traffic_actor_key"])
    identity(p["traffic_chain_key"])
    if p["actor_key"] == p["chain_key"]:
        raise ValueError("actor and chain keys must be distinct")
    for k in ("actor_label", "chain_label"):
        if type(p[k]) is not str or not 1 <= len(p[k]) <= 120:
            raise ValueError("missing unit")
    for k, lo, hi in (
        ("steps", 0, 6),
        ("key_events", 1, 4),
        ("error_events", 0, 2),
        ("paths", 0, 8),
        ("sources", 0, 8),
        ("categories", 0, 8),
    ):
        a = p[k]
        if (
            type(a) is not list
            or not lo <= len(a) <= hi
            or any(type(x) is not str for x in a)
            or len(a) != len(set(a))
        ):
            raise ValueError("invalid " + k)
        for value in a:
            literal(value)
    if len(p["steps"]) == 1:
        raise ValueError("funnel requires two to six steps")
    if (
        type(p["labels"]) is not list
        or len(p["labels"]) != len(p["steps"])
        or any(type(x) is not str or not 1 <= len(x) <= 120 for x in p["labels"])
    ):
        raise ValueError("invalid labels")
    for k in ("paths", "sources", "categories"):
        for value in p[k]:
            safe_dimension(value)
    for k in ("category_property", "path_property", "source_property"):
        if p[k]:
            prop(p[k])
    if p["pageview_event"]:
        literal(p["pageview_event"])
    if bool(p["pageview_event"]) != bool(p["path_property"] and p["source_property"]):
        raise ValueError("partial traffic mapping")
    if p["category_property"] and re.search(
        r"email|phone|token|secret|password|(?:^|_)id$|name$", p["category_property"], re.I
    ):
        raise ValueError("identifying breakdown property")
    if p["category_property"] and not p["steps"]:
        raise ValueError("breakdown requires activation cohort")
    if (
        type(p["semantic_evidence"]) is not list
        or not 1 <= len(p["semantic_evidence"]) <= 12
        or any(type(x) is not str or not 1 <= len(x) <= 500 for x in p["semantic_evidence"])
    ):
        raise ValueError("missing semantic evidence")
    if p["pageview_event"] in p["steps"] + p["key_events"] + p["error_events"] and (
        p["actor_key"],
        p["chain_key"],
    ) != (p["traffic_actor_key"], p["traffic_chain_key"]):
        raise ValueError("shared pageview event needs consistent identity semantics")
    exclusions(p["exclusions"])
    return p


def exclusions(items):
    if type(items) is not list or len(items) > 6:
        raise ValueError("invalid exclusions")
    clauses = []
    for x in items:
        if type(x) is not dict or set(x) != {"property", "value"}:
            raise ValueError("invalid exclusion")
        name = x["property"]
        value = x["value"]
        literal(value)
        if type(name) is not str:
            raise ValueError("invalid exclusion field")
        if name == "distinct_id":
            if type(value) is not str:
                raise ValueError("distinct_id exclusion must be text")
            expression = "distinct_id"
            expected = literal(value)
        else:
            source = "person.properties" if name.startswith("person:") else "properties"
            key = name[7:] if name.startswith("person:") else name
            prop(key)
            expression = f"JSONExtractRaw({source},{literal(key)})"
            expected = literal(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        clauses.append(f"NOT coalesce({expression}={expected},false)")
    return " AND ".join(clauses) or "true"


def timestamp(x):
    if not isinstance(x, dt.datetime) or x.utcoffset() != dt.timedelta(0):
        raise ValueError("UTC boundary required")
    return "toDateTime('" + x.strftime("%Y-%m-%d %H:%M:%S") + "','UTC')"


def event_list(p):
    return sorted(
        set(
            p["steps"]
            + p["key_events"]
            + p["error_events"]
            + ([p["pageview_event"]] if p["pageview_event"] else [])
        )
    )


def where(p, start, end, events=None):
    s = f"timestamp>={timestamp(start)} AND timestamp<{timestamp(end)} AND ({exclusions(p['exclusions'])})"
    if events is not None:
        if not events:
            raise ValueError("empty event selection")
        s += " AND event IN (" + ",".join(map(literal, events)) + ")"
    return s


def bucket(expr, values):
    allowed = ",".join(map(literal, values)) or "'__no_selected_category__'"
    return f"multiIf(NOT {nonempty(expr)},'Unknown',toString({expr}) IN ({allowed}),toString({expr}),'Other')"


def inventory(p, w):
    # Historical coverage is deliberately bounded to 90 days, not lifetime reliability.
    a, b, c = w
    hist = c - dt.timedelta(days=90)
    return f"SELECT event,count() AS observed,countIf(timestamp>={timestamp(a)} AND timestamp<{timestamp(b)}) AS prior,countIf(timestamp>={timestamp(b)}) AS current,min(timestamp) AS first_seen,max(timestamp) AS last_seen FROM events WHERE {where(p, hist, c)} GROUP BY event ORDER BY event LIMIT 201"


def coverage(p, w):
    a, b, c = w
    actor = identity(p["actor_key"])
    chain = identity(p["chain_key"])
    if p["pageview_event"]:
        ev = literal(p["pageview_event"])
        actor = f"if(event={ev},toString({identity(p['traffic_actor_key'])}),toString({actor}))"
        chain = f"if(event={ev},toString({identity(p['traffic_chain_key'])}),toString({chain}))"
    extra = []
    keys = sorted(
        set(
            [x["property"] for x in p["exclusions"]]
            + [x for x in (p["category_property"], p["path_property"], p["source_property"]) if x]
        )
    )
    for i, k in enumerate(keys):
        extra.append(f"countIf(NOT {nonempty(exclusion_field(k))}) AS missing_p{i}")
    fields = [
        "event",
        f"if(timestamp<{timestamp(b)},'prior','current') AS period",
        "count() AS raw",
        f"countIf({nonempty(actor)}) AS actor_rows",
        f"countIf({nonempty(actor)} AND {nonempty(chain)}) AS eligible_rows",
        f"uniqExactIf(toString({actor}),{nonempty(actor)}) AS actors",
        f"uniqExactIf(toString({actor}),{nonempty(actor)} AND {nonempty(chain)}) AS eligible_actors",
        f"uniqExactIf(tuple(toString({actor}),toString({chain})),{nonempty(actor)} AND {nonempty(chain)}) AS chains",
        "min(timestamp) AS first_seen",
        "max(timestamp) AS last_seen",
    ] + extra
    return (
        "SELECT "
        + ",".join(fields)
        + f" FROM events WHERE {where(p, a, c, event_list(p))} GROUP BY event,period ORDER BY event,period LIMIT 41",
        keys,
    )


def trends(p, w):
    a, b, c = w
    hist = c - dt.timedelta(days=90)
    actor = identity(p["actor_key"])
    chain = identity(p["chain_key"])
    selected = sorted(set(p["key_events"] + p["error_events"]))
    day = f"if(timestamp<{timestamp(a)},toString(toStartOfWeek(toTimeZone(timestamp,'UTC'),1)),toString(toDate(toTimeZone(timestamp,'UTC'))))"
    resolution = f"if(timestamp<{timestamp(a)},'week','day')"
    return f"SELECT {day} AS day,{resolution} AS resolution,event,count() AS raw,uniqExactIf(toString({actor}),{nonempty(actor)}) AS actors,uniqExactIf(tuple(toString({actor}),toString({chain})),{nonempty(actor)} AND {nonempty(chain)}) AS chains FROM events WHERE {where(p, hist, c, selected)} GROUP BY day,resolution,event ORDER BY day,resolution,event LIMIT 601"


def funnel_ctes(p, w):
    validate_plan(p)
    steps = p["steps"]
    n = len(steps)
    if not 2 <= n <= 6:
        raise ValueError("funnel unavailable")
    a, b, c = w
    actor = identity(p["actor_key"])
    chain = identity(p["chain_key"])
    cat = (
        bucket(prop(p["category_property"]), p["categories"])
        if p["category_property"]
        else "'Unknown'"
    )
    e = f"e AS (SELECT toString({actor}) AS actor,toString({chain}) AS attempt,event,timestamp AS t,if(timestamp<{timestamp(b)},'prior','current') AS period,{cat} AS category FROM events WHERE {where(p, a, c, steps)} AND {nonempty(actor)} AND {nonempty(chain)})"
    parts = [
        e,
        "b AS (SELECT DISTINCT actor,attempt,event,t,period FROM e)",
        "stages AS (SELECT period,actor,attempt,"
        + ",".join(
            f"arraySort(groupArrayIf(t,event={literal(ev)})) AS a{i}"
            for i, ev in enumerate(steps, 1)
        )
        + " FROM b GROUP BY period,actor,attempt)",
        "s1 AS (SELECT *,arrayElement(a1,1) AS t1,1 AS depth1 FROM stages WHERE length(a1)>0)",
    ]
    for i in range(2, n + 1):
        parts += [
            f"f{i} AS (SELECT *,arrayFilter(x -> depth{i - 1}={i - 1} AND x>t{i - 1},a{i}) AS later{i} FROM s{i - 1})",
            f"s{i} AS (SELECT *,arrayElement(later{i},1) AS t{i},if(length(later{i})>0,{i},depth{i - 1}) AS depth{i} FROM f{i})",
        ]
    times = ",".join(f"t{i}" for i in range(1, n + 1))
    parts += [
        f"attempts AS (SELECT period,actor,attempt,{times},depth{n} AS depth FROM s{n})",
        f"ranked AS (SELECT *,row_number() OVER(PARTITION BY period,actor ORDER BY depth DESC,{times},attempt) AS choice FROM attempts)",
        "chosen AS (SELECT *,formatDateTime(t1,'%Y-%m-%d','UTC') AS day FROM ranked WHERE choice=1)",
    ]
    return parts


def funnel(p, w):
    n = len(p["steps"])
    parts = funnel_ctes(p, w)
    pairs = list(
        dict.fromkeys([(1, i) for i in range(2, n + 1)] + [(i - 1, i) for i in range(2, n + 1)])
    )
    metrics = [f"countIf(depth>={i}) AS n{i}" for i in range(1, n + 1)]
    metrics += [
        f"arraySort(groupArrayIf(dateDiff('microsecond',t{x},t{y})/1000000.0,depth>={y})) AS d{x}_{y}"
        for x, y in pairs
    ]
    bad = " OR ".join(f"(depth>={i} AND t{i}<=t{i - 1})" for i in range(2, n + 1))
    metrics += [
        f"countIf({bad}) AS order_violations",
        "count()-uniqExact(actor) AS duplicate_choices",
    ]
    parts += [
        "daily AS (SELECT period,day," + ",".join(metrics) + " FROM chosen GROUP BY period,day)"
    ]
    cols = ["period", "day"] + [f"n{i}" for i in range(1, n + 1)]
    cols += [
        f"if(n{y}=0,NULL,(arrayElement(d{x}_{y},intDiv(n{y}+1,2))+arrayElement(d{x}_{y},intDiv(n{y}+2,2)))/2.0) AS median_d{x}_{y}"
        for x, y in pairs
    ]
    cols += ["order_violations", "duplicate_choices"]
    return (
        "WITH "
        + ",\n".join(parts)
        + " SELECT "
        + ",".join(cols)
        + " FROM daily ORDER BY period,day LIMIT 63"
    )


def breakdown(p, w):
    if not p["category_property"]:
        raise ValueError("no defensible category")
    n = len(p["steps"])
    parts = funnel_ctes(p, w)
    parts += [
        f"first_categories AS (SELECT period,actor,t,category,min(t) OVER(PARTITION BY period,actor) AS first_t FROM e WHERE event={literal(p['steps'][0])})",
        "cats AS (SELECT period,actor,if(uniqExactIf(category,t=first_t)=1,anyIf(category,t=first_t),'Ambiguous') AS category_bucket FROM first_categories GROUP BY period,actor)",
        "outcomes AS (SELECT c.period AS period,c.actor AS actor,c.depth AS depth,k.category_bucket AS category FROM chosen c JOIN cats k ON c.period=k.period AND c.actor=k.actor)",
    ]
    return (
        "WITH "
        + ",\n".join(parts)
        + f" SELECT category,count() AS total,countIf(depth={n}) AS converted FROM outcomes WHERE period='current' GROUP BY category ORDER BY category LIMIT 12"
    )


def traffic(p, w):
    if not p["pageview_event"]:
        raise ValueError("no-pageview mapping")
    a, b, c = w
    actor = identity(p["traffic_actor_key"])
    session = identity(p["traffic_chain_key"])
    path = bucket(prop(p["path_property"]), p["paths"])
    source = bucket(prop(p["source_property"]), p["sources"])
    # Traffic requires this key's semantics to be session, not attempt. The skill enforces it.
    parts = [
        f"v AS (SELECT if(timestamp<{timestamp(b)},'prior','current') AS period,toString({actor}) AS actor,toString({session}) AS session,timestamp AS t,{path} AS path,{source} AS source FROM events WHERE {where(p, a, c, [p['pageview_event']])} AND {nonempty(actor)} AND {nonempty(session)})",
        "ordered AS (SELECT *,min(t) OVER(PARTITION BY period,actor,session) AS first_t FROM v)",
        "entries AS (SELECT period,actor,session,if(uniqExactIf(path,t=first_t)=1,anyIf(path,t=first_t),'Ambiguous') AS entry_path,if(uniqExactIf(source,t=first_t)=1,anyIf(source,t=first_t),'Ambiguous') AS entry_source,count() AS pageviews FROM ordered GROUP BY period,actor,session)",
    ]
    return (
        "WITH "
        + ",".join(parts)
        + " SELECT period,entry_path AS path,entry_source AS source,count() AS sessions,sum(pageviews) AS pageviews FROM entries GROUP BY period,entry_path,entry_source ORDER BY period,entry_path,entry_source LIMIT 243"
    )


def request(c, step, p, w):
    builders = {
        "inventory": inventory,
        "coverage": coverage,
        "trends": trends,
        "funnel": funnel,
        "traffic": traffic,
        "breakdown": breakdown,
        "dimensions": dimensions,
    }
    if step not in builders:
        raise ValueError("unknown query step")
    if type(c.get("posthog_project_id")) is not str or not re.fullmatch(
        r"[0-9]{1,20}", c["posthog_project_id"]
    ):
        raise ValueError("invalid provider project ID")
    if step != "inventory":
        validate_plan(p)
    sql = builders[step](p, w)
    if step == "coverage":
        sql = sql[0]
    # Caller supplies validated data, never SQL; query text only comes from fixed builders.
    if not sql.startswith(("SELECT ", "WITH ")) or ";" in sql or "--" in sql or "/*" in sql:
        raise ValueError("invalid builder output")
    r = dict(
        service="analytics",
        step=step,
        method="POST",
        path="/api/projects/" + c["posthog_project_id"] + "/query/",
        body={"query": {"kind": "HogQLQuery", "query": sql}},
    )
    if len(json.dumps(r, ensure_ascii=False).encode()) > 16000:
        raise ValueError("request too large")
    return r


def table(data, columns, cap):
    # Pass only the documented provider data object after checking gateway status separately.
    if type(data) is not dict or len(json.dumps(data, ensure_ascii=False).encode()) > 64000:
        raise ValueError("invalid provider data")
    for k in (
        "error",
        "errors",
        "hasMore",
        "has_more",
        "next",
        "is_partial",
        "exceeded_limit",
    ):
        if data.get(k):
            raise ValueError("incomplete evidence: " + k)
    status = data.get("query_status")
    if status and (
        type(status) is not dict or status.get("complete") is not True or status.get("error")
    ):
        raise ValueError("pending/failed query")
    if data.get("columns") != columns or type(data.get("results")) is not list:
        raise ValueError("wrong result schema")
    rows = data["results"]
    if len(rows) >= cap or any(type(row) is not list or len(row) != len(columns) for row in rows):
        raise ValueError("truncated/malformed result")
    return [dict(zip(columns, row)) for row in rows]


def count(x):
    if type(x) is not int or x < 0:
        raise ValueError("invalid count")
    return x


def ratio(n, d):
    count(n)
    count(d)
    return None if d == 0 else n / d


def change(current, prior):
    count(current)
    count(prior)
    return {
        "current": current,
        "prior": prior,
        "delta": current - prior,
        "relative_pct": None if prior == 0 else 100 * (current - prior) / prior,
    }


def validate_funnel(rows, p, w):
    n = len(p["steps"])
    seen = set()
    totals = {"prior": [0] * n, "current": [0] * n}
    a, b, c = w
    for r in rows:
        day = dt.date.fromisoformat(r["day"])
        period = r["period"]
        lo, hi = (a, b) if period == "prior" else (b, c)
        if period not in totals or not lo.date() <= day < hi.date() or (period, day) in seen:
            raise ValueError("invalid cohort day")
        seen.add((period, day))
        nums = [count(r["n" + str(i)]) for i in range(1, n + 1)]
        if (
            any(x < y for x, y in zip(nums, nums[1:]))
            or count(r["order_violations"])
            or count(r["duplicate_choices"])
        ):
            raise ValueError("invalid chain counts")
        for x, y in dict.fromkeys(
            [(1, i) for i in range(2, n + 1)] + [(i - 1, i) for i in range(2, n + 1)]
        ):
            v = r[f"median_d{x}_{y}"]
            if nums[y - 1] == 0:
                if v is not None:
                    raise ValueError("median for empty cohort")
            elif (
                type(v) not in (int, float)
                or not math.isfinite(v)
                or not 0 < v < (hi - lo).total_seconds()
            ):
                raise ValueError("invalid median")
        totals[period] = [x + y for x, y in zip(totals[period], nums)]
    return totals


def validate_coverage(rows, events, w, property_keys):
    seen = set()
    result = {}
    for r in rows:
        k = (r["period"], r["event"])
        if k in seen or k[0] not in ("prior", "current") or k[1] not in events:
            raise ValueError("invalid coverage key")
        seen.add(k)
        raw, ar, er, actors, chains = [
            count(r[x]) for x in ("raw", "actor_rows", "eligible_rows", "actors", "chains")
        ]
        if (
            not count(r["eligible_actors"]) <= actors <= ar <= raw
            or not r["eligible_actors"] <= chains <= er <= ar
        ):
            raise ValueError("invalid coverage counts")
        for i, _ in enumerate(property_keys):
            if count(r["missing_p" + str(i)]) > raw:
                raise ValueError("invalid property coverage")
        if raw == 0:
            raise ValueError("empty grouped coverage row")
        lo, hi = (w[0], w[1]) if k[0] == "prior" else (w[1], w[2])
        if not lo <= observed_time(r["first_seen"]) <= observed_time(r["last_seen"]) < hi:
            raise ValueError("coverage date mismatch")
        result[k] = r
    return result


def validate_trends(rows, p, w, cov):
    a, b, c = w
    hist = c - dt.timedelta(days=90)
    seen = set()
    sums = {}
    series = {}
    events = sorted(set(p["key_events"] + p["error_events"]))
    for r in rows:
        day = dt.date.fromisoformat(r["day"])
        ev = r["event"]
        res = r["resolution"]
        k = (r["day"], res, ev)
        if k in seen or ev not in events:
            raise ValueError("invalid trend key")
        if res == "day" and not a.date() <= day < c.date():
            raise ValueError("invalid daily trend date")
        if res == "week" and (
            day.weekday() != 0 or not hist.date() - dt.timedelta(days=6) <= day < a.date()
        ):
            raise ValueError("invalid weekly trend date")
        if res not in ("day", "week"):
            raise ValueError("invalid trend resolution")
        seen.add(k)
        raw, actors, chains = [count(r[x]) for x in ("raw", "actors", "chains")]
        if max(actors, chains) > raw:
            raise ValueError("invalid trend aggregates")
        if res == "day":
            period = "prior" if day < b.date() else "current"
            sums[(period, ev)] = sums.get((period, ev), 0) + raw
        series[k] = r
    for period in ("prior", "current"):
        for ev in events:
            if sums.get((period, ev), 0) != cov.get((period, ev), {}).get("raw", 0):
                raise ValueError("trend/coverage mismatch")
    for i in range((c - a).days):
        day = (a + dt.timedelta(days=i)).date().isoformat()
        for ev in events:
            series.setdefault(
                (day, "day", ev),
                dict(day=day, resolution="day", event=ev, raw=0, actors=0, chains=0),
            )
    comparisons = {
        ev: change(sums.get(("current", ev), 0), sums.get(("prior", ev), 0)) for ev in events
    }
    return sorted(
        series.values(), key=lambda r: (r["day"], r["resolution"], r["event"])
    ), comparisons


def validate_breakdown(rows, p, current_funnel):
    seen = set()
    n = success = 0
    for r in rows:
        cat = r["category"]
        if cat in seen or cat not in set(p["categories"]) | RESERVED:
            raise ValueError("invalid category")
        seen.add(cat)
        count(r["total"])
        count(r["converted"])
        if r["converted"] > r["total"]:
            raise ValueError("impossible conversion")
        n += r["total"]
        success += r["converted"]
    if (n, success) != (current_funnel[0], current_funnel[-1]):
        raise ValueError("breakdown/funnel mismatch")
    return n, success


def validate_traffic(rows, p, cov):
    seen = set()
    totals = {x: [0, 0] for x in ("prior", "current")}
    for r in rows:
        k = (r["period"], r["path"], r["source"])
        if (
            k in seen
            or k[0] not in totals
            or k[1] not in set(p["paths"]) | RESERVED
            or k[2] not in set(p["sources"]) | RESERVED
        ):
            raise ValueError("invalid traffic bucket")
        seen.add(k)
        n = count(r["sessions"])
        v = count(r["pageviews"])
        if n > v:
            raise ValueError("sessions exceed pageviews")
        totals[k[0]][0] += n
        totals[k[0]][1] += v
    for period, (n, v) in totals.items():
        r = cov.get((period, p["pageview_event"]), {})
        if n != r.get("chains", 0) or v != r.get("eligible_rows", 0):
            raise ValueError("traffic/coverage mismatch")
    return totals


def fisher(a, b, c, d):
    return fisher_exact_two_sided(a, b, c, d)


def screen(rows, plan):
    total = sum(count(r["total"]) for r in rows)
    yes = sum(count(r["converted"]) for r in rows)
    by_name = {r["category"]: r for r in rows}
    family = plan["categories"]
    if not family:
        return {
            "status": "insufficient evidence",
            "test": "two-sided Fisher exact; Holm correction",
            "family_size": 0,
            "results": [],
        }
    tables = []
    for name in family:
        r = by_name.get(name, {"total": 0, "converted": 0})
        a = r["converted"]
        b = r["total"] - a
        c = yes - a
        d = total - r["total"] - c
        tables.append((a, b, c, d))
    checked = screen_comparisons(tables)
    return {
        "status": "screened",
        "test": "two-sided Fisher exact; Holm correction",
        "family_size": len(family),
        "alpha": 0.05,
        "results": [
            dict(category=name, n=a + b, converted=a, rate=ratio(a, a + b), **v)
            for name, (a, b, c, d), v in zip(family, tables, checked, strict=True)
        ],
    }


def reference_funnel(rows, steps):
    # Synthetic oracle only: rows=(period,actor,attempt,event,numeric_time).
    # Exclusions and UTC half-open boundaries must already have been applied.
    groups = {}
    for period, actor, attempt, event, t in rows:
        if actor is None or attempt is None or actor == "" or attempt == "":
            continue
        groups.setdefault((period, actor, attempt), set()).add((event, t))
    chosen = {}
    for (period, actor, attempt), events in groups.items():
        times = []
        for ev in steps:
            valid = sorted(t for e, t in events if e == ev and (not times or t > times[-1]))
            if not valid:
                break
            times.append(valid[0])
        if times:
            rank = (-len(times), tuple(times), attempt)
            if (period, actor) not in chosen or rank < chosen[(period, actor)][0]:
                chosen[(period, actor)] = (rank, times)
    return {
        period: {
            "counts": [
                sum(len(t) >= i for (per, _), (_, t) in chosen.items() if per == period)
                for i in range(1, len(steps) + 1)
            ],
            "medians": [
                statistics.median(
                    [
                        t[i] - t[0]
                        for (per, _), (_, t) in chosen.items()
                        if per == period and len(t) > i
                    ]
                )
                if any(per == period and len(t) > i for (per, _), (_, t) in chosen.items())
                else None
                for i in range(1, len(steps))
            ],
        }
        for period in ("prior", "current")
    }


def observed_time(value):
    if type(value) is not str:
        raise ValueError("invalid observed timestamp")
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("provider timestamp timezone unverified")
    return t.astimezone(dt.timezone.utc)


def validate_inventory(rows, w):
    a, b, c = w
    lo = c - dt.timedelta(days=90)
    seen = {}
    for r in rows:
        ev = r["event"]
        if type(ev) is not str or not 1 <= len(ev) <= 256 or ev in seen:
            raise ValueError("invalid inventory event")
        observed, prior, current = [count(r[k]) for k in ("observed", "prior", "current")]
        if observed == 0 or prior + current > observed:
            raise ValueError("invalid inventory counts")
        first, last = observed_time(r["first_seen"]), observed_time(r["last_seen"])
        if not lo <= first <= last < c:
            raise ValueError("invalid observed range")
        seen[ev] = r
    return seen


def reconcile_coverage(cov, inventory_rows, p, w):
    inventory_map = validate_inventory(inventory_rows, w)
    a, b, c = w
    for period, lo, hi in (("prior", a, b), ("current", b, c)):
        for ev in event_list(p):
            r = cov.get((period, ev), {})
            if r.get("raw", 0) != inventory_map.get(ev, {}).get(period, 0):
                raise ValueError("inventory/coverage mismatch")
            if r:
                first, last = observed_time(r["first_seen"]), observed_time(r["last_seen"])
                if not lo <= first <= last < hi:
                    raise ValueError("coverage date mismatch")
    return cov


def reconcile_funnel(rows, p, w, cov):
    totals = validate_funnel(rows, p, w)
    for period, nums in totals.items():
        for i, ev in enumerate(p["steps"]):
            eligible = cov.get((period, ev), {}).get("eligible_actors", 0)
            if nums[i] > eligible or (i == 0 and nums[i] != eligible):
                raise ValueError("funnel/coverage mismatch")
    # Daily zero-fill is valid only after successful table/schema/coverage reconciliation.
    present = {(r["period"], r["day"]): r for r in rows}
    a, b, c = w
    for period, lo, hi in (("prior", a, b), ("current", b, c)):
        for i in range((hi - lo).days):
            day = (lo + dt.timedelta(days=i)).date().isoformat()
            if (period, day) not in present:
                empty = dict(period=period, day=day, order_violations=0, duplicate_choices=0)
                empty.update({"n" + str(j): 0 for j in range(1, len(p["steps"]) + 1)})
                empty.update({f"median_d{x}_{y}": None for x, y in median_pairs(len(p["steps"]))})
                present[(period, day)] = empty
    return totals, sorted(present.values(), key=lambda r: (r["day"], r["period"]))


def plan_state(previous, binding, plan, schema_signature):
    validate_plan(plan)
    encoded = json.dumps(schema_signature, sort_keys=True, ensure_ascii=False)
    if len(encoded.encode()) > 8000:
        raise ValueError("schema signature too large")
    proposed = {
        "binding": binding,
        "version": VERSION,
        "plan": plan,
        "schema_signature": schema_signature,
    }
    if previous is None:
        return {"state": "provisional", "pin": proposed, "changes": []}
    if type(previous) is not dict or set(previous) != set(proposed):
        raise ValueError("invalid previous pin")
    validate_plan(previous["plan"])
    # An explicit saved-input edit authorizes a new mapping, not a new software release.
    if previous["binding"] != binding:
        return {"state": "reconfigured", "pin": proposed, "changes": ["saved inputs"]}
    changes = [k for k in proposed if proposed[k] != previous[k]]
    detail = []
    for k in changes:
        detail += (
            ["plan." + name for name in plan if plan[name] != previous["plan"].get(name)]
            if k == "plan"
            else [k]
        )
    return {
        "state": "schema changed" if changes else "pinned",
        "pin": previous,
        "proposed": proposed if changes else None,
        "changes": detail,
    }


def funnel_display(rows, plan):
    answer = []
    for r in rows:
        first = r["n1"]
        for i, (ev, label) in enumerate(zip(plan["steps"], plan["labels"]), 1):
            n = r["n" + str(i)]
            previous = r.get("n" + str(i - 1))
            answer.append(
                {
                    "period": r["period"],
                    "selected_start_day": r["day"],
                    "step": i,
                    "event": ev,
                    "label": label,
                    "chain_actors": n,
                    "of_first_pct": None if first == 0 else 100 * ratio(n, first),
                    "of_previous_pct": None
                    if previous is None or previous == 0
                    else 100 * ratio(n, previous),
                    "median_from_first_seconds": (0 if n else None)
                    if i == 1
                    else r[f"median_d1_{i}"],
                    "median_from_previous_seconds": None if i == 1 else r[f"median_d{i - 1}_{i}"],
                }
            )
    return answer


def coverage_display(cov, property_keys):
    answer = []
    for (period, event), r in sorted(cov.items()):
        raw = r["raw"]
        answer.append(
            {
                "period": period,
                "event": event,
                "raw": raw,
                "missing_actor_rows": raw - r["actor_rows"],
                "missing_actor_or_chain_rows": raw - r["eligible_rows"],
                "actor_coverage": ratio(r["actor_rows"], raw),
                "chain_coverage": ratio(r["eligible_rows"], raw),
                "missing_properties": {
                    name: r["missing_p" + str(i)] for i, name in enumerate(property_keys)
                },
            }
        )
    return answer


def exclusion_field(name):
    if name == "distinct_id":
        return "distinct_id"
    return prop(name[7:], "person.properties") if name.startswith("person:") else prop(name)


def median_pairs(n):
    return list(
        dict.fromkeys([(1, i) for i in range(2, n + 1)] + [(i - 1, i) for i in range(2, n + 1)])
    )


def query_columns(step, p):
    if step == "inventory":
        return ["event", "observed", "prior", "current", "first_seen", "last_seen"], 201
    if step == "coverage":
        _, keys = coverage(p, settings({"posthog_project_id": "1", "as_of_utc": "2026-01-01"})[1])
        return [
            "event",
            "period",
            "raw",
            "actor_rows",
            "eligible_rows",
            "actors",
            "eligible_actors",
            "chains",
            "first_seen",
            "last_seen",
        ] + ["missing_p" + str(i) for i in range(len(keys))], 41
    if step == "trends":
        return ["day", "resolution", "event", "raw", "actors", "chains"], 601
    if step == "funnel":
        n = len(p["steps"])
        return ["period", "day"] + [f"n{i}" for i in range(1, n + 1)] + [
            f"median_d{x}_{y}" for x, y in median_pairs(n)
        ] + ["order_violations", "duplicate_choices"], 63
    if step == "traffic":
        return ["period", "path", "source", "sessions", "pageviews"], 243
    if step == "breakdown":
        return ["category", "total", "converted"], 12
    if step == "dimensions":
        return ["kind", "value", "volume"], 25
    raise ValueError("unknown query schema")


def safe_dimension(value):
    if (
        type(value) is not str
        or not re.fullmatch(r"[A-Za-z/][A-Za-z0-9 _./-]{0,63}", value)
        or value in RESERVED
        or re.search(r"[0-9]{5}|[a-f0-9]{8}-[a-f0-9]{4}", value, re.I)
    ):
        raise ValueError("unsafe or identifying dimension value")
    return value


def validate_dimensions(rows, p):
    result = {k: [] for k in ("categories", "paths", "sources")}
    seen = set()
    for r in rows:
        kind, value = r["kind"], r["value"]
        if kind not in result or type(value) is not str or (kind, value) in seen:
            raise ValueError("invalid safe dimension value")
        safe_dimension(value)
        count(r["volume"])
        seen.add((kind, value))
        result[kind].append(value)
        if len(result[kind]) > 8:
            raise ValueError("too many dimension values")
    return result


def error_signals(series, plan, funnel_totals):
    # Descriptive heuristics, not bug diagnoses or a matched-cohort failure rate.
    rows = []
    for event in plan["error_events"]:
        counts = [r["raw"] for r in series if r["event"] == event and r["resolution"] == "day"]
        baseline = statistics.median(counts) if counts else 0
        if counts and max(counts) >= 20 and max(counts) >= 5 * max(1, baseline):
            rows.append(
                {
                    "event": event,
                    "signal": "daily concentration",
                    "peak": max(counts),
                    "median": baseline,
                    "rule": "peak >= 20 and >= 5 x max(1, median)",
                }
            )
    if funnel_totals:
        for period, values in funnel_totals.items():
            rows.append(
                {
                    "period": period,
                    "signal": "no observed completion within window",
                    "actors": values[0] - values[-1],
                }
            )
    return rows


def dimensions(p, w):
    # Label discovery uses volume, never conversion, and exports no raw identifiers.
    pieces = []
    a, b, c = w
    for kind, key, events in [
        ("categories", p["category_property"], p["steps"][:1]),
        ("paths", p["path_property"], [p["pageview_event"]]),
        ("sources", p["source_property"], [p["pageview_event"]]),
    ]:
        if not key or not all(events):
            continue
        expr = f"toString({prop(key)})"
        safe = f"match({expr},'^[A-Za-z/][A-Za-z0-9 _./-]{{0,63}}$') AND NOT match({expr},'[0-9]{{5}}|[a-fA-F0-9]{{8}}-[a-fA-F0-9]{{4}}') AND {expr} NOT IN ('Unknown','Other','Ambiguous')"
        pieces.append(
            f"SELECT * FROM (SELECT '{kind}' AS kind,{expr} AS value,count() AS volume FROM events WHERE {where(p, a, c, events)} AND {safe} GROUP BY value ORDER BY volume DESC,value LIMIT 8)"
        )
    if not pieces:
        raise ValueError("no supported dimension fields")
    return " UNION ALL ".join(pieces)


def public_pin(pin):
    """Keep exclusions comparable without publishing their supplied scalar values."""
    copy = json.loads(json.dumps(pin))
    for rule in copy["plan"]["exclusions"]:
        rule["value"] = {"sha256": stable_hash(rule["value"])}
    return copy


def restore_pin(pin, binding, rules):
    if not isinstance(pin, dict) or pin.get("binding") != binding:
        raise ValueError("previous settings binding mismatch")
    exclusions(rules)
    copy = json.loads(json.dumps(pin))
    expected = [
        {"property": r["property"], "value": {"sha256": stable_hash(r["value"])}} for r in rules
    ]
    if copy.get("plan", {}).get("exclusions") != expected:
        raise ValueError("previous exclusions mismatch")
    copy["plan"]["exclusions"] = json.loads(json.dumps(rules))
    validate_plan(copy["plan"])
    return copy


def safe_sql(sql, rules):
    # Remove complete exclusion clauses, rather than accidentally rewriting event literals.
    exclusions(rules)
    for rule in rules:
        sql = sql.replace(exclusions([rule]), "[exclusion " + stable_hash(rule) + "]")
    return sql
```
