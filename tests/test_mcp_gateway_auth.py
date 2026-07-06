"""Tests for the MCP gateway's streamable-HTTP transport security:
bearer-token auth (app/mcp_server/server.py's _BearerTokenMiddleware) AND
DNS-rebinding protection (Host/Origin header validation, MCP spec
2025-11-25's "Servers MUST validate the Origin header on all incoming
connections to prevent DNS rebinding attacks").

Bearer-token auth closes a real gap: FastMCP applies zero authentication
to the streamable-HTTP transport by default, so before this, any network
client that could reach MCP_SERVER_HOST:MCP_SERVER_PORT could call
sensitive tools directly (including replaying a guessed/observed
approval_id — see test_approval_gate.py's replay-prevention tests for the
other half of that fix), completely bypassing the FastAPI layer's auth.
Not full OAuth 2.1 (ROADMAP.md Stage 4.3, explicitly descoped) — a static
bearer token closing the "zero auth at all" gap for the documented
remote-deployment mode.

DNS-rebinding protection uses the mcp SDK's OWN TransportSecurityMiddleware
(mcp.server.transport_security), not a hand-rolled Origin check — the SDK
ships this feature already but leaves it disabled "for backwards
compatibility." app/mcp_server/server.py's _authenticated_streamable_http_app()
enables it via app/config.py's MCP_ALLOWED_HOSTS/MCP_ALLOWED_ORIGINS.

Uses raw httpx requests against the real gateway app rather than a full MCP
ClientSession — the mcp SDK's streamable-HTTP client doesn't cleanly
surface a 401/403 as a prompt exception (its retry/stream-reading logic
just hangs waiting for a response that will never arrive in MCP's expected
shape), so asserting on the raw HTTP status/body is both faster and a more
direct test of what actually matters here: does the middleware gate the
request before it reaches the MCP handler.
"""

import asyncio

import httpx
import pytest
import uvicorn

from app.config import get_settings

_INITIALIZE_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.0.1"},
    },
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    from app.db import session as db_session_module

    db_path = tmp_path / "gateway_auth_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


def test_authenticated_app_refuses_to_build_without_token(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_TOKEN", "")
    get_settings.cache_clear()
    try:
        import app.mcp_server.server as server_module

        server_module._settings = get_settings()
        with pytest.raises(RuntimeError, match="MCP_SERVER_TOKEN is not set"):
            server_module._authenticated_streamable_http_app()
    finally:
        get_settings.cache_clear()


_next_port = [8850]


@pytest.fixture
async def running_gateway(monkeypatch, isolated_db, request):
    """Starts the real gateway app (with the bearer-token middleware) on a
    fresh port per test (avoids any TIME_WAIT/rebind interaction between
    sequential tests reusing the same port in one process) and yields its
    base URL, tearing the server down afterward regardless of test outcome.

    Accepts an optional indirect param (a string) setting MCP_ALLOWED_ORIGINS
    for tests exercising Origin validation; defaults to unset (empty
    allowlist), matching this project's real default.
    """
    port = _next_port[0]
    _next_port[0] += 1
    monkeypatch.setenv("MCP_SERVER_TOKEN", "real-token")
    monkeypatch.setenv("MCP_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_SERVER_PORT", str(port))
    allowed_origins = getattr(request, "param", "")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", allowed_origins)
    get_settings.cache_clear()

    import app.mcp_server.server as server_module

    server_module._settings = get_settings()
    await server_module._bootstrap()
    # FastMCP lazily creates ONE StreamableHTTPSessionManager per FastMCP
    # instance and caches it (mcp.streamable_http_app()'s `if
    # self._session_manager is None` check) — that manager's .run() can
    # only ever execute once. `mcp` is a module-level singleton reused
    # across every test in this file, so without resetting this, the
    # second/third test's Starlette lifespan hits "SessionManager .run()
    # can only be called once" and silently fails startup (server.started
    # never flips True, hanging the polling loop below forever). A real
    # server process only calls streamable_http_app() once, so this reset
    # is purely a test-isolation concern, not a production one.
    server_module.mcp._session_manager = None
    app = server_module._authenticated_streamable_http_app()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.05)
                if server_task.done():
                    server_task.result()  # re-raises a startup failure immediately, not silently
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await server_task
        get_settings.cache_clear()


