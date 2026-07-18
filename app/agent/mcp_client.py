"""Agent-side MCP client.

Talks to the custom MCP server over JSON-RPC, either by spawning it as a
local stdio subprocess (default, zero config) or by connecting to a remote
server over streamable-HTTP — the same two integration paths a real
orchestrator (e.g. watsonx Orchestrate) supports when registering an
external MCP tool server, as opposed to importing the tool functions
directly in-process. The transport/location for a given tool call is
resolved via app.mcp_server.registry, which today maps every domain to the
same one gateway process — see registry.py for why that indirection exists.
"""

import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from app import metrics
from app.config import get_settings
from app.mcp_server.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState, get_breaker
from app.mcp_server.registry import (
    ServerLocation,
    get_registry,
    resolve_domain_for_tool,
    resolve_server_for_tool,
)
from app.observability import record_tool_call

# Scoped-token cache for the HTTP transport (app/mcp_server/token_exchange.py):
# keyed by a frozenset of domains rather than a single domain, since the
# only pattern any real caller uses today is "one session shared across a
# whole ticket run" (see mcp_session()'s tool_name=None default below and
# app/agent/mcp_session_cache.py's owner task) — that session needs a token
# covering EVERY domain it might touch (an offboarding ticket calls both
# identity_disable_user and access_revoke_access through the SAME session),
# not one domain. Module-level (not per-run) since the token is a bearer
# credential for the gateway itself, not tied to any one ticket, and reuse
# across runs avoids a token-exchange round-trip on every ticket.
_scoped_token_cache: dict[frozenset[str], tuple[str, float]] = {}
_TOKEN_REFRESH_SAFETY_BUFFER_SECONDS = 30.0


def _exchange_url(mcp_url: str) -> str:
    parts = urlsplit(mcp_url)
    return urlunsplit((parts.scheme, parts.netloc, "/token/exchange", "", ""))


async def _get_scoped_token(mcp_url: str, domains: frozenset[str], admin_token: str) -> str:
    """Returns a cached-if-still-fresh, else newly-exchanged, scoped token
    covering exactly `domains`. Exchanges using the raw admin token
    (MCP_SERVER_TOKEN) — the only credential POST /token/exchange accepts
    — so this is the one place that secret is still used directly; every
    actual MCP protocol request afterward uses the derived token instead.
    """
    cached = _scoped_token_cache.get(domains)
    now = time.time()
    if cached is not None and cached[1] - _TOKEN_REFRESH_SAFETY_BUFFER_SECONDS > now:
        return cached[0]

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            _exchange_url(mcp_url),
            json={"scopes": sorted(domains)},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    response.raise_for_status()
    body = response.json()
    token = body["access_token"]
    _scoped_token_cache[domains] = (token, now + body.get("expires_in", 0))
    return token


def _sync_breaker_gauge(domain: str, breaker: CircuitBreaker) -> None:
    """Mirrors the domain's breaker state onto the mcp_circuit_breaker_open
    gauge (1 = open or half-open, 0 = closed) so an alert rule can fire on
    "a backend domain has been tripped for N minutes" without log-scraping.
    Called after every state-changing breaker interaction rather than on a
    timer — the gauge is only ever as stale as the last call attempt, and a
    domain with no traffic has no failures to alert on anyway.
    """
    metrics.CIRCUIT_BREAKER_OPEN.labels(domain=domain).set(
        0 if breaker.state == CircuitState.CLOSED else 1
    )


