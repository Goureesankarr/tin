"""Operator-run Google Ads Keyword Planner service (gak) client.

Allowlisted endpoints, bounded bodies, a DNS-pinned connection to a vetted address and an
opaque failure. The service reports no per-request cost; each call still leaves a receipt.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import re
import socket
from decimal import Decimal
from urllib.parse import quote, urlsplit

import httpx

from tin_lite.keyword_plan import phrase, safe_url
from tin_lite.organic_audit import MARKETS
from tin_lite.usage_capture import begin_observation, observe_failure, observe_tool

ENDPOINTS = {
    "ideas": "api/v1/keywords/ideas",
    "volume": "api/v1/keywords/volume",
    "cluster": "api/v1/cluster",
}
BOUNDS = {"seeds": 20, "limit": 200, "volume_keywords": 200, "cluster_keywords": 100}
RESPONSE_BYTES = 4_000_000
LANGUAGE = "en"
MESSAGE = "Keyword service outcome could not be confirmed."
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class GakError(RuntimeError):
    """Opaque keyword-service failure; transport and envelope details never leave here."""


class _Refused(Exception):
    def __init__(self, kind: str, status_code: int | None = None):
        super().__init__(kind)
        self.kind = kind
        self.status_code = status_code


def validate_base_url(value) -> str:
    """Accept an HTTPS DNS origin or a plain-HTTP loopback origin; nothing else."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 500
        or any(ord(c) < 33 or ord(c) == 127 for c in value)
    ):
        raise ValueError("Keyword service URL must be a single origin without whitespace.")
    parsed = urlsplit(value)
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise ValueError("Keyword service URL must be a bare origin without credentials or path.")
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("Keyword service URL has an invalid port.") from None
    if not host:
        raise ValueError("Keyword service URL requires a host.")
    literal = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    if parsed.scheme == "https":
        if literal is not None or len(host) > 253 or host.endswith("."):
            raise ValueError("Keyword service HTTPS URL requires a DNS hostname.")
        if any(not _LABEL.fullmatch(label) for label in host.split(".")):
            raise ValueError("Keyword service HTTPS URL requires a DNS hostname.")
        authority = host
    elif parsed.scheme == "http":
        if literal is None or not literal.is_loopback or str(literal) not in LOOPBACK_HOSTS:
            raise ValueError("Keyword service plain-HTTP URL must point at loopback.")
        authority = f"[{literal}]" if literal.version == 6 else str(literal)
    else:
        raise ValueError("Keyword service URL must use https, or http on loopback.")
    if port is not None:
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}"


def _phrases(values, *, bound: int, label: str) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= bound:
        raise ValueError(f"Supply between one and {bound} {label}.")
    return [phrase(item) for item in values]


def _limit(value) -> int:
    if isinstance(value, bool) or type(value) is not int or not 1 <= value <= BOUNDS["limit"]:
        raise ValueError(f"Keyword idea limit must be between 1 and {BOUNDS['limit']}.")
    return value


def request_for(kind: str, *, market: str, value, tag: str) -> tuple[dict, str]:
    """Return the service body and the deterministic request ID for a pinned tag."""
    if market not in MARKETS:
        raise ValueError("Unknown keyword market.")
    if not isinstance(tag, str) or not tag:
        raise ValueError("Keyword requests require a tag.")
    request_id = "tin-" + hashlib.sha256(tag.encode()).hexdigest()[:40]
    if not isinstance(value, dict):
        raise ValueError("Keyword requests take a mapping of inputs.")
    if kind == "ideas":
        seeds, url = value.get("seeds"), value.get("url")
        body: dict = {}
        if seeds is not None:
            body["seeds"] = _phrases(seeds, bound=BOUNDS["seeds"], label="seed phrases")
        if url is not None:
            if not isinstance(url, str) or not url.startswith("https://") or safe_url(url) is None:
                raise ValueError("Keyword idea URLs must be public HTTPS addresses.")
            # The service echoes a normalized origin with a trailing slash; send the same form.
            body["url"] = url if urlsplit(url).path else url + "/"
        if not body:
            raise ValueError("Keyword ideas require seed phrases or a URL.")
        body["include_adult"] = False
        body["limit"] = _limit(value.get("limit", BOUNDS["limit"]))
    elif kind in {"volume", "cluster"}:
        bound = BOUNDS["volume_keywords" if kind == "volume" else "cluster_keywords"]
        body = {"keywords": _phrases(value.get("keywords"), bound=bound, label="keywords")}
    else:
        raise ValueError("Unknown keyword service endpoint.")
    body["country"] = market
    body["language"] = LANGUAGE
    return body, request_id


def _vetted_addresses(rows, *, allow_private: bool) -> list[str]:
    ips = []
    for row in rows:
        try:
            address = ipaddress.ip_address(row[4][0])
        except (ValueError, TypeError, IndexError):
            raise _Refused("connection") from None
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        if (
            address.is_multicast
            or address.is_reserved
            or address.is_link_local
            or address.is_unspecified
        ):
            raise _Refused("connection")
        if not address.is_global and not (
            allow_private and (address.is_loopback or address.is_private)
        ):
            raise _Refused("connection")
        if str(address) not in ips:
            ips.append(str(address))
    if not ips:
        raise _Refused("connection")
    return ips


