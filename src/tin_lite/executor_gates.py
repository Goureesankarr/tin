"""Start-time readiness gates for provider-backed executors.

The run service raises these reasons when a start is refused; the growth onboarding plan
reads the same reasons so its appendix never sends an agent to a door that is locked.
"""

from __future__ import annotations

from typing import Any


def organic_audit_gate(settings: Any) -> str | None:
    if (
        not getattr(settings, "dataforseo_login", None)
        or not getattr(settings, "dataforseo_password", None)
        or getattr(settings, "organic_audit_max_cost_usd", 0) < 0.05
    ):
        return (
            "Organic audit requires a configured DataForSEO account and an explicit spending limit."
        )
    return None


def keyword_plan_gate(settings: Any) -> str | None:
    if (
        not getattr(settings, "dataforseo_login", None)
        or not getattr(settings, "dataforseo_password", None)
        or not getattr(settings, "luna_api_key", None)
        or getattr(settings, "keyword_plan_max_cost_usd", 0) < 5
    ):
        return (
            "Keyword planning requires DataForSEO, the native model, "
            "and an enabled spending ceiling of at least $5."
        )
    return None


def paid_ads_gate(settings: Any) -> str | None:
    if (
        not getattr(settings, "dataforseo_login", None)
        or not getattr(settings, "dataforseo_password", None)
        or not getattr(settings, "luna_api_key", None)
        or not getattr(settings, "gak_url", None)
        or not getattr(settings, "gak_token", None)
        or getattr(settings, "paid_ads_max_cost_usd", 0) < 3
    ):
        return (
            "Paid ads assessment requires DataForSEO, the Keyword Planner service, the native "
            "model and an enabled ceiling of at least $3."
        )
    return None


def organic_system_gate(settings: Any, *, keyword_max_cost_usd: float = 9) -> str | None:
    if (
        not getattr(settings, "dataforseo_login", None)
        or not getattr(settings, "dataforseo_password", None)
        or not getattr(settings, "luna_api_key", None)
        or getattr(settings, "organic_audit_max_cost_usd", 0) < 0.05
        or getattr(settings, "keyword_plan_max_cost_usd", 0) < keyword_max_cost_usd
        or getattr(settings, "content_plan_max_cost_usd", 0) < 1
    ):
        return (
            "Enable audit, keyword research and the $1 content-planning allowance first. "
            "No child workflow was started."
        )
    return None
