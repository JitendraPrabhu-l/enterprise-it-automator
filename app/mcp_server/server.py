"""Gateway MCP server: composes the identity/access/ticketing domain servers
onto ONE process, exposing each domain's tools under a namespaced name
(identity_get_user, access_grant_access, ticketing_add_ticket_comment, ...).

Every sensitive mutation (identity_disable_user, access_revoke_access)
requires a pre-approved `approval_id` minted by the FastAPI HITL flow,
enforced server-side in each domain server's use of
approval_gate.require_approval — the LLM cannot talk its way past this by
claiming an action is authorized.

Composed via add_tool() rather than FastMCP.mount() (the mcp SDK version
this project depends on doesn't have mount() — verified via dir(FastMCP) —
so this achieves the same "one gateway, domain-separated tool modules"
outcome using the actually-available API): each domain server's registered
tool function is re-registered on the gateway under a prefixed name,
preserving the original description.

Same tools, same approval_gate enforcement, two transports:
- stdio (default): the server is spawned as a subprocess of the agent process,
  zero config, for local dev (see app/agent/mcp_client.py).
- streamable-http: the server runs standalone on MCP_SERVER_HOST:MCP_SERVER_PORT,
  as a genuinely separate/remote process an orchestrator (e.g. watsonx
  Orchestrate) could register by URL instead of spawning.

Run directly for local stdio testing:
    python -m app.mcp_server.server
Run as a standalone remote server over HTTP:
    python -m app.mcp_server.server --transport http
Or set MCP_TRANSPORT=http in .env to change the default for both `main()` above
and the transport app/agent/mcp_client.py connects with.
"""

import functools
import hmac
import inspect
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.db.session import init_db
from app.mcp_server.access_server import access_mcp
from app.mcp_server.identity_server import identity_mcp
from app.mcp_server.rate_limit import check_rate_limit
from app.mcp_server.tools import is_sensitive
from app.mcp_server.ticketing_server import ticketing_mcp

logger = logging.getLogger(__name__)

_settings = get_settings()

# DNS-rebinding protection (MCP spec 2025-11-25: "Servers MUST validate the
# Origin header on all incoming connections... If the Origin header is
# present and invalid, servers MUST respond with HTTP 403 Forbidden.") —
# the installed mcp SDK already implements this (Host + Origin header
# validation) via TransportSecuritySettings, and even auto-enables it with
# a sensible loopback-only allowlist when host is 127.0.0.1/localhost/::1
# (FastMCP.__init__'s own default). _authenticated_streamable_http_app()
# below explicitly (re)configures mcp.settings.transport_security from
# app/config.py's allowlists before every use — the spec-native fix,
# rather than hand-rolling a second Origin check that would only duplicate
# (and could silently conflict with) the SDK's own.
mcp = FastMCP(
    "enterprise-it-automator",
    host=_settings.mcp_server_host,
    port=_settings.mcp_server_port,
)

_DOMAIN_SERVERS = {
    "identity": identity_mcp,
    "access": access_mcp,
    "ticketing": ticketing_mcp,
}


