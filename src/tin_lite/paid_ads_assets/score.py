"""Deterministic economics, scoring and verdict for the paid-ads assessment.

Every number here was calibrated on 2026-09-22 against four real projects with Google Ads
history (see docs/paid-ads-assessment-implementation.md). The model nodes never change a
verdict: this module decides, and they explain and shape the campaign around the decision.
"""

from __future__ import annotations

import json
from pathlib import Path

BENCHMARKS = json.loads((Path(__file__).with_name("benchmarks.json")).read_text())
RUBRIC = json.loads((Path(__file__).with_name("rubric.json")).read_text())

VERDICTS = ("go", "test", "not_now", "continue", "restructure", "restart", "do_not_restart")


def allowable(profile: dict, benchmarks: dict = BENCHMARKS) -> tuple[float, dict]:
    """The most a paying customer may cost: min(12-month payback, LTV/3), or first-purchase
    gross profit for one-time sales. Unknown price falls back to the industry figure."""
    industry = benchmarks["industries"][profile["industry"]]
    price = profile.get("price")
    if price is None or price <= 0:
        return float(industry["fallback_allowable_cpa"]), {
            "rule": "industry fallback; price unknown",
            "confidence": "low",
        }
    margin = profile["gross_margin"]
    if profile["billing"] == "one_time":
        gp = price * margin
        cap = gp * profile.get("repeat_factor", 1.0)
        return cap, {"rule": "first-purchase gross profit x repeat", "gross_profit": gp}
    monthly = price / 12 if profile["billing"] == "annual" else price
    gp = monthly * margin
    payback = 12 * gp
    ltv3 = gp * industry["lifetime_months"] / 3
    return min(payback, ltv3), {
        "rule": "min(12-month payback, LTV/3)",
        "monthly_gross_profit": gp,
        "payback_ceiling": payback,
        "ltv_over_three": ltv3,
        "lifetime_months_assumed": industry["lifetime_months"],
    }


def lp_multiplier(profile: dict, benchmarks: dict = BENCHMARKS) -> float:
    lp = benchmarks["landing_page"]
    m = 1.0
    if not profile.get("pricing_shown", True):
        m *= lp["no_pricing_shown"]
    if profile.get("mobile_ok") is False:
        m *= lp["mobile_broken"]
    if profile.get("free_step"):
        m *= lp["free_step_before_paywall"]
    return m


def demand(volumes: dict, labels: dict, benchmarks: dict = BENCHMARKS) -> dict:
    """Volume by intent bucket, the clicks-weighted intent multiplier, and auction density.

    `volumes` maps keyword id to {"volume", "competition_index"}; `labels` maps keyword id to
    an intent label. Irrelevant keywords are excluded from every total."""
    buckets: dict[str, int] = {}
    comp_weight = comp_total = 0.0
    for kid, label in labels.items():
        row = volumes.get(kid) or {}
        volume = row.get("volume") or 0
        buckets[label] = buckets.get(label, 0) + volume
        if label in benchmarks["buyer_intents"]:
            comp_weight += (row.get("competition_index") or 0) * volume
            comp_total += volume
    buyer = sum(v for k, v in buckets.items() if k in benchmarks["buyer_intents"])
    total = sum(v for k, v in buckets.items() if k != "irrelevant")
    multipliers = benchmarks["intent_cvr_multiplier"]
    weighted = (
        sum(multipliers.get(k, 0) * v for k, v in buckets.items() if k != "irrelevant") / total
        if total
        else 0.0
    )
    return {
        "buckets": buckets,
        "buyer_volume": buyer,
        "total_volume": total,
        "weighted_intent": weighted,
        "buyer_share": buyer / total if total else 0.0,
        "competition_index": comp_weight / comp_total if comp_total else 0.0,
        "captive_volume": buckets.get("captive", 0),
    }


def choose_bid(curve: list[dict], max_cpc: float, buyer_volume: float, benchmarks=BENCHMARKS):
    """The highest forecast bid level whose CPC is affordable, else the lowest level; demand is
    floored at a plain click-through share of buyer-intent volume because the forecast returns
    almost nothing for new terms with no bid history."""
    points = sorted((p for p in curve if p.get("clicks")), key=lambda p: p["bid"])
    if not points:
        return None
    affordable = [p for p in points if p["cpc"] <= max_cpc]
    chosen = dict(affordable[-1] if affordable else points[0])
    chosen["affordable"] = bool(affordable)
    floor = buyer_volume * benchmarks["demand_floor_ctr"]
    if chosen["clicks"] < floor:
        chosen.update(clicks=floor, cost=floor * chosen["cpc"], floored=True)
    else:
        chosen["floored"] = False
    return chosen


