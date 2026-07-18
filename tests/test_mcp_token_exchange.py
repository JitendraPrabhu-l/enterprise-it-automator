"""Tests for the MCP HTTP transport's scoped-token exchange
(app/mcp_server/token_exchange.py + the scope-aware half of
app/mcp_server/server.py's _BearerTokenMiddleware) — the right-sized
OAuth-2.1-flavored token exchange described in that module's docstring.

Mirrors tests/test_mcp_gateway_auth.py's real-running-gateway pattern
(raw httpx against a live uvicorn instance, not a full MCP ClientSession —
see that file's module docstring for why) for the integration half, plus
pure unit tests for mint/verify that don't need a running server at all.
"""

import asyncio
import time

import httpx
import jwt
import pytest
import uvicorn

from app.config import get_settings
from app.mcp_server.token_exchange import (
    AUDIENCE,
    ISSUER,
    InvalidScopedTokenError,
    mint_scoped_token,
    verify_scoped_token,
)

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


# --- unit tests: mint/verify, no running server -----------------------------


def test_mint_and_verify_round_trip(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_TOKEN", "admin-secret")
    get_settings.cache_clear()
    try:
        response = mint_scoped_token(["identity", "access"])
        assert response["token_type"] == "Bearer"
        scopes = verify_scoped_token(response["access_token"])
        assert scopes == {"identity", "access"}
    finally:
        get_settings.cache_clear()


def test_verify_rejects_expired_token(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_TOKEN", "admin-secret")
    get_settings.cache_clear()
    try:
        response = mint_scoped_token(["identity"], ttl_seconds=-1)  # already expired
        with pytest.raises(InvalidScopedTokenError):
            verify_scoped_token(response["access_token"])
    finally:
        get_settings.cache_clear()


def test_verify_rejects_token_signed_with_a_different_secret(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_TOKEN", "admin-secret")
    get_settings.cache_clear()
    try:
        forged = jwt.encode(
            {"iss": ISSUER, "aud": AUDIENCE, "scope": "identity",
             "iat": int(time.time()), "exp": int(time.time()) + 300},
            "wrong-secret", algorithm="HS256",
        )
        with pytest.raises(InvalidScopedTokenError):
            verify_scoped_token(forged)
    finally:
        get_settings.cache_clear()


def test_verify_rejects_wrong_audience(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_TOKEN", "admin-secret")
    get_settings.cache_clear()
    try:
        wrong_aud = jwt.encode(
            {"iss": ISSUER, "aud": "some-other-service", "scope": "identity",
             "iat": int(time.time()), "exp": int(time.time()) + 300},
            "admin-secret", algorithm="HS256",
        )
        with pytest.raises(InvalidScopedTokenError):
            verify_scoped_token(wrong_aud)
    finally:
        get_settings.cache_clear()


def test_mint_requires_admin_token_configured(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_TOKEN", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="MCP_SERVER_TOKEN must be set"):
            mint_scoped_token(["identity"])
    finally:
        get_settings.cache_clear()


# --- integration tests: real running gateway --------------------------------


@pytest.fixture
async def isolated_db(monkeypatch, tmp_path):
    from app.db import session as db_session_module

    db_path = tmp_path / "token_exchange_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    await db_session_module.init_db()
    yield
    db_session_module._engine = None
    db_session_module._session_factory = None
    get_settings.cache_clear()


_next_port = [8950]


@pytest.fixture
async def running_gateway(monkeypatch, isolated_db):
    port = _next_port[0]
    _next_port[0] += 1
    monkeypatch.setenv("MCP_SERVER_TOKEN", "real-token")
    monkeypatch.setenv("MCP_SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_SERVER_PORT", str(port))
    get_settings.cache_clear()

    import app.mcp_server.server as server_module

    server_module._settings = get_settings()
    await server_module._bootstrap()
    server_module.mcp._session_manager = None  # see test_mcp_gateway_auth.py's comment on this reset
    app = server_module._authenticated_streamable_http_app()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.05)
                if server_task.done():
                    server_task.result()
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await server_task
        get_settings.cache_clear()


async def _exchange(base_url: str, token: str, scopes: list[str]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=5.0) as client:
        return await client.post(
            f"{base_url}/token/exchange",
            json={"scopes": scopes},
            headers={"Authorization": f"Bearer {token}"},
        )


async def test_exchange_requires_admin_token(running_gateway):
    resp = await _exchange(running_gateway, "wrong-token", ["identity"])
    assert resp.status_code == 401


async def test_exchange_rejects_missing_authorization(running_gateway):
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{running_gateway}/token/exchange", json={"scopes": ["identity"]})
    assert resp.status_code == 401


async def test_exchange_rejects_unknown_domain(running_gateway):
    resp = await _exchange(running_gateway, "real-token", ["not-a-real-domain"])
    assert resp.status_code == 400


async def test_exchange_rejects_empty_scopes(running_gateway):
    resp = await _exchange(running_gateway, "real-token", [])
    assert resp.status_code == 400


async def test_exchange_mints_a_usable_token(running_gateway):
    resp = await _exchange(running_gateway, "real-token", ["identity"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["scope"] == "identity"
    assert body["access_token"]


async def test_scoped_token_cannot_itself_mint_another_token(running_gateway):
    minted = await _exchange(running_gateway, "real-token", ["identity"])
    scoped_token = minted.json()["access_token"]
    resp = await _exchange(running_gateway, scoped_token, ["access"])
    assert resp.status_code == 401


async def _initialize_session(client: httpx.AsyncClient, base_url: str, token: str) -> dict:
    """Streamable-HTTP requires an initialize handshake before any other
    method — a follow-up request without the returned Mcp-Session-Id header
    gets a 400 from FastMCP's OWN session manager, unrelated to this
    project's auth/scope middleware. Returns headers (incl. the session id)
    ready to reuse on a subsequent request with the SAME token.
    """
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {token}"}
    resp = await client.post(base_url + "/mcp", json=_INITIALIZE_PAYLOAD, headers=headers)
    assert resp.status_code == 200, resp.text
    session_id = resp.headers["mcp-session-id"]
    return {**headers, "mcp-session-id": session_id}


async def test_scoped_token_permits_a_tool_call_in_its_domain(running_gateway):
    minted = await _exchange(running_gateway, "real-token", ["identity"])
    scoped_token = minted.json()["access_token"]
    payload = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "identity_get_user", "arguments": {"username": "nobody"}},
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        headers = await _initialize_session(client, running_gateway, scoped_token)
        resp = await client.post(running_gateway + "/mcp", json=payload, headers=headers)
    # Reaches the real MCP handler (which then reports "no such user" INSIDE
    # the JSON-RPC response, a business-logic outcome) rather than being
    # blocked at the HTTP layer — proves the scope check let it through.
    assert resp.status_code == 200
    assert "insufficient_scope" not in resp.text


async def test_scoped_token_rejects_a_tool_call_outside_its_domain(running_gateway):
    minted = await _exchange(running_gateway, "real-token", ["identity"])
    scoped_token = minted.json()["access_token"]
    payload = {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "access_grant_access", "arguments": {"username": "x", "resource": "vpn"}},
    }
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {scoped_token}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway + "/mcp", json=payload, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"] == "insufficient_scope"
    assert 'error="insufficient_scope"' in resp.headers["www-authenticate"]


async def test_scoped_token_permits_discovery_regardless_of_domain(running_gateway):
    minted = await _exchange(running_gateway, "real-token", ["ticketing"])
    scoped_token = minted.json()["access_token"]
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {scoped_token}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway + "/mcp", json=_INITIALIZE_PAYLOAD, headers=headers)
    assert resp.status_code == 200


async def test_admin_token_still_works_unchanged_for_tool_calls(running_gateway):
    payload = {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "access_grant_access", "arguments": {"username": "x", "resource": "vpn"}},
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        headers = await _initialize_session(client, running_gateway, "real-token")
        resp = await client.post(running_gateway + "/mcp", json=payload, headers=headers)
    # Not blocked by scope enforcement at all (admin bypasses it entirely) —
    # whatever happens next is real MCP/business-logic behavior, HTTP 200.
    assert resp.status_code == 200
    assert "insufficient_scope" not in resp.text


async def test_client_caches_scoped_token_across_sessions(monkeypatch, running_gateway):
    """app/agent/mcp_client.py's mcp_session() must not re-exchange a token
    for every single session it opens — the whole point of caching is
    avoiding a network round-trip to the gateway's /token/exchange on
    every ticket run. Opens two sessions back-to-back and confirms only
    ONE real HTTP POST to /token/exchange happened, by counting actual
    httpx.AsyncClient.post calls to that path (not just calls to the
    wrapping function, which would always be 2 regardless of caching).
    """
    import app.agent.mcp_client as mcp_client_module

    monkeypatch.setenv("MCP_SERVER_TOKEN", "real-token")
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_SERVER_URL", f"{running_gateway}/mcp")
    get_settings.cache_clear()
    mcp_client_module._scoped_token_cache.clear()

    exchange_calls = 0
    original_post = httpx.AsyncClient.post

    async def _counting_post(self, url, *args, **kwargs):
        nonlocal exchange_calls
        if "/token/exchange" in str(url):
            exchange_calls += 1
        return await original_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _counting_post)

    try:
        async with mcp_client_module.mcp_session() as session1:
            await session1.list_tools()
        async with mcp_client_module.mcp_session() as session2:
            await session2.list_tools()
    finally:
        mcp_client_module._scoped_token_cache.clear()
        get_settings.cache_clear()

    assert exchange_calls == 1


async def test_expired_scoped_token_is_rejected_on_the_mcp_path(monkeypatch, running_gateway):
    monkeypatch.setenv("MCP_SERVER_TOKEN", "real-token")
    get_settings.cache_clear()
    expired = mint_scoped_token(["identity"], ttl_seconds=-1)["access_token"]
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {expired}"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(running_gateway + "/mcp", json=_INITIALIZE_PAYLOAD, headers=headers)
    assert resp.status_code == 401