def _rate_limited(tool_name: str, fn):
    """Wraps a tool function with a per-tool-name rate-limit check (MCP
    spec 2025-11-25's "Servers MUST: ... rate limit tool invocations").

    Raising RateLimitExceededError from inside the wrapped function is the
    correct way to surface this to a caller: the mcp SDK's Tool.run()
    catches any exception raised by a tool function and re-raises it as a
    ToolError, which becomes a proper `isError: true` MCP Tool Execution
    Error (spec-defined, actionable feedback a client/LLM can see and back
    off from) — not a raw Python exception or a protocol-level error.
    Wrapping at gateway-composition time (here) rather than inside each
    domain server's own @domain_mcp.tool() function keeps the domain
    servers themselves free of cross-cutting concerns, same reasoning as
    trace_graph_node wrapping graph nodes at registration time in
    app/agent/graph.py rather than decorating their definitions.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            check_rate_limit(tool_name)
            return await fn(*args, **kwargs)

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        check_rate_limit(tool_name)
        return fn(*args, **kwargs)

    return sync_wrapper


async def _compose_gateway() -> None:
    """Re-registers every domain server's tools on the gateway under a
    domain-prefixed name. Runs once at bootstrap, after each domain
    server's tools are already registered on its own FastMCP instance via
    the @domain_mcp.tool() decorators in identity_server.py etc. Carries
    each tool's annotations (readOnlyHint/destructiveHint/idempotentHint/
    openWorldHint — MCP spec 2025-11-25) through to the gateway
    registration, not just name/description — dropping them here would
    silently discard the hints identity_server.py etc. set.
    """
    for prefix, domain_server in _DOMAIN_SERVERS.items():
        domain_tools = await domain_server.list_tools()
        for tool in domain_tools:
            registered = domain_server._tool_manager.get_tool(tool.name)
            namespaced_name = f"{prefix}_{tool.name}"
            mcp.add_tool(
                _rate_limited(namespaced_name, registered.fn),
                name=namespaced_name,
                description=tool.description,
                annotations=tool.annotations,
            )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def is_sensitive_action(tool_name: str) -> bool:
    """Report whether a tool name requires human approval before execution."""
    check_rate_limit("is_sensitive_action")
    return is_sensitive(tool_name)


async def _bootstrap() -> None:
    await init_db()
    await _compose_gateway()


class _BearerTokenMiddleware:
    """Minimal ASGI middleware requiring `Authorization: Bearer <token>` on
    every request before it reaches the MCP handler.

    Not full OAuth 2.1 (Protected Resource Metadata, audience-scoped tokens
    per ROADMAP.md's Stage 4.3) — that's explicitly descoped as too large
    for this project. This closes the more basic gap: FastMCP's
    streamable-HTTP transport applies zero authentication of its own, so
    any network client that can reach MCP_SERVER_HOST:MCP_SERVER_PORT
    could otherwise call tools directly (including replaying a
    guessed/observed approval_id against a sensitive tool — see
    approval_gate.py's executed_at guard for the other half of that fix),
    completely bypassing the FastAPI layer's
    require_api_key/require_reviewer_token. DNS-rebinding (Origin/Host
    header) protection is handled separately by the mcp SDK's own
    TransportSecurityMiddleware, enabled via the `transport_security`
    passed to FastMCP() above — not duplicated here.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        expected = f"Bearer {self._token}"
        if not auth_header or not hmac.compare_digest(auth_header, expected):
            response = JSONResponse(
                {"error": "Missing or invalid Authorization bearer token"}, status_code=401
            )
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


def _authenticated_streamable_http_app() -> Starlette:
    token = _settings.mcp_server_token
    if not token:
        raise RuntimeError(
            "MCP_SERVER_TOKEN is not set — refusing to start the streamable-HTTP "
            "transport unauthenticated. Set MCP_SERVER_TOKEN in .env (and pass it "
            "as an Authorization: Bearer header from any client connecting over "
            "HTTP) before running --transport http."
        )
    # Re-derive from the current _settings (not just what was live at import
    # time) so tests that reassign server_module._settings mid-process (see
    # tests/test_mcp_gateway_auth.py) get the DNS-rebinding allowlists they
    # actually configured, not whatever was true when this module first
    # loaded. mcp.settings.transport_security is read fresh by
    # streamable_http_app() every time it (re)builds the session manager.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_settings.mcp_allowed_host_list,
        allowed_origins=_settings.mcp_allowed_origin_list,
    )
    app = mcp.streamable_http_app()
    app.add_middleware(_BearerTokenMiddleware, token=token)
    return app


def main() -> None:
    import argparse
    import asyncio

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=_settings.mcp_transport,
        help="Transport to serve on (default: MCP_TRANSPORT env/config, else stdio).",
    )
    args = parser.parse_args()

    asyncio.run(_bootstrap())
    if args.transport == "http":
        app = _authenticated_streamable_http_app()
        uvicorn.run(app, host=_settings.mcp_server_host, port=_settings.mcp_server_port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
