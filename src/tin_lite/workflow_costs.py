"""Reusable configured cost policies. No supplier call or per-launch estimation job."""

from functools import lru_cache

from tin_lite.billing_contracts import NANOS_PER_CENT, digest

FUNDING = "per_operation_v1"
SESSION_FUNDING = "procedure_session_v1"
POLICY = "configured-cost-bound-v1"


@lru_cache(maxsize=512)
def _estimate(definition_digest, scope_digest, price_digest, maximum):
    # Until a workflow has a calibrated lower estimate, its authored spending bound
    # is the honest conservative estimate. Do not learn from another user's runs.
    return (
        digest([POLICY, definition_digest, scope_digest, price_digest, maximum]),
        maximum,
    )


def configured_terms(terms, definition, inputs):
    if terms["kind"] == "included":
        return terms
    estimate_id, amount = _estimate(
        digest(definition), digest(inputs or {}), digest(terms), terms["maximum_nanos"]
    )
    return {
        **terms,
        "funding": terms.get("funding", FUNDING),
        "estimate": {
            "id": estimate_id,
            "policy": POLICY,
            "basis": "conservative_configured_bound",
            "amount_nanos": amount,
        },
    }


def incremental(terms):
    return terms.get("funding") == FUNDING


def session_funded(terms):
    return terms.get("funding") == SESSION_FUNDING


def liability(terms, committed):
    """Protect root-level rounding without rounding each individual supplier call."""
    if not incremental(terms):
        return terms["maximum_nanos"]
    amount = committed + (terms["execution_fee_nanos"] if committed else 0)
    return min(
        terms["maximum_nanos"],
        ((amount + NANOS_PER_CENT - 1) // NANOS_PER_CENT) * NANOS_PER_CENT,
    )
