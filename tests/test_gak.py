"""Operator gak Keyword Planner client: bounded requests, pinned addresses, opaque failures."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from test_procedure_publication import run_fixture

from tin_lite.domain import EffectReceipt
from tin_lite.gak import (
    BOUNDS,
    ENDPOINTS,
    GakError,
    GoogleAdsKeywordData,
    client_from_settings,
    request_for,
    validate_base_url,
)
from tin_lite.settings import Settings
from tin_lite.usage_capture import external_usage_scope

REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TOKEN = "synthetic-gak-token-0123456789abcdef0123456789"  # noqa: S105
BASE = "https://gak.example:8443"
PUBLIC = "93.184.216.34"


def resolver_for(table):
    async def resolve(host, port, **_kwargs):
        addresses = table[host]
        if isinstance(addresses, str):
            addresses = [addresses]
        return [(None, None, None, None, (address, port)) for address in addresses]

    return resolve


def envelope(body, request_id, rows, **meta):
    payload = {
        "ok": True,
        "data": rows,
        "meta": {
            "cached": False,
            "row_count": len(rows),
            "request_id": request_id,
            "cost_usd": "0",
            "endpoint": "ideas",
            "country": body["country"],
            "language": body["language"],
            "api_version": "1",
            "schema_version": "1",
            "request": body,
            **meta,
        },
    }
    return payload


def row(keyword="crm software"):
    return {
        "keyword": keyword,
        "avg_monthly_searches": 1000,
        "competition": "HIGH",
        "competition_index": 90,
        "low_top_of_page_bid_micros": 1_000_000,
        "high_top_of_page_bid_micros": 5_000_000,
    }


# ---------------------------------------------------------------- request bodies


def test_ideas_request_omits_absent_fields_and_pins_a_request_id():
    body, request_id = request_for(
        "ideas", market="GB", value={"seeds": [" crm  software ", "b2b crm"], "limit": 50}, tag="t"
    )
    assert body == {
        "seeds": ["crm software", "b2b crm"],
        "include_adult": False,
        "limit": 50,
        "country": "GB",
        "language": "en",
    }
    assert REQUEST_ID.fullmatch(request_id) and request_id.startswith("tin-")
    assert request_id == request_for("ideas", market="GB", value={"seeds": ["x"]}, tag="t")[1]
    assert request_id != request_for("ideas", market="GB", value={"seeds": ["x"]}, tag="u")[1]
    with_url, _ = request_for(
        "ideas", market="US", value={"url": "https://shop.example/pricing", "limit": 10}, tag="t"
    )
    assert with_url == {
        "url": "https://shop.example/pricing",
        "include_adult": False,
        "limit": 10,
        "country": "US",
        "language": "en",
    }
    volume, _ = request_for("volume", market="CA", value={"keywords": ["a", "b"]}, tag="t")
    assert volume == {"keywords": ["a", "b"], "country": "CA", "language": "en"}
    cluster, _ = request_for("cluster", market="AU", value={"keywords": ["a"]}, tag="t")
    assert cluster == {"keywords": ["a"], "country": "AU", "language": "en"}


@pytest.mark.parametrize(
    ("kind", "market", "value"),
    [
        ("ideas", "US", {"seeds": ["s"] * (BOUNDS["seeds"] + 1), "limit": 10}),
        ("ideas", "US", {"seeds": [], "limit": 10}),
        ("ideas", "US", {"seeds": ["s"], "limit": 0}),
        ("ideas", "US", {"seeds": ["s"], "limit": BOUNDS["limit"] + 1}),
        ("ideas", "US", {"seeds": ["s"], "limit": True}),
        ("ideas", "US", {"seeds": ["s"], "limit": "10"}),
        ("ideas", "US", {"url": "http://shop.example/", "limit": 10}),
        ("ideas", "US", {"url": "https://user:pw@shop.example/", "limit": 10}),
        ("ideas", "US", {"url": "https://127.0.0.1/", "limit": 10}),
        ("ideas", "US", {"limit": 10}),
        ("ideas", "US", {"seeds": ["\x00"], "limit": 10}),
        ("ideas", "US", "crm"),
        ("volume", "US", {"keywords": ["k"] * (BOUNDS["volume_keywords"] + 1)}),
        ("volume", "US", {"keywords": []}),
        ("volume", "US", {"keywords": "k"}),
        ("cluster", "US", {"keywords": ["k"] * (BOUNDS["cluster_keywords"] + 1)}),
        ("ideas", "DE", {"seeds": ["s"], "limit": 10}),
        ("ideas", "us", {"seeds": ["s"], "limit": 10}),
        ("forecast", "US", {"seeds": ["s"], "limit": 10}),
    ],
)
def test_request_bounds_are_enforced(kind, market, value):
    with pytest.raises(ValueError):
        request_for(kind, market=market, value=value, tag="t")


# ---------------------------------------------------------------- base URL


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://gak.example", "https://gak.example"),
        ("https://gak.example/", "https://gak.example"),
        ("https://GAK.Example:8443", "https://gak.example:8443"),
        ("https://gak", "https://gak"),
        ("http://127.0.0.1", "http://127.0.0.1"),
        ("http://127.0.0.1:9000/", "http://127.0.0.1:9000"),
        ("http://[::1]:9000", "http://[::1]:9000"),
    ],
)
def test_base_url_accepts_dns_https_and_loopback_http(value, expected):
    assert validate_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://gak.example",
        "http://10.0.0.5:9000",
        "http://127.0.0.2",
        "http://localhost:9000",
        "https://127.0.0.1",
        "https://[::1]",
        "https://93.184.216.34",
        "https://user:pw@gak.example",
        "https://gak.example/api",
        "https://gak.example/api/",
        "https://gak.example?x=1",
        "https://gak.example#frag",
        "https://gak.example:bad",
        "https://gak.example ",
        "https://gak example",
        "https://gak_example.com",
        "https://gak.example.",
        "ftp://gak.example",
        "gak.example",
        "https://",
        "",
        None,
        42,
    ],
)
def test_base_url_refuses_everything_else(value):
    with pytest.raises(ValueError):
        validate_base_url(value)


# ---------------------------------------------------------------- transport


def client(handler, *, base=BASE, resolver=None, allow_private=True, token=TOKEN):
    return GoogleAdsKeywordData(
        base,
        token,
        transport=httpx.MockTransport(handler),
        resolver=resolver or resolver_for({"gak.example": PUBLIC}),
        allow_private=allow_private,
    )


async def test_happy_path_pins_the_address_and_returns_the_bounded_shape():
    body, request_id = request_for(
        "ideas", market="US", value={"seeds": ["crm software"], "limit": 5}, tag="tag-1"
    )
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json=envelope(body, request_id, [row(), row("crm tools")], cached=True),
            headers={"X-Request-ID": request_id},
        )

    provider = client(handler)
    result = await provider.query(
        "ideas", market="US", value={"seeds": ["crm software"], "limit": 5}, tag="tag-1"
    )
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.host == PUBLIC and request.url.port == 8443
    assert request.url.path == "/" + ENDPOINTS["ideas"]
    assert request.headers["host"] == "gak.example:8443"
    assert request.extensions["sni_hostname"] == "gak.example"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["x-request-id"] == request_id
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["user-agent"] == "Tin-GAK/1"
    assert json.loads(request.content) == body
    assert result == {
        "items": [row(), row("crm tools")],
        "items_count": 2,
        "reported_cost_usd": "0",
        "provider_task_id": request_id,
        "cached": True,
    }


async def test_volume_and_cluster_bound_rows_by_the_keyword_count():
    keywords = ["a", "b", "c"]
    calls = []

    def handler(request):
        calls.append(request)
        sent = json.loads(request.content)
        rows = [{"keyword": k, "monthly_search_volumes": []} for k in sent["keywords"]]
        if request.url.path.endswith("/cluster"):
            rows = [{"theme": "t", "keywords": sent["keywords"], "keyword_count": 3}]
        return httpx.Response(
            200,
            json=envelope(sent, request.headers["x-request-id"], rows),
            headers={"X-Request-ID": request.headers["x-request-id"]},
        )

    provider = client(handler)
    volume = await provider.query("volume", market="US", value={"keywords": keywords}, tag="v")
    cluster = await provider.query("cluster", market="US", value={"keywords": keywords}, tag="c")
    assert volume["items_count"] == 3 and cluster["items_count"] == 1
    assert [c.url.path for c in calls] == ["/api/v1/keywords/volume", "/api/v1/cluster"]


@pytest.mark.parametrize("allow_private", [True, False])
async def test_loopback_http_requires_the_private_flag(allow_private):
    calls = []

    def handler(request):
        calls.append(request)
        sent = json.loads(request.content)
        return httpx.Response(
            200,
            json=envelope(sent, request.headers["x-request-id"], []),
            headers={"X-Request-ID": request.headers["x-request-id"]},
        )

    provider = client(
        handler,
        base="http://127.0.0.1:9000",
        resolver=resolver_for({"127.0.0.1": "127.0.0.1"}),
        allow_private=allow_private,
    )
    coroutine = provider.query("volume", market="US", value={"keywords": ["a"]}, tag="t")
    if allow_private:
        result = await coroutine
        assert result["items"] == [] and calls[0].url.host == "127.0.0.1"
        assert calls[0].headers["host"] == "127.0.0.1:9000"
    else:
        with pytest.raises(GakError):
            await coroutine
        assert calls == []


@pytest.mark.parametrize(
    ("addresses", "allow_private"),
    [
        (["169.254.169.254"], True),
        (["224.0.0.1"], True),
        (["::ffff:127.0.0.1"], False),
        (["0.0.0.0"], True),  # noqa: S104
        (["::"], True),
        ([PUBLIC, "10.0.0.5"], False),
        ([], True),
        (["not-an-ip"], True),
    ],
)
async def test_refused_addresses_never_connect(addresses, allow_private):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    provider = client(
        handler, resolver=resolver_for({"gak.example": addresses}), allow_private=allow_private
    )
    with pytest.raises(GakError):
        await provider.query("volume", market="US", value={"keywords": ["a"]}, tag="t")
    assert calls == []


async def test_public_address_needs_no_flag_and_mixed_resolution_keeps_first_route():
    calls = []

    def handler(request):
        calls.append(request)
        sent = json.loads(request.content)
        return httpx.Response(
            200,
            json=envelope(sent, request.headers["x-request-id"], []),
            headers={"X-Request-ID": request.headers["x-request-id"]},
        )

    provider = client(
        handler,
        resolver=resolver_for({"gak.example": ["2606:2800:220:1:248:1893:25c8:1946", PUBLIC]}),
        allow_private=False,
    )
    await provider.query("volume", market="US", value={"keywords": ["a"]}, tag="t")
    assert calls[0].url.host == "2606:2800:220:1:248:1893:25c8:1946"


FAILURES = [
    "redirect",
    "gzip",
    "oversize",
    "unauthorized",
    "rate_limited",
    "bad_gateway",
    "wrong_request_id",
    "wrong_header_echo",
    "country_mismatch",
    "request_mismatch",
    "cost",
    "nan_cost",
    "too_many_rows",
    "non_dict_row",
    "row_count",
    "not_ok",
    "http_500",
    "token_reflected",
    "token_base64",
    "not_json",
    "connect_error",
    "timeout",
]


@pytest.mark.parametrize("failure", FAILURES)
async def test_failures_are_opaque_and_never_retried(failure):
    value = {"seeds": ["crm software"], "limit": 2}
    body, request_id = request_for("ideas", market="US", value=value, tag="t")
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "connect_error":
            raise httpx.ConnectError("refused")
        if failure == "timeout":
            raise httpx.ReadTimeout("slow")
        headers = {"X-Request-ID": request_id}
        payload = envelope(body, request_id, [row()])
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.example/"})
        if failure == "gzip":
            return httpx.Response(
                200, content=b"x", headers={**headers, "content-encoding": "gzip"}
            )
        if failure == "oversize":
            return httpx.Response(200, content=b"[" + b" " * 3_999_999 + b"]", headers=headers)
        if failure in {"unauthorized", "rate_limited", "bad_gateway"}:
            status = {"unauthorized": 401, "rate_limited": 429, "bad_gateway": 502}[failure]
            return httpx.Response(
                status,
                json={
                    "ok": False,
                    "error": {"code": failure, "message": "refused", "details": {}},
                    "meta": {"request_id": request_id},
                },
                headers={**headers, "retry-after": "5"},
            )
        if failure == "wrong_request_id":
            payload["meta"]["request_id"] = "tin-other"
        if failure == "wrong_header_echo":
            headers["X-Request-ID"] = "tin-other"
        if failure == "country_mismatch":
            payload["meta"]["request"] = {**body, "country": "GB"}
        if failure == "request_mismatch":
            payload["meta"]["request"] = {**body, "limit": 200}
        if failure == "cost":
            payload["meta"]["cost_usd"] = "0.01"
        if failure == "nan_cost":
            payload["meta"]["cost_usd"] = "NaN"
        if failure == "too_many_rows":
            payload["data"] = [row(), row(), row()]
            payload["meta"]["row_count"] = 3
        if failure == "non_dict_row":
            payload["data"] = [["keyword"]]
        if failure == "row_count":
            payload["meta"]["row_count"] = 2
        if failure == "not_ok":
            payload["ok"] = False
        if failure == "http_500":
            return httpx.Response(500, json=payload, headers=headers)
        if failure == "token_reflected":
            payload["data"][0]["keyword"] = f"echo {TOKEN}"
        if failure == "token_base64":
            import base64

            payload["data"][0]["keyword"] = base64.b64encode(TOKEN.encode()).decode()
        if failure == "not_json":
            return httpx.Response(200, content=b"<html>", headers=headers)
        return httpx.Response(200, json=payload, headers=headers)

    provider = client(handler)
    with pytest.raises(GakError, match="could not be confirmed") as info:
        await provider.query("ideas", market="US", value=value, tag="t")
    assert len(calls) == 1
    assert info.value.__cause__ is None and TOKEN not in str(info.value)


# ---------------------------------------------------------------- settings


def settings_values(monkeypatch, **overrides):
    for field in Settings.model_fields.values():
        monkeypatch.delenv(field.alias, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    values = {
        field.alias: 5432 if name.endswith("port") else "synthetic"
        for name, field in Settings.model_fields.items()
        if field.is_required()
    }
    values["CLERK_PUBLISHABLE_KEY"] = "pk_test_synthetic"
    values.update(overrides)
    return values


def test_settings_require_url_and_token_together(monkeypatch):
    with pytest.raises(ValueError, match="TIN_LITE_GAK_URL and TIN_LITE_GAK_TOKEN"):
        Settings(_env_file=None, **settings_values(monkeypatch, TIN_LITE_GAK_URL=BASE))
    with pytest.raises(ValueError, match="TIN_LITE_GAK_URL and TIN_LITE_GAK_TOKEN"):
        Settings(_env_file=None, **settings_values(monkeypatch, TIN_LITE_GAK_TOKEN=TOKEN))
    with pytest.raises(ValueError, match="TIN_LITE_GAK_TOKEN must be at least 32"):
        Settings(
            _env_file=None,
            **settings_values(monkeypatch, TIN_LITE_GAK_URL=BASE, TIN_LITE_GAK_TOKEN="short"),  # noqa: S106
        )
    settings = Settings(
        _env_file=None,
        **settings_values(monkeypatch, TIN_LITE_GAK_URL=BASE + "/", TIN_LITE_GAK_TOKEN=TOKEN),
    )
    assert settings.gak_url == BASE and settings.gak_allow_private is False
    assert TOKEN not in repr(settings)
    assert isinstance(client_from_settings(settings), GoogleAdsKeywordData)
    assert client_from_settings(Settings(_env_file=None, **settings_values(monkeypatch))) is None


def test_settings_plain_http_is_loopback_only_and_flagged(monkeypatch):
    loopback = dict(TIN_LITE_GAK_URL="http://127.0.0.1:9000", TIN_LITE_GAK_TOKEN=TOKEN)
    with pytest.raises(ValueError, match="TIN_LITE_GAK_ALLOW_PRIVATE"):
        Settings(_env_file=None, **settings_values(monkeypatch, **loopback))
    settings = Settings(
        _env_file=None,
        **settings_values(monkeypatch, **loopback, TIN_LITE_GAK_ALLOW_PRIVATE="true"),
    )
    assert settings.gak_url == "http://127.0.0.1:9000" and settings.gak_allow_private is True
    with pytest.raises(ValueError, match="TIN_LITE_GAK_URL"):
        Settings(
            _env_file=None,
            **settings_values(
                monkeypatch,
                TIN_LITE_GAK_URL="http://gak.example",
                TIN_LITE_GAK_TOKEN=TOKEN,
                TIN_LITE_GAK_ALLOW_PRIVATE="true",
            ),
        )


# ---------------------------------------------------------------- receipts


class MemoryDB:
    """Exercise receipt decisions; separate Postgres tests prove SQL atomicity."""

    def __init__(self):
        self.run = run_fixture()
        self.effects = {}
        self.locks = {}

    async def get_effect(self, key, **kwargs):
        return self.effects.get(key)

    async def fetchval(self, *args):
        return None  # The locked connection finds no run budget, so nothing is priced.

    @asynccontextmanager
    async def effect_lock(self, key, operation, **kwargs):
        async with self.locks.setdefault(key, asyncio.Lock()):
            yield self, self.effects.get(key)

    async def start_effect(self, conn, *, execution_key, operation):
        self.effects.setdefault(
            execution_key, EffectReceipt(execution_key, operation, "started", {})
        )

    async def save_effect_progress(self, conn, *, execution_key, result):
        self.effects[execution_key] = replace(self.effects[execution_key], result=result)

    async def complete_effect(self, conn, *, execution_key, result):
        self.effects[execution_key] = replace(
            self.effects[execution_key], status="completed", result=result
        )


async def test_observation_receipt_reports_provider_and_zero_cost():
    db = MemoryDB()

    def handler(request):
        sent = json.loads(request.content)
        return httpx.Response(
            200,
            json=envelope(sent, request.headers["x-request-id"], [row()]),
            headers={"X-Request-ID": request.headers["x-request-id"]},
        )

    provider = client(handler)
    with external_usage_scope(db, None, db.run.id, "ideas"):
        await provider.query("ideas", market="US", value={"seeds": ["a"], "limit": 1}, tag="t")
    (receipt,) = db.effects.values()
    assert receipt.status == "completed" and receipt.operation == "external_usage_v1"
    assert receipt.result["provider"] == "gak" and receipt.result["category"] == "tool"
    assert receipt.result["endpoint"] == ENDPOINTS["ideas"]
    assert receipt.result["outcome"] == "response_received"
    assert receipt.result["reported_cost_usd"] == "0"
    assert receipt.result["usage"] == {"requests": 1}
    assert TOKEN not in json.dumps(receipt.result)
    # A repeated step never dispatches a second request behind an unreconciled receipt.
    with pytest.raises(RuntimeError), external_usage_scope(db, None, db.run.id, "ideas"):
        await provider.query("ideas", market="US", value={"seeds": ["a"], "limit": 1}, tag="t")


@pytest.mark.parametrize(
    ("failure", "kind", "status_code"),
    [
        ("bad_gateway", "http", 502),
        ("redirect", "http", 302),
        ("wrong_request_id", "api", 200),
        ("connect_error", "connection", None),
        ("timeout", "timeout", None),
        ("private_address", "connection", None),
    ],
)
async def test_failed_observation_keeps_bounded_transport_facts(failure, kind, status_code):
    db = MemoryDB()

    def handler(request):
        if failure == "connect_error":
            raise httpx.ConnectError("secret detail")
        if failure == "timeout":
            raise httpx.ReadTimeout("secret detail")
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.example/"})
        if failure == "wrong_request_id":
            sent = json.loads(request.content)
            return httpx.Response(
                200, json=envelope(sent, "tin-other", []), headers={"X-Request-ID": "tin-other"}
            )
        return httpx.Response(
            502,
            json={"ok": False, "error": {"code": "upstream", "message": "secret detail"}},
        )

    provider = client(
        handler,
        resolver=resolver_for(
            {"gak.example": "10.0.0.5" if failure == "private_address" else PUBLIC}
        ),
        allow_private=False,
    )
    with pytest.raises(GakError), external_usage_scope(db, None, db.run.id, "volume"):
        await provider.query("volume", market="US", value={"keywords": ["a"]}, tag="t")
    (receipt,) = db.effects.values()
    assert receipt.status == "started" and receipt.result["outcome"] == "unconfirmed"
    assert receipt.result["failure"]["kind"] == kind
    assert receipt.result["failure"].get("status_code") == status_code
    assert receipt.result["reported_cost_usd"] is None
    assert "secret detail" not in json.dumps(receipt.result)
