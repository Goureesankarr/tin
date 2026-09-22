"""Narrow trusted OnPage adapter; no credentials or arbitrary provider tools in MCP.

Contract: https://docs.dataforseo.com/v3/on_page/task_post/
Recovery: https://docs.dataforseo.com/v3/appendix/id_list/
There are deliberately no automatic retries of chargeable POST requests.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx

from tin_lite.organic_audit import AUDIT_POLICY, public_site
from tin_lite.usage_capture import begin_observation, observe_tool


class DataForSEOError(RuntimeError):
    """Safe provider error: never include response bodies or credentials."""


class DataForSEO:
    def __init__(self, login: str, password: str, *, transport=None) -> None:
        self._auth = httpx.BasicAuth(login, password)
        self._transport = transport

    async def _request(self, method: str, endpoint: str, payload: list | None = None) -> dict:
        observation = await begin_observation("dataforseo", "tool", f"on_page/{endpoint}")
        try:
            async with (
                asyncio.timeout(40),
                httpx.AsyncClient(
                    auth=self._auth,
                    timeout=30,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._transport,
                ) as client,
                client.stream(
                    method, f"https://api.dataforseo.com/v3/on_page/{endpoint}", json=payload
                ) as response,
            ):
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 4_000_000:
                        raise DataForSEOError("Provider response exceeded the audit read limit.")
                data = json.loads(body)
            if not isinstance(data, dict) or data.get("status_code") != 20000:
                raise DataForSEOError("Provider request was not confirmed successful.")
            tasks = data.get("tasks")
            if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
                raise DataForSEOError("Provider returned an invalid task envelope.")
            task = tasks[0]
            await observe_tool(observation, task)
            if task.get("status_code") not in {20000, 20100, 40601, 40602}:
                raise DataForSEOError("Provider task was not successful or pending.")
            cost = Decimal(str(task.get("cost", 0)))
            if not cost.is_finite() or cost < 0:
                raise DataForSEOError("Provider returned invalid cost metadata.")
            return task
        except DataForSEOError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, TimeoutError) as exc:
            raise DataForSEOError("Provider request outcome is unavailable.") from exc

    @staticmethod
    async def validate_target(url: str) -> tuple[str, str]:
        url, host = public_site(url)
        # Tin never fetches this URL itself. The external crawler owns its own DNS
        # and redirect handling; this preflight is not a DNS-pinned Tin fetcher.
        try:
            async with asyncio.timeout(10):
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    host, 443, type=socket.SOCK_STREAM
                )
        except (OSError, TimeoutError) as exc:
            raise ValueError("The public website hostname could not be resolved.") from exc
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("The website must resolve only to public addresses.")
        return url, host

    @staticmethod
    def crawl_request(*, host: str, tag: str, respect_sitemap: bool = False) -> dict:
        public_site(f"https://{host}/")
        if type(respect_sitemap) is not bool:
            raise ValueError("respect_sitemap must be boolean")
        return {
            "target": host,
            "start_url": f"https://{host}/",
            "tag": tag,
            "max_crawl_pages": AUDIT_POLICY["max_pages"],
            "allow_subdomains": False,
            "enable_www_redirect_check": False,
            "respect_sitemap": respect_sitemap,
            "load_resources": False,
            "enable_javascript": False,
            "enable_browser_rendering": False,
            "enable_content_parsing": False,
            "store_raw_html": False,
            "crawl_delay": 2000,
        }

    async def submit(self, request: dict) -> dict:
        expected = self.crawl_request(
            host=request["target"],
            tag=request["tag"],
            respect_sitemap=request.get("respect_sitemap", False),
        )
        if request != expected:
            raise ValueError("Crawl request differs from the supported bounded contract.")
        task = await self._request("POST", "task_post", [request])
        if task.get("status_code") not in {20000, 20100}:
            raise DataForSEOError("Crawl submission was not acknowledged.")
        return {"task_id": str(UUID(task["id"])), "reported_cost_usd": str(task.get("cost", 0))}

    async def recover(self, *, request: dict, submitted_at: str) -> dict | None:
        """A tag correlates a request; it does NOT make task_post idempotent.

        No match or an incomplete listing never authorizes another paid submission.
        Account-wide metadata stays inside this trusted adapter.
        """
        start = datetime.fromisoformat(submitted_at) - timedelta(minutes=2)
        finish = start + timedelta(minutes=15)
        matches: dict[str, dict] = {}
        for offset in range(0, 5000, 1000):
            task = await self._request(
                "POST",
                "id_list",
                [
                    {
                        "datetime_from": start.strftime("%Y-%m-%d %H:%M:%S +00:00"),
                        "datetime_to": finish.strftime("%Y-%m-%d %H:%M:%S +00:00"),
                        "limit": 1000,
                        "offset": offset,
                        "include_metadata": True,
                        "sort": "asc",
                    }
                ],
            )
            rows = task.get("result")
            if not isinstance(rows, list):
                raise DataForSEOError("Task recovery metadata is unavailable.")
            for row in rows:
                metadata = row.get("metadata") or {}
                if all(metadata.get(key) == value for key, value in request.items()):
                    task_id = str(UUID(row["id"]))
                    matches[task_id] = {
                        "task_id": task_id,
                        "reported_cost_usd": (
                            str(row["cost"]) if row.get("cost") is not None else None
                        ),
                    }
            if len(rows) < 1000:
                if len(matches) > 1:
                    raise DataForSEOError("Multiple provider tasks match the saved request.")
                return next(iter(matches.values()), None)
        raise DataForSEOError("Task recovery reached its bounded listing limit.")

    async def summary(self, task_id: str) -> dict:
        task = await self._request("GET", f"summary/{UUID(task_id)}")
        if task["status_code"] in {40601, 40602}:
            return {"crawl_progress": "in_progress"}
        results = task.get("result")
        if not isinstance(results, list) or len(results) != 1:
            raise DataForSEOError("Crawl summary is unavailable.")
        raw = results[0]
        if raw.get("crawl_progress") not in {"in_progress", "finished"}:
            raise DataForSEOError("Crawl state is unknown.")
        domain = raw.get("domain_info") or {}
        return {
            "crawl_progress": raw["crawl_progress"],
            "crawl_status": raw.get("crawl_status", {}),
            "crawl_stop_reason": raw.get("crawl_stop_reason"),
            "domain": domain.get("name"),
            "extended_crawl_status": domain.get("extended_crawl_status"),
            "reported_cost_usd": str(task.get("cost", 0)),
        }

    async def pages(self, task_id: str, *, include_broken: bool = False) -> list[dict[str, Any]]:
        task = await self._request(
            "POST",
            "pages",
            [
                {
                    "id": str(UUID(task_id)),
                    "limit": AUDIT_POLICY["max_pages"],
                    "filters": [["resource_type", "in", ["html", "broken"]]]
                    if include_broken
                    else [["resource_type", "=", "html"]],
                    "order_by": ["url,asc"],
                }
            ],
        )
        results = task.get("result")
        if task["status_code"] != 20000 or not isinstance(results, list) or len(results) != 1:
            raise DataForSEOError("Crawl pages are unavailable.")
        items = results[0].get("items")
        if items is None and results[0].get("items_count") == 0:
            return []
        if not isinstance(items, list) or len(items) > AUDIT_POLICY["max_pages"]:
            raise DataForSEOError("Crawl pages exceeded their collection contract.")
        return items

    async def stop(self, task_id: str) -> None:
        await self._request("POST", "force_stop", [{"id": str(UUID(task_id))}])
