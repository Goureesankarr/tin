"""Prioritize measured demand and broaden discovery across supported buyer jobs."""

from pydantic import Field

from tin_lite import keyword_plan as v1
from tin_lite import keyword_plan_v3 as v3
from tin_lite import keyword_plan_v4 as v4


class Seeds(v3.Seeds):
    specific: list[str] = Field(min_length=1, max_length=6)


POLICY = {**v4.POLICY, "version": "keyword-plan-v5", "seed_strategy": "buyer-jobs.v1"}
SCHEMAS = {**v4.SCHEMAS, "seeds": Seeds.model_json_schema()}


def seed_values(value):
    parsed = Seeds.model_validate(value)
    if len(v1.phrases(parsed.core)) < 3:
        raise ValueError("At least three distinct core lookup phrases are required")
    return v1.phrases([*parsed.core, *parsed.specific])


INSTRUCTIONS = dict(v4.INSTRUCTIONS)
for stage in INSTRUCTIONS:
    INSTRUCTIONS[stage] += """
Consider distinct supported buyer jobs before choosing seeds: finding a solution, comparing
alternatives, evaluating total cost, implementation, migration, operations and troubleshooting.
Use both broad category language and specific buyer language within the existing seed budget.
Do not expand into consumer searches or capabilities the product does not offer.
An empty successful volume lookup is weak demand evidence: conservatively treat its traffic
potential as low/unproven, not hidden high demand. A failed, skipped or unavailable lookup is
missing measurement, not evidence of low demand. Keep measured zero distinct from both.
Never invent numerical volume or competition. Compare every retained candidate using observed
volume, buyer fit, available competition/SERP evidence and existing page coverage. Explain
missing measurements and give qualitative priorities with their evidence. Recommend a niche
without measured demand only for an explicit buyer/product reason, not a traffic promise.
Group synonyms by intent without adding overlapping keyword volumes as unique traffic.
"""

INSTRUCTIONS["seeds"] = INSTRUCTIONS["seeds"].replace(
    "specific: one to four narrower problem or integration phrases",
    "specific: one to six narrower buyer-job or integration phrases",
)
INSTRUCTIONS["seeds"] += """
Use distinct supported jobs across the specific seeds instead of spending them all on one
framework. Prefer at least three jobs when product evidence supports them; fewer is appropriate
for a narrow product. All provider lookups remain within the run's existing spending ceiling.
"""
