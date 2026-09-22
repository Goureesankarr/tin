"""Small connection inventory shared by evidence-consuming workflows; never opens secrets."""

from __future__ import annotations


async def integration_inventory(database, project_id) -> dict:
    reader = getattr(database, "list_integration_connections", None)
    if reader is None:
        return {"status": "unavailable", "connections": []}
    connections = await reader(project_id)
    rows = sorted(
        ({"provider": row.provider_key, "status": str(row.status)} for row in connections),
        key=lambda row: row["provider"],
    )
    return {
        "status": "observed",
        "connections": rows[:50],
        "truncated": len(rows) > 50,
        "meaning": (
            "Availability only. Check relevant authorized evidence; "
            "connection status does not establish a measured outcome."
        ),
    }