def observed_override(profile: dict, allow_cpa: float):
    obs = profile.get("observed") or {}
    if profile.get("ads_history") not in {"running", "stopped"}:
        return None
    spend, clicks, purchases = obs.get("spend"), obs.get("clicks"), obs.get("purchases")
    if not spend or not clicks or not purchases:
        return None
    cpa = spend / purchases
    return {
        "cpa_customer": cpa,
        "cpc": spend / clicks,
        "cvr": purchases / clicks,
        "headroom": allow_cpa / cpa if cpa else None,
    }


def implied_soft(profile: dict, allow_cpa: float):
    obs = profile.get("observed") or {}
    if not obs.get("spend") or not obs.get("soft_conversions") or obs.get("purchases"):
        return None
    per_soft = obs["spend"] / obs["soft_conversions"]
    return {
        "cost_per_soft_event": per_soft,
        "soft_to_purchase_needed_for_breakeven": per_soft / allow_cpa if allow_cpa else None,
        "cpa_at_10pct": per_soft / 0.10,
        "cpa_at_25pct": per_soft / 0.25,
    }


def minimum_test(cost_per_event, benchmarks: dict = BENCHMARKS) -> float:
    mt = benchmarks["minimum_test"]
    floor = mt["google_search_floor_usd_month"]
    if not cost_per_event:
        return float(floor)
    return float(max(floor, mt["conversions_for_a_read"] * cost_per_event))


def scores(*, headroom, clicks, profile, buyer_share, competition_index) -> dict:
    weights, bands = RUBRIC["score_weights"], RUBRIC["score_bands"]
    ready = BENCHMARKS["readiness"]
    econ = max(0.0, min(weights["economics"], weights["economics"] * headroom / 1.5))
    dem = bands["demand_max"]
    for ceiling, points in bands["demand_clicks"]:
        if clicks < ceiling:
            dem = points
            break
    readiness = (
        ready["tracking"].get(profile.get("tracking", "none"), 0)
        + (ready["pricing_shown"] if profile.get("pricing_shown", True) else 0)
        + (ready["mobile_ok"] if profile.get("mobile_ok") is not False else 0)
        + ready["event"].get(profile["conversion_event"], 0)
    )
    fit = min(weights["fit"], weights["fit"] * buyer_share / bands["fit_full_buyer_share"])
    low, high = bands["auction_moderate"]
    points = bands["auction_points"]
    auction = (
        points["moderate"]
        if low <= competition_index <= high
        else points["low"]
        if competition_index < low
        else points["high"]
    )
    total = econ + dem + readiness + fit + auction
    return {
        "economics": round(econ, 1),
        "demand": dem,
        "readiness": readiness,
        "fit": round(fit, 1),
        "auction": auction,
        "total": round(total, 1),
    }


def verdict(
    *, profile, observed, cheap_test, headroom, est, allow_cpa, budget, min_test, total
) -> tuple[str, str]:
    """History first, then economics, then budget, then the score."""
    rules = RUBRIC["verdict"]
    running = profile.get("ads_history") == "running"
    if observed and observed["headroom"] is not None and observed["headroom"] >= 1.0:
        return ("continue" if running else "restart"), (
            f"observed cost per customer ${observed['cpa_customer']:.0f} is inside the allowable "
            f"${allow_cpa:.0f}"
        )
    if observed and observed["headroom"] is not None:
        return ("restructure" if running else "do_not_restart"), (
            f"observed cost per customer ${observed['cpa_customer']:.0f} is above the allowable "
            f"${allow_cpa:.0f}"
        )
    if cheap_test and headroom < rules["test_headroom"]:
        return "test", (
            "captive-intent terms exist; an exact-match test of about $100-150 costs less than "
            "the uncertainty"
        )
    if headroom < rules["test_headroom"]:
        cpa = est.get("cpa_customer") if est else None
        return "not_now", (
            f"estimated cost per customer ${cpa:.0f} is above the allowable ${allow_cpa:.0f}"
            if cpa
            else "no affordable demand was found"
        )
    if budget is not None and budget < min_test:
        return "not_now", f"budget ${budget:.0f} a month is below the minimum test ${min_test:.0f}"
    if total < rules["not_now_score"]:
        return "not_now", f"readiness and fit score {total:.0f} is below {rules['not_now_score']}"
    if headroom >= rules["go_headroom"] and total >= rules["go_score"]:
        return "go", "economics and demand both clear"
    return "test", "economics are marginal; run the minimum test and read it at thirty events"


