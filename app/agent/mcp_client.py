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
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from app.config import get_settings
from app.mcp_server.circuit_breaker import CircuitOpenError, get_breaker
from app.mcp_server.registry import ServerLocation, resolve_domain_for_tool, resolve_server_for_tool
from app.observability import record_tool_call


@asynccontextmanager
async def _session_at(location: ServerLocation):
    if location.transport == "http":
        # The gateway's streamable-HTTP transport requires an Authorization
        # bearer token (see mcp_server/server.py's _BearerTokenMiddleware) —
        # FastMCP applies no auth of its own, so without this any network
        # client that can reach the server could call sensitive tools
        # directly, bypassing the FastAPI layer's auth entirely.
        token = get_settings().mcp_server_token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
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

    tool_name is optional and defaults to resolving the identity domain
    (today equivalent to the one gateway location regardless, since all
    domains currently share it) — kept optional so existing call sites that
    don't know which specific tool they'll call yet (e.g. opening a session
    up front to reuse across a whole ticket run) still work unchanged.
    """
    location = resolve_server_for_tool(tool_name or "identity_")
    async with _session_at(location) as session:
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
        raise CircuitOpenError(
            f"Circuit breaker open for domain {domain!r} — too many recent "
            f"failures, refusing to call {name!r} until the recovery timeout elapses."
        )

    try:
        result = await session.call_tool(name, arguments)
    except Exception:
        breaker.record_failure()
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
        record_tool_call(name, ok=False, domain=domain)
        raise exc

    breaker.record_success()
    record_tool_call(name, ok=True, domain=domain)
    for block in result.content:
        if hasattr(block, "text"):
            return block.text
    return None


def is_transient_error(exc: Exception) -> bool:
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