def _credential_forms(token: str) -> set[str]:
    return {
        token,
        json.dumps(token, ensure_ascii=False)[1:-1],
        json.dumps(token, ensure_ascii=True)[1:-1],
        quote(token, safe=""),
        base64.b64encode(token.encode()).decode(),
    }


class GoogleAdsKeywordData:
    def __init__(
        self, base_url: str, token: str, *, transport=None, resolver=None, allow_private=False
    ):
        self._base = validate_base_url(base_url)
        if not isinstance(token, str) or not token:
            raise ValueError("Keyword service token is required.")
        self._token = token
        self._transport = transport
        self._resolver = resolver
        self._allow_private = bool(allow_private)

    async def query(self, kind: str, *, market: str, value, tag: str) -> dict:
        body, request_id = request_for(kind, market=market, value=value, tag=tag)
        observation = await begin_observation("gak", "tool", ENDPOINTS[kind])
        try:
            async with asyncio.timeout(50):
                data, cached = await self._exchange(kind, body, request_id)
        except _Refused as refused:
            await observe_failure(observation, kind=refused.kind, status_code=refused.status_code)
            raise GakError(MESSAGE) from None
        except (TimeoutError, httpx.TimeoutException):
            await observe_failure(observation, kind="timeout")
            raise GakError(MESSAGE) from None
        except (httpx.TransportError, OSError):
            await observe_failure(observation, kind="connection")
            raise GakError(MESSAGE) from None
        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
            KeyError,
            ArithmeticError,
            UnicodeError,
            RecursionError,
        ):
            await observe_failure(observation, kind="api")
            raise GakError(MESSAGE) from None
        await observe_tool(observation, {"cost": "0"})
        return {
            "items": data,
            "items_count": len(data),
            "reported_cost_usd": "0",
            "provider_task_id": request_id,
            "cached": cached,
        }

    async def _exchange(self, kind: str, body: dict, request_id: str) -> tuple[list, bool]:
        parsed = urlsplit(self._base)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        resolver = self._resolver or asyncio.get_running_loop().getaddrinfo
        rows = await resolver(host, port, type=socket.SOCK_STREAM)
        # Keep the resolver's route preference; lexical sorting can select unreachable IPv6.
        ips = _vetted_addresses(rows, allow_private=self._allow_private)
        headers = {
            "Host": parsed.netloc,
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "User-Agent": "Tin-GAK/1",
            "X-Request-ID": request_id,
        }
        content = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        url = httpx.URL(f"{self._base}/{ENDPOINTS[kind]}").copy_with(host=ips[0])
        async with (
            httpx.AsyncClient(
                timeout=45, follow_redirects=False, trust_env=False, transport=self._transport
            ) as client,
            client.stream(
                "POST",
                url,
                content=content,
                headers=headers,
                extensions={"sni_hostname": host},
            ) as response,
        ):
            status = response.status_code
            # Compressed responses are refused before decompression allocation.
            if response.headers.get("content-encoding", "identity") != "identity":
                raise _Refused("http", status)
            if 300 <= status < 400 or status != 200:
                raise _Refused("http", status)
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > RESPONSE_BYTES:
                    raise _Refused("http", status)
            echoed = response.headers.get("x-request-id")
        payload = json.loads(raw)
        safe = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        # Never let an auth echo become model context, a receipt or an artifact.
        if any(form in safe for form in _credential_forms(self._token)):
            raise _Refused("api", status)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise _Refused("api", status)
        meta, data = payload.get("meta"), payload.get("data")
        if not isinstance(meta, dict) or not isinstance(data, list):
            raise _Refused("api", status)
        if meta.get("request_id") != request_id or echoed != request_id:
            raise _Refused("api", status)
        if meta.get("request") != body:
            raise _Refused("api", status)
        if meta.get("country") != body["country"] or meta.get("language") != body["language"]:
            raise _Refused("api", status)
        cost = Decimal(str(meta.get("cost_usd")))
        if not cost.is_finite() or cost != 0:
            raise _Refused("api", status)
        if any(not isinstance(row, dict) for row in data):
            raise _Refused("api", status)
        bound = body["limit"] if kind == "ideas" else len(body["keywords"])
        if len(data) > bound:
            raise _Refused("api", status)
        row_count = meta.get("row_count")
        if type(row_count) is not int or row_count != len(data):
            raise _Refused("api", status)
        return data, bool(meta.get("cached"))


def client_from_settings(settings) -> GoogleAdsKeywordData | None:
    url, token = getattr(settings, "gak_url", None), getattr(settings, "gak_token", None)
    if not url or token is None:
        return None
    return GoogleAdsKeywordData(
        url,
        token.get_secret_value(),
        allow_private=bool(getattr(settings, "gak_allow_private", False)),
    )