def ranges(est: dict | None, allow_cpa: float, min_test: float, budget) -> dict:
    """Numeric bounds the verdict node must stay inside."""
    cap = budget if budget is not None else max(min_test, (est or {}).get("cost_month") or 0)
    cap = cap or min_test
    monthly_low = min(cap, max(100.0, min(min_test, cap) * 0.5))
    monthly_high = max(monthly_low, cap)
    return {
        "daily_budget_usd": {
            "min": round(monthly_low / 30, 2),
            "max": round(monthly_high / 30, 2),
        },
        "target_cpa_usd": {"min": 0.0, "max": round(allow_cpa, 2)},
        "monthly_budget_usd": {"min": round(monthly_low, 2), "max": round(monthly_high, 2)},
    }


def assess(
    profile: dict,
    volumes: dict,
    labels: dict,
    curve: list[dict],
    paid_slots: list[int],
    benchmarks: dict = BENCHMARKS,
) -> dict:
    industry = benchmarks["industries"][profile["industry"]]
    event = benchmarks["conversion_event"][profile["conversion_event"]]
    allow_cpa, allow_note = allowable(profile, benchmarks)
    allow_per_event = allow_cpa * event["close_rate"]
    dem = demand(volumes, labels, benchmarks)
    lpm = lp_multiplier(profile, benchmarks)
    est_cvr = industry["search_cvr"] * event["cvr_multiplier"] * dem["weighted_intent"] * lpm
    max_cpc = allow_per_event * est_cvr
    chosen = choose_bid(curve, max_cpc, dem["buyer_volume"], benchmarks)
    est = None
    if chosen:
        events = chosen["clicks"] * est_cvr
        customers = events * event["close_rate"]
        est = {
            "bid": chosen["bid"],
            "clicks_month": round(chosen["clicks"]),
            "cpc": chosen["cpc"],
            "cost_month": round(chosen["cost"]),
            "events_month": round(events, 1),
            "customers_month": round(customers, 2),
            "cpa_customer": round(chosen["cost"] / customers) if customers else None,
            "affordable_bid_found": chosen["affordable"],
            "floored": chosen["floored"],
        }
    headroom = allow_cpa / est["cpa_customer"] if est and est.get("cpa_customer") else 0.0
    observed = observed_override(profile, allow_cpa)
    implied = implied_soft(profile, allow_cpa)
    cost_per_event = (est["cpc"] / est_cvr) if est and est_cvr else None
    min_test = minimum_test(cost_per_event, benchmarks)
    budget = profile.get("budget_usd_month")
    cheap_test = dem["captive_volume"] > 0 and (
        budget is None or budget >= benchmarks["minimum_test"]["cheap_test_min_budget_usd"]
    )
    card = scores(
        headroom=headroom,
        clicks=est["clicks_month"] if est else 0,
        profile=profile,
        buyer_share=dem["buyer_share"],
        competition_index=dem["competition_index"],
    )
    decision, binding = verdict(
        profile=profile,
        observed=observed,
        cheap_test=cheap_test,
        headroom=headroom,
        est=est,
        allow_cpa=allow_cpa,
        budget=budget,
        min_test=min_test,
        total=card["total"],
    )
    return {
        "allowable_cpa_customer": round(allow_cpa, 2),
        "allowable_note": allow_note,
        "allowable_per_event": round(allow_per_event, 2),
        "conversion_event": profile["conversion_event"],
        "est_cvr_click_to_event": round(est_cvr, 4),
        "weighted_intent": round(dem["weighted_intent"], 2),
        "lp_multiplier": round(lpm, 2),
        "max_affordable_cpc": round(max_cpc, 2),
        "volume_buckets": dem["buckets"],
        "buyer_share": round(dem["buyer_share"], 2),
        "buyer_volume": dem["buyer_volume"],
        "forecast_curve": sorted(curve, key=lambda p: p["bid"]),
        "estimate": est,
        "headroom": round(headroom, 2),
        "observed": observed,
        "implied_from_soft_conversions": implied,
        "auction": {
            "competition_index": round(dem["competition_index"]),
            "paid_slots": list(paid_slots),
        },
        "min_test_usd_month": round(min_test),
        "cheap_test": cheap_test,
        "scores": card,
        "verdict": decision,
        "binding_constraint": binding,
        "ranges": ranges(est, allow_cpa, min_test, budget),
        "benchmarks_version": benchmarks["version"],
    }
