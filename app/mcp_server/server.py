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
import json
import logging
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.db.session import init_db
from app.mcp_server.access_server import access_mcp
from app.mcp_server.identity_server import identity_mcp
from app.mcp_server.prompts import register_prompts
from app.mcp_server.rate_limit import check_rate_limit
from app.mcp_server.registry import get_registry, resolve_domain_for_tool
from app.mcp_server.resources import register_resources
from app.mcp_server.token_exchange import InvalidScopedTokenError, mint_scoped_token, verify_scoped_token
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

# Resources (read-only, app-controlled data — audit trail, employee
# directory) and prompts (reusable ticket-drafting templates) registered
# at import time, same as the domain servers' @domain_mcp.tool()
# decorators — unlike tools, these aren't composed from separate domain
# FastMCP instances (see resources.py/prompts.py docstrings for why).
register_resources(mcp)
register_prompts(mcp)

_DOMAIN_SERVERS = {
    "identity": identity_mcp,
    "access": access_mcp,
    "ticketing": ticketing_mcp,
}


def _logged(tool_name: str, fn):
    """Wraps a tool function so its start/success/error are sent to the
    client as MCP logging notifications (notifications/message —
    MCP spec's Utilities: Logging), not just written to this process's own
    stderr/stdout.

    Server-side stderr logging (Python's `logging` module, unconfigured here
    — see logging_config.py's docstring for why configure_logging() is only
    ever called from app/api/main.py, not this module) only reaches a stdio
    client, since stdio is the one transport where the host captures the
    child process's stderr automatically (per /docs/tools/debugging: "all
    messages logged to stderr will be captured by the host application").
    Over streamable-HTTP, stderr goes nowhere a remote client can see —
    log message notifications are the transport-agnostic mechanism the spec
    actually defines for "tell the client what's happening," so this is the
    only way an HTTP-connected client (e.g. MCP Inspector, or a future
    non-stdio orchestrator) gets any visibility into tool execution at all.

    Wrapped at gateway-composition time alongside _rate_limited, for the
    same reason given there: keeps this cross-cutting concern out of every
    individual @domain_mcp.tool() function.
    """
    async def _emit(level: Literal["debug", "info", "warning", "error"], message: str) -> None:
        try:
            ctx = mcp.get_context()
            await ctx.log(level, message, logger_name=tool_name)
        except (LookupError, ValueError):
            # get_context() falls back to request_context=None (catching its
            # own internal LookupError) when called outside an active MCP
            # request — e.g. tests exercising domain-server tool functions
            # directly, without going through a real MCP session — and
            # ctx.log() then raises ValueError reading that None context.
            # Falling back to a no-op here, rather than letting either
            # propagate, keeps logging purely best-effort: it must never be
            # the reason a tool call fails.
            pass

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            await _emit("debug", f"{tool_name} invoked")
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                await _emit("error", f"{tool_name} failed: {exc}")
                raise
            await _emit("debug", f"{tool_name} completed")
            return result

        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    return sync_wrapper


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
            if registered is None:  # list_tools() just returned it — can't happen
                raise RuntimeError(f"Domain server {prefix!r} lists {tool.name!r} but can't resolve it")
            namespaced_name = f"{prefix}_{tool.name}"
            mcp.add_tool(
                _rate_limited(namespaced_name, _logged(namespaced_name, registered.fn)),
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


_TOKEN_EXCHANGE_PATH = "/token/exchange"


async def _buffer_body(receive: Receive) -> tuple[bytes, Receive]:
    """Reads the full ASGI request body and returns (body, a replaying
    receive callable) — standard pattern for ASGI middleware that needs to
    inspect a body and then still let the wrapped app read it. Only used
    on the path that actually forwards to self._app afterward (the
    /token/exchange short-circuit below never needs to replay, since it
    always terminates the request itself).
    """
    body = b""
    messages: list[Message] = []
    more_body = True
    while more_body:
        message = await receive()
        messages.append(message)
        body += message.get("body", b"")
        more_body = message.get("more_body", False)

    async def _replay() -> Message:
        if messages:
            return messages.pop(0)
        return await receive()

    return body, _replay


def _tool_names_from_jsonrpc_body(body: bytes) -> list[str]:
    """Extracts every `params.name` from `tools/call` request(s) in a
    JSON-RPC body — a single request object or a batched array of them
    (MCP/JSON-RPC both permit batching). Returns [] for anything that
    isn't valid JSON, isn't a tools/call, or has no resolvable name — those
    all fall through to "not a tool-call needing a scope check" rather
    than being treated as an error here; FastMCP's own JSON-RPC handling
    is what actually validates/rejects a malformed request.
    """
    try:
        parsed = json.loads(body) if body else None
    except (ValueError, TypeError):
        return []
    messages = parsed if isinstance(parsed, list) else [parsed]
    names = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("method") != "tools/call":
            continue
        name = (message.get("params") or {}).get("name")
        if isinstance(name, str):
            names.append(name)
    return names


class _BearerTokenMiddleware:
    """ASGI middleware enforcing bearer-token auth on every request before
    it reaches the MCP handler, plus (new) scoped-token domain enforcement
    and the /token/exchange endpoint — see app/mcp_server/token_exchange.py
    for the full design rationale.

    Two credential shapes are accepted on the MCP protocol path itself:
    - The raw MCP_SERVER_TOKEN (admin): full access, unchanged from before
      scoped tokens existed — every existing HTTP-transport deployment
      keeps working with zero config changes.
    - A scoped JWT minted via POST /token/exchange (admin-only to mint):
      valid for exactly the domain(s) it was scoped to; a tools/call for a
      tool outside those domains gets 403 insufficient_scope instead of
      reaching the tool at all.

    Not full OAuth 2.1 (RFC 9728 Protected Resource Metadata, a user-facing
    PKCE authorization-code flow) — that remains explicitly descoped as
    disproportionate to this project (see token_exchange.py's module
    docstring). DNS-rebinding (Origin/Host header) protection is handled
    separately by the mcp SDK's own TransportSecurityMiddleware, enabled
    via the `transport_security` passed to FastMCP() above — not
    duplicated here.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        if scope["path"] == _TOKEN_EXCHANGE_PATH:
            await self._handle_token_exchange(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        presented = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else None

        if presented is not None and hmac.compare_digest(presented, self._token):
            # Admin credential: full access, exactly today's pre-scoped-token behavior.
            await self._app(scope, receive, send)
            return

        if presented is None:
            await JSONResponse(
                {"error": "Missing or invalid Authorization bearer token"}, status_code=401
            )(scope, receive, send)
            return

        try:
            scopes = verify_scoped_token(presented)
        except InvalidScopedTokenError as exc:
            await JSONResponse(
                {"error": f"Invalid or expired scoped token: {exc}"}, status_code=401
            )(scope, receive, send)
            return

        if scope["method"] != "POST":
            # GET (streamable-HTTP's server-initiated SSE direction) carries
            # no tools/call body to check — any valid token may open it.
            await self._app(scope, receive, send)
            return

        body, replay_receive = await _buffer_body(receive)
        tool_names = _tool_names_from_jsonrpc_body(body)
        for tool_name in tool_names:
            domain = resolve_domain_for_tool(tool_name)
            if domain not in scopes:
                await JSONResponse(
                    {
                        "error": "insufficient_scope",
                        "detail": f"Token scoped to {sorted(scopes)} does not cover "
                        f"domain {domain!r} required by tool {tool_name!r}.",
                    },
                    status_code=403,
                    headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
                )(scope, receive, send)
                return

        await self._app(scope, replay_receive, send)

    async def _handle_token_exchange(self, scope: Scope, receive: Receive, send: Send) -> None:
        """POST /token/exchange — admin-only. Never reaches self._app; this
        endpoint is fully synthetic, existing only inside this middleware
        (see server.py module docstring's _TOKEN_EXCHANGE_PATH usage for
        why: bolting a real route onto FastMCP's own Starlette app is more
        fragile across mcp SDK versions than intercepting one fixed path
        here, where every other request is already being inspected anyway).
        """
        if scope["method"] != "POST":
            await JSONResponse({"error": "POST required"}, status_code=405)(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        presented = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else None
        if presented is None or not hmac.compare_digest(presented, self._token):
            await JSONResponse(
                {"error": "POST /token/exchange requires the admin MCP_SERVER_TOKEN"}, status_code=401
            )(scope, receive, send)
            return

        request = Request(scope, receive)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            await JSONResponse({"error": "Request body must be JSON"}, status_code=400)(scope, receive, send)
            return

        requested = payload.get("scopes") if isinstance(payload, dict) else None
        if not isinstance(requested, list) or not requested or not all(isinstance(s, str) for s in requested):
            await JSONResponse(
                {"error": "Body must be {\"scopes\": [<domain>, ...]}"}, status_code=400
            )(scope, receive, send)
            return

        valid_domains = set(get_registry())
        unknown = sorted(set(requested) - valid_domains)
        if unknown:
            await JSONResponse(
                {"error": f"Unknown domain(s): {unknown}. Valid domains: {sorted(valid_domains)}"},
                status_code=400,
            )(scope, receive, send)
            return

        token_response = mint_scoped_token(requested)
        await JSONResponse(token_response, status_code=200)(scope, receive, send)


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


async def _serve(transport: str) -> None:
    # _bootstrap() and the actual serving loop below MUST run in the same
    # asyncio event loop — app/db/session.py's engine is created here (via
    # init_db()) as a module-level singleton, and asyncpg binds every
    # connection it opens to whichever event loop was running at connect
    # time. Previously main() called `asyncio.run(_bootstrap())` (its own
    # throwaway loop, torn down as soon as it returned) and then separately
    # started uvicorn/mcp.run(), which spins up ANOTHER event loop to serve
    # requests — every session_scope() call from a request handler then
    # tried to reuse pooled connections that belonged to the already-closed
    # bootstrap loop. Live-verified against a real docker-compose Postgres
    # container: this produced "asyncpg.exceptions.InterfaceError: cannot
    # perform operation: another operation is in progress" on the first
    # identity_create_user call in HTTP-transport mode. SQLite/aiosqlite
    # doesn't bind connections to a loop the same way, so this was silent
    # under the (default, and previously only ever actually run) SQLite
    # setup — Postgres just exposed a bug that applies to both transports.
    await _bootstrap()
    if transport == "http":
        import uvicorn

        app = _authenticated_streamable_http_app()
        config = uvicorn.Config(app, host=_settings.mcp_server_host, port=_settings.mcp_server_port)
        server = uvicorn.Server(config)
        await server.serve()
    else:
        await mcp.run_stdio_async()


def main() -> None:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=_settings.mcp_transport,
        help="Transport to serve on (default: MCP_TRANSPORT env/config, else stdio).",
    )
    args = parser.parse_args()

    asyncio.run(_serve(args.transport))


if __name__ == "__main__":
    main()