async def test_gateway_rejects_request_with_no_authorization_header(running_gateway):
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway, json=_INITIALIZE_PAYLOAD, headers=_MCP_HEADERS)
    assert resp.status_code == 401


async def test_gateway_rejects_wrong_bearer_token(running_gateway):
    headers = {**_MCP_HEADERS, "Authorization": "Bearer wrong-token"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway, json=_INITIALIZE_PAYLOAD, headers=headers)
    assert resp.status_code == 401


async def test_gateway_accepts_correct_bearer_token(running_gateway):
    headers = {**_MCP_HEADERS, "Authorization": "Bearer real-token"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway, json=_INITIALIZE_PAYLOAD, headers=headers)
    # Correct token reaches the real MCP handler — a 401 here would mean the
    # middleware is still gating even a valid token. The response body is
    # a real MCP protocol response (success or a protocol-level error),
    # never the middleware's own 401 JSON.
    assert resp.status_code == 200
    assert "Missing or invalid Authorization" not in resp.text


async def test_gateway_accepts_request_with_no_origin_header(running_gateway):
    """Absence of Origin is NOT itself invalid per MCP spec 2025-11-25 —
    only a PRESENT-but-disallowed Origin must be rejected. This project's
    real caller (app/agent/mcp_client.py's httpx client) never sends an
    Origin header at all, so requiring one would break the actual
    documented usage."""
    headers = {**_MCP_HEADERS, "Authorization": "Bearer real-token"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway, json=_INITIALIZE_PAYLOAD, headers=headers)
    assert resp.status_code == 200


async def test_gateway_rejects_disallowed_origin(running_gateway):
    """DNS-rebinding protection: a request carrying an Origin header not in
    MCP_ALLOWED_ORIGINS must be refused with 403, even with a correct
    bearer token — this is exactly the scenario where a browser has been
    tricked into pointing a request at this server."""
    headers = {
        **_MCP_HEADERS,
        "Authorization": "Bearer real-token",
        "Origin": "https://evil.example.com",
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway, json=_INITIALIZE_PAYLOAD, headers=headers)
    assert resp.status_code == 403


@pytest.mark.parametrize("running_gateway", ["https://trusted.example.com"], indirect=True)
async def test_gateway_accepts_allowlisted_origin(running_gateway):
    headers = {
        **_MCP_HEADERS,
        "Authorization": "Bearer real-token",
        "Origin": "https://trusted.example.com",
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway, json=_INITIALIZE_PAYLOAD, headers=headers)
    assert resp.status_code == 200


@pytest.mark.parametrize("running_gateway", ["https://trusted.example.com"], indirect=True)
async def test_gateway_rejects_origin_not_in_nonempty_allowlist(running_gateway):
    """A non-empty allowlist must still reject anything not explicitly on
    it — confirms the check is exact-match, not "any Origin passes once
    the allowlist is non-empty"."""
    headers = {
        **_MCP_HEADERS,
        "Authorization": "Bearer real-token",
        "Origin": "https://other.example.com",
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway, json=_INITIALIZE_PAYLOAD, headers=headers)
    assert resp.status_code == 403


async def test_gateway_rejects_disallowed_origin_even_without_a_valid_token(running_gateway):
    """A disallowed Origin combined with a missing/invalid token must still
    be rejected (never silently accepted) — this doesn't assert WHICH
    layer rejects first: _BearerTokenMiddleware (added via
    app.add_middleware, so it wraps OUTSIDE the SDK's own
    TransportSecurityMiddleware, which lives inside streamable_http_app()'s
    ASGI stack) runs its check first in this implementation, so a request
    with neither a valid token nor an allowed Origin gets a 401 here, not
    a 403 — both are equally "rejected", and Origin validation is
    confirmed to independently reject even a CORRECTLY authenticated
    request in test_gateway_rejects_disallowed_origin above."""
    headers = {**_MCP_HEADERS, "Origin": "https://evil.example.com"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway, json=_INITIALIZE_PAYLOAD, headers=headers)
    assert resp.status_code in (401, 403)