@asynccontextmanager
async def _session_at(location: ServerLocation, domains: frozenset[str]):
    if location.transport == "http":
        # The gateway's streamable-HTTP transport requires an Authorization
        # bearer token (see mcp_server/server.py's _BearerTokenMiddleware) —
        # FastMCP applies no auth of its own, so without this any network
        # client that can reach the server could call sensitive tools
        # directly, bypassing the FastAPI layer's auth entirely.
        if location.url is None:
            raise RuntimeError(
                "Server registry maps a domain to transport 'http' with no url — "
                "check MCP_SERVER_URL / app.mcp_server.registry."
            )
        admin_token = get_settings().mcp_server_token
        if admin_token:
            # Exchange for a scoped, short-lived token (token_exchange.py)
            # rather than sending the raw admin secret on every request —
            # see the module-level _scoped_token_cache docstring above for
            # why `domains` covers every domain this session might touch,
            # not just one. Falls through to the empty-headers branch below
            # only when admin_token itself is unset, matching this
            # transport's pre-existing "server refuses to start without a
            # token" behavior — an agent misconfigured the same way gets
            # the same 401 from the server it always would have.
            token = await _get_scoped_token(location.url, domains, admin_token)
            headers = {"Authorization": f"Bearer {token}"}
        else:
            headers = {}
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client(location.url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        return

    # mcp.client.stdio.get_default_environment() only inherits a small
    # security-allowlisted set of OS vars (PATH, HOME, etc.) when env=None —
    # it silently drops app config like DATABASE_URL, so the spawned server
    # would fall back to config.py's defaults instead of this process's
    # actual settings. Pass the full parent environment through explicitly.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server.server"],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


@asynccontextmanager
async def mcp_session(tool_name: str | None = None):
    """Opens a session at the location the registry resolves for tool_name.

    tool_name is optional and defaults to resolving the identity domain for
    LOCATION purposes (today equivalent to the one gateway location
    regardless, since all domains currently share it) — kept optional so
    existing call sites that don't know which specific tool they'll call
    yet (e.g. opening a session up front to reuse across a whole ticket
    run — every real call site in this codebase does exactly this; see
    app/agent/mcp_session_cache.py's owner task) still work unchanged.

    For the HTTP transport's scoped token (_session_at above), tool_name=None
    requests a token covering EVERY registered domain rather than just
    identity — a session opened without knowing which tool(s) it'll serve
    must be able to serve any of them (that shared-session-per-ticket-run
    pattern is the only one any caller uses today). Passing an explicit
    tool_name narrows the token to just that tool's domain — not exercised
    by any current call site, but the correct behavior if one is ever added
    (e.g. a future single-purpose integration that only ever calls one
    domain's tools and should get a genuinely least-privilege token).
    """
    location = resolve_server_for_tool(tool_name or "identity_")
    domains = frozenset({resolve_domain_for_tool(tool_name)}) if tool_name else frozenset(get_registry())
    async with _session_at(location, domains) as session:
        yield session


async def list_tools(session: ClientSession) -> list[dict]:
    result = await session.list_tools()
    return [
        {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
        for t in result.tools
    ]


async def call_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> Any:
    """Calls a tool through a per-domain circuit breaker: a sustained run of
    transient failures against one domain (e.g. the ticketing backend down)
    trips that domain's breaker and fails fast for further calls to it,
    without touching the other domains' breakers — they have nothing to do
    with it. Application-logic failures (ToolError-shaped RuntimeErrors,
    e.g. "user already disabled") do NOT count against the breaker, since
    they say nothing about backend health.
    """
    domain = resolve_domain_for_tool(name)
    breaker = get_breaker(domain)
    if not breaker.allow_request():
        _sync_breaker_gauge(domain, breaker)
        raise CircuitOpenError(
            f"Circuit breaker open for domain {domain!r} — too many recent "
            f"failures, refusing to call {name!r} until the recovery timeout elapses."
        )

    try:
        result = await session.call_tool(name, arguments)
    except Exception:
        breaker.record_failure()
        _sync_breaker_gauge(domain, breaker)
        record_tool_call(name, ok=False, domain=domain)
        raise

    if result.isError:
        text = "; ".join(
            block.text for block in result.content if hasattr(block, "text")
        )
        exc = RuntimeError(f"MCP tool {name!r} failed: {text}")
        if is_transient_error(exc):
            breaker.record_failure()
        else:
            breaker.record_success()
        _sync_breaker_gauge(domain, breaker)
        record_tool_call(name, ok=False, domain=domain)
        raise exc

    breaker.record_success()
    _sync_breaker_gauge(domain, breaker)
    record_tool_call(name, ok=True, domain=domain)
    for block in result.content:
        if hasattr(block, "text"):
            return block.text
    return None


def is_transient_error(exc: BaseException) -> bool:
    """Distinguishes retryable/transport failures from permanent
    application-logic ones. Shared by the agent's node-level RetryPolicy
    (app/agent/graph.py) and the per-domain circuit breaker
    (app/mcp_server/circuit_breaker.py) — both need the same classification:
    a RuntimeError from call_tool() above specifically means the MCP tool
    executed and reported an application-level failure (e.g.
    ToolError("User already disabled") crossing the wire) — that's a logic
    error, not a transport hiccup. Retrying it would just re-fail
    identically, and it says nothing about the backend's health, so it
    must not trip the circuit breaker either. Genuine transport failures
    (dropped connection, subprocess death, timeout) surface as different
    exception types before ever reaching that wrapping, so they fall
    through to the transient branch below. LLM provider rate-limit/server
    errors (OpenAI/Anthropic/httpx) are also transient and worth retrying
    with backoff, though they're unrelated to MCP backend health.
    """
    if isinstance(exc, RuntimeError):
        return False
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return False
    return True
